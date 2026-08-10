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
from app.core import provider_health
from app.core.rate_limit import check_rate_limit
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
        logger.warning(
            f"[SCHMITZ-{entorno}] Petición rechazada: no hay API key configurada "
            f"para ese entorno. Cargarla desde el panel antes de recibir tráfico."
        )
        raise HTTPException(
            401,
            f"Schmitz/{entorno} no tiene API key configurada.",
        )

    clave_recibida = request.headers.get("x-api-key", "")
    if not clave_recibida or not secrets.compare_digest(clave_recibida, clave_esperada):
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
        items_for_env = [item[0] for item in batch if item[1] == current_env]
        
        # 2. Persistir en SQLite en un solo COMMIT
        db = get_session("schmitz", current_env)
        try:
            events_to_add = []
            for payload in items_for_env:
                try:
                    # Usamos el mapper con extracción de Tenant (el router no recibe headers en el inner batch, se asume tenant generico o payload-based aqui)
                    canonical_list = map_schmitz_payload(payload)
                    raw_json_str   = json.dumps(payload, ensure_ascii=False)
                    for canonical in canonical_list:
                        events_to_add.append(NormalizedRCEvent(
                            provider="schmitz",
                            status="pending",
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
        except Exception as e:
            logger.warning(f"Excepción capturada en schmitz: {e}")
            logger.error(f"Error saving batch: {e}")
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
            # 1. Auditoría fire-and-forget asíncrona (DEBT-04)
            for payload, env_val in batch:
                asyncio.create_task(asyncio.to_thread(log_raw_payload, "schmitz", env_val, payload))
                
            # Guardar el lote en un thread aparte para no bloquear el API
            await asyncio.to_thread(_persist_batch, batch)
            
            # Despertar worker de forma segura en el main thread
            try:
                from app.worker.processor import trigger_worker
                # Un mismo lote puede mezclar entornos (prod y test llegan por el
                # mismo endpoint). Despertar solo el del primer elemento dejaba
                # los del otro entorno esperando al ciclo natural del worker.
                for env_despertar in {env_val for _, env_val in batch}:
                    trigger_worker("schmitz", env_despertar)
            except Exception as e:
                logger.warning(f"Excepción capturada en schmitz: {e}")
            
            for _ in range(len(batch)):
                _webhook_queue.task_done()

async def start_webhook_batch_processor():
    """Inicia el loop de procesamiento por lotes. Llamar desde el startup de la app principal."""
    global _batch_task
    _batch_task = asyncio.create_task(_batch_processor_loop())

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
            logger.warning(
                f"[SCHMITZ-{env}] Rate limit superado, payload descartado. "
                f"Reintentar en {retry_after}s."
            )
            return {"status": "accepted", "note": "rate limit"}

        try:
            _webhook_queue.put_nowait((payload, env))
        except asyncio.QueueFull:
            # El consumidor no da abasto. Se descarta el payload dejando rastro:
            # aceptar y perder en silencio sería peor que un log explícito.
            logger.error(
                f"[SCHMITZ-{env}] Cola de ingesta llena ({_WEBHOOK_QUEUE_MAXSIZE}). "
                f"Payload descartado. Revisar si el worker o la BD están atascados."
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
    if x_data_type and x_data_type.strip().lower() == "trip":
        return {"status": "ok", "message": "TripData recibido y descartado."}

    try:
        try:
            payload = await request.json()
        except Exception as e:
            logger.warning(f"Excepción capturada en schmitz: {e}")
            return {"status": "accepted"}

        allowed, remaining, retry_after = check_rate_limit("schmitz", env)
        if not allowed:
            # El spec de Schmitz exige responder 2xx siempre: un 429 podría
            # hacer que marquen el endpoint como no confiable. Se responde 202
            # y se descarta el excedente, dejando rastro en el log.
            logger.warning(
                f"[SCHMITZ-{env}] Rate limit superado, payload descartado. "
                f"Reintentar en {retry_after}s."
            )
            return {"status": "accepted", "note": "rate limit"}

        try:
            _webhook_queue.put_nowait((payload, env))
        except asyncio.QueueFull:
            # El consumidor no da abasto. Se descarta el payload dejando rastro:
            # aceptar y perder en silencio sería peor que un log explícito.
            logger.error(
                f"[SCHMITZ-{env}] Cola de ingesta llena ({_WEBHOOK_QUEUE_MAXSIZE}). "
                f"Payload descartado. Revisar si el worker o la BD están atascados."
            )
            return {"status": "accepted", "note": "cola saturada"}

        provider_health.set_mode("schmitz", env, "push")
        provider_health.report_fetch_ok("schmitz", env)
    except Exception as e:
        logger.warning(f"Excepción capturada en schmitz: {e}")
        logger.error(f"Error inesperado en Json/Data: {e}")
        
    return {"status": "accepted"}
