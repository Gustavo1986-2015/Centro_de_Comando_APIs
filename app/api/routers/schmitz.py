from fastapi import APIRouter, Request, Depends, status, Query, HTTPException, Header
import asyncio
import json
import logging
import os

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.providers.schmitz.mapper import map_schmitz_payload
from app.core.auditor import log_raw_payload

from app.models.config_models import ProviderConfig
from app.core.crypto import decrypt
from app.core import latencia, provider_health, safety_net
from app.core.rate_limit import check_rate_limit
from app.core.auth_alerts import registrar_rechazo
from app.core.queue_metrics import registrar_espera_cola
import secrets
import time
import threading as _threading

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schmitz", tags=["Schmitz"])

# Router sin prefijo para cumplir el endpoint oficial del spec Schmitz.
# Schmitz hardcodea /Json/Data como destino — no es negociable con el proveedor.
router_spec = APIRouter(tags=["Schmitz"])

# In-memory queue for webhook batching
# Cola acotada a propósito. Sin techo, si el consumidor se atasca (BD lenta,
# disco saturado) la cola crece sin límite hasta agotar la memoria del proceso.
#
# Dimensionada para la prueba de Schmitz: 80 ev/s sostenidos. El consumidor
# drena hasta 200 ev/s, así que 20.000 posiciones equivalen a ~4 minutos de
# tráfico acumulado — margen de sobra para un pico o una pausa del disco, y
# techo firme para no caer por memoria.
_WEBHOOK_QUEUE_MAXSIZE = int(os.getenv("WEBHOOK_QUEUE_MAXSIZE", "20000"))
_webhook_queue = asyncio.Queue(maxsize=_WEBHOOK_QUEUE_MAXSIZE)
_batch_task = None

# Caché de la clave del webhook, por entorno.
#
# La validación abría una sesión a la base de configuración en CADA petición.
# Medido: 1,13 ms de media pero hasta 125 ms bajo contención, y esa misma base
# recibe las estadísticas diarias. A 40 mensajes por segundo eso son 40 lecturas
# por segundo compitiendo con las escrituras, que es lo que produce los picos de
# latencia y los tiempos de espera agotados.
#
# La clave cambia solo cuando alguien la edita en el panel, así que medio minuto
# de desfase es un intercambio razonable frente al costo por petición.
_AUTH_TTL = 30
_auth_cache: dict[str, tuple[float, str | None]] = {}
_auth_lock = _threading.Lock()


def invalidar_cache_auth(env: str | None = None):
    """Fuerza la relectura tras guardar la configuración desde el panel."""
    with _auth_lock:
        if env:
            _auth_cache.pop(env.lower(), None)
        else:
            _auth_cache.clear()


def _clave_esperada(entorno: str) -> str | None:
    """
    Clave configurada para ese entorno, o None si no hay ninguna.

    Devolver None es distinto de devolver cadena vacía: significa que no se
    puede autenticar a nadie, y el llamador debe rechazar.
    """
    ahora = time.time()

    with _auth_lock:
        cacheada = _auth_cache.get(entorno)
        if cacheada and cacheada[0] > ahora:
            return cacheada[1]

    clave = None
    db = None
    try:
        # La apertura de la sesión entra en el try: si la base no está
        # disponible, la excepción debe convertirse en un rechazo y no
        # propagarse como error 500 desde el endpoint.
        db = get_session("system_config", "global")
        # Se busca SOLO el entorno pedido. Antes había un fallback a "cualquier
        # configuración de schmitz" cuando faltaba la del entorno: eso permitía
        # que una petición a prod se autenticara con la clave de test.
        provider = db.query(ProviderConfig).filter_by(
            provider_name="schmitz", env=entorno
        ).first()
        if provider and provider.webhook_auth_secret_enc:
            clave = decrypt(provider.webhook_auth_secret_enc)
    except Exception as e:
        logger.error(f"[SCHMITZ-{entorno}] No se pudo leer la configuración de acceso: {e}")
        return None
    finally:
        if db is not None:
            db.close()

    with _auth_lock:
        _auth_cache[entorno] = (ahora + _AUTH_TTL, clave)
    return clave


def _validate_schmitz_auth(request: Request, env: str = Query("prod")):
    """Valida la clave del webhook de Schmitz contra la configuración cifrada."""
    entorno = (env or "prod").lower()
    clave_esperada = _clave_esperada(entorno)

    if not clave_esperada:
        registrar_rechazo(
            "schmitz", entorno, "falta configurar la API key",
            "Cargarla desde el panel antes de recibir tráfico.",
        )
        raise HTTPException(
            401,
            f"Schmitz/{entorno} no tiene API key configurada.",
        )

    clave_recibida = request.headers.get("x-api-key", "")
    if not clave_recibida or not secrets.compare_digest(clave_recibida, clave_esperada):
        # Agrupado: un 401 sostenido indica una integración mal configurada y
        # tiene que verse, pero una línea por rechazo bajo carga escondería el
        # problema igual que el silencio.
        registrar_rechazo(
            "schmitz", entorno,
            "API key incorrecta" if clave_recibida else "falta el header x-api-key",
            "Verificar que la clave del proveedor coincida con la del panel.",
        )
        raise HTTPException(401, "API key invalida")

    return True


def _persist_batch(batch: list):
    """
    Guarda un lote de webhooks en SQLite en una sola transacción.
    batch es una lista de tuplas: (payload, env)
    """
    if not batch: return
    
    # Agrupamos por entorno (usualmente todos son del mismo)
    envs = set([item[1] for item in batch])
    
    for current_env in envs:
        # (payload, ingest_id) por entorno: el identificador viaja con el
        # evento desde la recepción.
        items_for_env = [(item[0], item[2]) for item in batch if item[1] == current_env]
        ingest_ids_lote = [iid for _, iid in items_for_env]
        
        # 2. Persistir en SQLite en un solo COMMIT
        db = get_session("schmitz", current_env)
        try:
            events_to_add = []
            for payload, iid_payload in items_for_env:
                try:
                    # Usamos el mapper con extracción de Tenant (el router no recibe headers en el inner batch, se asume tenant generico o payload-based aqui)
                    canonical_list = map_schmitz_payload(payload)
                    raw_json_str   = json.dumps(payload, ensure_ascii=False)
                    # El ingest_id del payload ya se asignó en la recepción. Un
                    # payload puede generar varios eventos canónicos (motor
                    # multi-evento), así que cada uno lleva un sufijo: el índice
                    # convierte el id del payload en uno único por evento, y es
                    # estable entre reintentos.
                    for indice, canonical in enumerate(canonical_list):
                        events_to_add.append(NormalizedRCEvent(
                            provider="schmitz",
                            status="pending",
                            ingest_id=f"{iid_payload}-{indice}",
                            raw_data=raw_json_str,
                            chassis_number=canonical.chassis_number,
                            latitude=canonical.latitude,
                            longitude=canonical.longitude,
                            speed=canonical.speed,
                            code=canonical.code,            # unico campo que varia entre clones
                            date=canonical.date,
                            altitude=canonical.altitude,
                            battery=canonical.battery,
                            course=canonical.course,
                            humidity=canonical.humidity,
                            ignition=canonical.ignition,
                            odometer=canonical.odometer,
                            temperature=canonical.temperature,
                            serial_number=canonical.serial_number,
                            shipment=canonical.shipment,
                            vehicle_type=canonical.vehicle_type,
                            vehicle_brand=canonical.vehicle_brand,
                            vehicle_model=canonical.vehicle_model,
                        ))
                except ValueError as ve:
                    logger.warning(f"Drop and Forget activado: {ve}")
                except Exception as e:
                    logger.warning(f"Excepción capturada en schmitz: {e}")
                    logger.error(f"Error procesando payload en batch: {e}")
            
            if events_to_add:
                db.add_all(events_to_add)
                db.commit()
                # No se confirma nada: el camino feliz nunca escribió en la red
                # de seguridad, así que no hay pendiente que resolver.
                provider_health.report_events_in("schmitz", current_env, len(events_to_add))
        except Exception as e:
            db.rollback()
            # Acá estaba el agujero: se registraba el error y el lote se
            # descartaba. El proveedor ya tenía su 202 y el evento no existía en
            # ninguna parte. Ahora queda en la red de seguridad, que ya lo
            # escribió a disco al recibirlo, y el reintentador lo recupera.
            # Recién acá se escribe a disco: el lote que no pudo entrar. En el
            # camino feliz este archivo no se toca nunca.
            for payload_fallido, iid_fallido in items_for_env:
                safety_net.registrar_pendiente(
                    "schmitz", current_env, iid_fallido, payload_fallido
                )
            transitorio = safety_net.es_transitorio(e)
            logger.error(
                f"Error saving batch de schmitz/{current_env}: {e} | "
                f"{len(ingest_ids_lote)} evento(s) quedan en la red de seguridad "
                f"para reintento{'' if transitorio else ' (error NO transitorio)'}."
            )
        finally:
            db.close()
            
async def _batch_processor_loop():
    """Consume de la cola y guarda en BD cada segundo o cuando hay 100 items."""
    while True:
        batch = []
        try:
            # Esperamos hasta 0.5s para acumular items
            item = await asyncio.wait_for(_webhook_queue.get(), timeout=0.5)
            batch.append(item)
            
            while len(batch) < 100 and not _webhook_queue.empty():
                batch.append(_webhook_queue.get_nowait())
                
        except asyncio.TimeoutError:
            pass

        if batch:
            normalizado = []
            esperas = []
            ahora_pc = time.perf_counter()
            for item in batch:
                if len(item) == 3:
                    payload_i, env_i, encolado_en = item
                    esperas.append((ahora_pc - encolado_en) * 1000.0)
                else:
                    payload_i, env_i = item
                # El identificador se asigna ACÁ, en la recepción. Es lo que
                # hace idempotente al reintentador: sin él, reinsertar
                # duplicaría y el duplicado viajaría a Recurso Confiable.
                #
                # NO se escribe a la red de seguridad todavía. Antes sí, y era
                # un error: a 40 msg/s eran millones de líneas por día, y
                # estado() relee ese archivo completo en cada consulta del
                # panel. Es el mismo defecto que el deque sin tope.
                #
                # Ahora se escribe SOLO si el INSERT falla. El camino feliz no
                # paga nada, y el archivo contiene decenas de líneas en vez de
                # millones. El payload igual está a salvo: el crudo se escribe
                # unas líneas más abajo, antes de persistir.
                iid = safety_net.nuevo_ingest_id()
                normalizado.append((payload_i, env_i, iid))
            batch = normalizado

            if esperas:
                registrar_espera_cola("schmitz", esperas, _webhook_queue.qsize(),
                                      _WEBHOOK_QUEUE_MAXSIZE)

            # 1. Auditoría de crudos.
            #
            # Ya no se crea una tarea por payload: log_raw_payload encola en un
            # anexador con hilo propio y retorna al instante. A 40 msg/s, lo de
            # antes eran 40 tareas por segundo sobre el mismo executor que usa
            # la persistencia, compitiendo con los INSERT.
            for payload, env_val, _iid in batch:
                log_raw_payload("schmitz", env_val, payload)
                
            # Guardar el lote en un thread aparte para no bloquear el API
            await asyncio.to_thread(_persist_batch, batch)
            
            # Despertar worker de forma segura en el main thread
            try:
                from app.worker.processor import trigger_worker
                # Un mismo lote puede mezclar entornos (prod y test llegan por el
                # mismo endpoint). Despertar solo el del primer elemento dejaba
                # los del otro entorno esperando al ciclo natural del worker.
                for env_despertar in {env_val for _, env_val, _ in batch}:
                    trigger_worker("schmitz", env_despertar)
            except Exception as e:
                logger.warning(f"Excepción capturada en schmitz: {e}")
            
            for _ in range(len(batch)):
                _webhook_queue.task_done()

_retry_task = None


async def start_webhook_batch_processor():
    """Inicia el loop de procesamiento por lotes. Llamar desde el startup de la app principal."""
    global _batch_task, _retry_task
    _batch_task = asyncio.create_task(_batch_processor_loop())

    # El reintentador arranca junto con la ingesta. Su primera pasada recupera
    # lo que haya quedado sin persistir de una ejecución anterior: ese es el
    # punto de que la red de seguridad viva en disco y no en memoria.
    _retry_task = asyncio.create_task(
        safety_net.bucle_reintentador(persistir_desde_red_de_seguridad)
    )

    # Sonda del bucle de eventos: mide si el proceso está trabado. Es lo que
    # permite separar "la latencia es de la red" de "la latencia es nuestra".
    asyncio.create_task(latencia.sondear_bucle_eventos())

@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def schmitz_webhook(
    request: Request,
    env: str = Query("prod", description="Entorno: test o prod"),
    authorized: bool = Depends(_validate_schmitz_auth)
):
    try:
        try:
            payload = await request.json()
        except Exception as e:
            logger.warning(f"Excepción capturada en schmitz: {e}")
            # Schmitz manual dice "always return 200/202"
            return {"status": "accepted"}

        allowed, remaining, retry_after = check_rate_limit("schmitz", env)
        if not allowed:
            # El spec de Schmitz exige responder 2xx siempre: un 429 podría
            # hacer que marquen el endpoint como no confiable. Se responde 202
            # y se descarta el excedente, dejando rastro en el log.
            # Se responde aceptado, así que el evento NO se puede descartar:
            # va a la red de seguridad y el reintentador lo persiste. Antes se
            # perdía sin siquiera dejar el crudo en disco, porque este camino
            # corta antes de encolar y log_raw_payload nunca llegaba a correr.
            iid = safety_net.nuevo_ingest_id()
            safety_net.registrar_pendiente("schmitz", env, iid, payload)
            logger.warning(
                f"[SCHMITZ-{env}] Rate limit superado. El payload NO se descarta: "
                f"queda en la red de seguridad para persistir. Reintentar en {retry_after}s."
            )
            return {"status": "accepted", "note": "rate limit"}

        try:
            # Se encola con la marca de recepción: la diferencia contra el
            # momento de persistir es la espera en cola, donde se esconde el
            # atraso bajo carga. El tiempo de respuesta del endpoint no lo
            # revela, porque responde apenas encola.
            _webhook_queue.put_nowait((payload, env, time.perf_counter()))
        except asyncio.QueueFull:
            # El consumidor no da abasto. Un log explícito ya no alcanza: se
            # respondió aceptado, así que el evento tiene que terminar en algún
            # lado. Va a la red de seguridad y el reintentador lo persiste
            # cuando la presión baje.
            iid = safety_net.nuevo_ingest_id()
            safety_net.registrar_pendiente("schmitz", env, iid, payload)
            logger.error(
                f"[SCHMITZ-{env}] Cola de ingesta llena ({_WEBHOOK_QUEUE_MAXSIZE}). "
                f"El payload NO se descarta: queda en la red de seguridad. "
                f"Revisar si el worker o la BD están atascados."
            )
            return {"status": "accepted", "note": "cola saturada"}

        provider_health.set_mode("schmitz", env, "push")
        provider_health.report_fetch_ok("schmitz", env)
    except Exception as e:
        logger.warning(f"Excepción capturada en schmitz: {e}")
        logger.error(f"Error inesperado en webhook: {e}")
    
    return {"status": "accepted"}

@router_spec.post("/Json/Data", status_code=status.HTTP_202_ACCEPTED)
async def schmitz_json_data(
    request: Request,
    x_data_type: str = Header(None, alias="X-Data-Type"),
    env: str = Query("prod", description="Entorno: test o prod"),
    authorized: bool = Depends(_validate_schmitz_auth)
):
    """
    Endpoint oficial del spec Schmitz Push API v1.35.
    Recibe con header X-Data-Type: 'Status' (tiempo real) o 'Trip' (estadisticas).

    TripData: se descarta silenciosamente.
    StatusData: mismo flujo que /schmitz/webhook, entra a la cola en memoria.
    """
    # El cronómetro arranca apenas entra al handler. La dependencia de auth ya
    # corrió antes, así que ese tramo se marca aparte más abajo.
    crono = latencia.Cronometro("schmitz", env)

    if x_data_type and x_data_type.strip().lower() == "trip":
        # TripData sale por acá sin leer el cuerpo. Es la medición más valiosa
        # que tenemos: lo que tarda este camino es infraestructura pura, porque
        # el hub no hace nada. La diferencia contra StatusData es el costo real
        # del procesamiento.
        crono.marca("auth")
        crono.cerrar()
        return {"status": "ok", "message": "TripData recibido y descartado."}

    crono.marca("auth")

    try:
        try:
            payload = await request.json()
        except Exception as e:
            logger.warning(f"Excepción capturada en schmitz: {e}")
            return {"status": "accepted"}
        crono.marca("parseo")

        allowed, remaining, retry_after = check_rate_limit("schmitz", env)
        crono.marca("rate_limit")
        if not allowed:
            # El spec de Schmitz exige responder 2xx siempre: un 429 podría
            # hacer que marquen el endpoint como no confiable. Se responde 202
            # y se descarta el excedente, dejando rastro en el log.
            # Se responde aceptado, así que el evento NO se puede descartar:
            # va a la red de seguridad y el reintentador lo persiste. Antes se
            # perdía sin siquiera dejar el crudo en disco, porque este camino
            # corta antes de encolar y log_raw_payload nunca llegaba a correr.
            iid = safety_net.nuevo_ingest_id()
            safety_net.registrar_pendiente("schmitz", env, iid, payload)
            logger.warning(
                f"[SCHMITZ-{env}] Rate limit superado. El payload NO se descarta: "
                f"queda en la red de seguridad para persistir. Reintentar en {retry_after}s."
            )
            crono.marca("respaldo")
            return {"status": "accepted", "note": "rate limit"}

        try:
            # Se encola con la marca de recepción: la diferencia contra el
            # momento de persistir es la espera en cola, donde se esconde el
            # atraso bajo carga. El tiempo de respuesta del endpoint no lo
            # revela, porque responde apenas encola.
            _webhook_queue.put_nowait((payload, env, time.perf_counter()))
            crono.marca("encolado")
        except asyncio.QueueFull:
            # El consumidor no da abasto. Un log explícito ya no alcanza: se
            # respondió aceptado, así que el evento tiene que terminar en algún
            # lado. Va a la red de seguridad y el reintentador lo persiste
            # cuando la presión baje.
            iid = safety_net.nuevo_ingest_id()
            safety_net.registrar_pendiente("schmitz", env, iid, payload)
            logger.error(
                f"[SCHMITZ-{env}] Cola de ingesta llena ({_WEBHOOK_QUEUE_MAXSIZE}). "
                f"El payload NO se descarta: queda en la red de seguridad. "
                f"Revisar si el worker o la BD están atascados."
            )
            return {"status": "accepted", "note": "cola saturada"}

        provider_health.set_mode("schmitz", env, "push")
        provider_health.report_fetch_ok("schmitz", env)
    except Exception as e:
        logger.warning(f"Excepción capturada en schmitz: {e}")
        logger.error(f"Error inesperado en Json/Data: {e}")
    finally:
        # Siempre se cierra, incluso si hubo error: una petición que falló
        # también consumió tiempo, y esconderla falsearía el promedio.
        crono.cerrar()

    return {"status": "accepted"}


async def persistir_desde_red_de_seguridad(provider: str, env: str,
                                           lote: list[tuple[dict, str]]) -> int:
    """
    Reinserta eventos que quedaron en la red de seguridad.

    Es idempotente por el índice único sobre `ingest_id`: si el evento ya había
    entrado, el INSERT no hace nada en vez de crear un duplicado que terminaría
    viajando a Recurso Confiable.

    Propaga la excepción a propósito: el reintentador necesita distinguir un
    lock —que se reintenta— de un error de datos —que va a cuarentena—.
    """
    if provider.lower() != "schmitz" or not lote:
        return 0

    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    filas = []
    for payload, iid in lote:
        canonical_list = map_schmitz_payload(payload)
        raw_json_str = json.dumps(payload, ensure_ascii=False)
        for indice, canonical in enumerate(canonical_list):
            filas.append({
                "provider": "schmitz",
                "status": "pending",
                "ingest_id": f"{iid}-{indice}",
                "raw_data": raw_json_str,
                "chassis_number": canonical.chassis_number,
                "latitude": canonical.latitude,
                "longitude": canonical.longitude,
                "speed": canonical.speed,
                "code": canonical.code,
                "date": canonical.date,
                "altitude": canonical.altitude,
                "battery": canonical.battery,
                "course": canonical.course,
                "humidity": canonical.humidity,
                "ignition": canonical.ignition,
                "odometer": canonical.odometer,
                "temperature": canonical.temperature,
                "serial_number": canonical.serial_number,
                "shipment": canonical.shipment,
                "vehicle_type": canonical.vehicle_type,
                "vehicle_brand": canonical.vehicle_brand,
                "vehicle_model": canonical.vehicle_model,
            })

    if not filas:
        return 0

    def _insertar() -> int:
        db = get_session("schmitz", env)
        try:
            # ON CONFLICT DO NOTHING contra el índice único de ingest_id.
            stmt = sqlite_insert(NormalizedRCEvent).values(filas)
            stmt = stmt.on_conflict_do_nothing(index_elements=["ingest_id"])
            db.execute(stmt)
            db.commit()
            # Se cuenta lo que quedó REALMENTE en la base, no lo que informa
            # rowcount: en un INSERT masivo con ON CONFLICT, SQLite lo devuelve
            # de forma poco confiable, y si informa 0 cuando sí insertó, el
            # reintentador cree que falló y deja el evento colgado para siempre.
            ids = [f["ingest_id"] for f in filas]
            presentes = (
                db.query(NormalizedRCEvent.ingest_id)
                .filter(NormalizedRCEvent.ingest_id.in_(ids))
                .count()
            )
            return presentes
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    insertados = await asyncio.to_thread(_insertar)
    if insertados:
        provider_health.report_events_in("schmitz", env, insertados)
    return insertados
