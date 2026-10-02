from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from datetime import datetime, timezone
import json
import logging
import asyncio

from app.database import get_session
from app.models.config_models import ProviderConfig
from app.core.dynamic_mapper import DynamicMapper
from app.core import provider_health
from app.core.rate_limit import check_rate_limit
from app.models.db_models import NormalizedRCEvent
from app.core.auditor import log_raw_payload
from app.core import admision, safety_net, webhook_auth
from app.core.crypto import decrypt
from app.core.auth_alerts import registrar_rechazo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/dynamic", tags=["iPaaS Dynamic Webhook"])

def _validate_dynamic_auth(
    provider_name: str,
    request: Request,
    env: str = Query("prod", description="Entorno de destino: test o prod")
):
    """Valida auth del webhook dinámico contra DB cifrada (se ejecuta en ThreadPool)."""
    db_global = get_session("system_config", "global")
    try:
        config = db_global.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider_name),
            ProviderConfig.env == env
        ).first()
        
        if not config:
            # Deja rastro con el nombre EXACTO que llegó. Sin esto, un proveedor
            # mal apuntado solo se veía como una línea de acceso con 404, y había
            # que adivinar qué URL estaba usando: pasó con Tive, que mandaba a la
            # raíz y no a la ruta configurada.
            registrar_rechazo(
                provider_name, env, f"proveedor '{provider_name}' no registrado",
                "Revisar la URL configurada en el proveedor: el nombre al final de "
                "/webhook/dynamic/ tiene que coincidir con una integración del panel.",
            )
            raise HTTPException(status_code=404, detail=f"Proveedor '{provider_name}' en entorno '{env}' no está registrado.")
        if not config.is_active:
            raise HTTPException(status_code=403, detail=f"El proveedor '{provider_name}' está desactivado temporalmente.")
            
        if not config.webhook_auth_secret_enc:
            registrar_rechazo(
                provider_name, env, "falta configurar la API key",
                "Cargarla desde el panel antes de recibir tráfico.",
            )
            raise HTTPException(
                status_code=401,
                detail=f"Webhook no autenticado. Configure API key para {provider_name} en el Dashboard."
            )
        
        stored_key = decrypt(config.webhook_auth_secret_enc)
        if not stored_key:
            raise HTTPException(status_code=500, detail="Error interno de autenticacion.")

        # Modo de autenticación. Sin configuración explícita es "header", el
        # comportamiento histórico: las integraciones existentes no cambian.
        try:
            auth_cfg = webhook_auth.resolver_config(getattr(config, "webhook_auth_config", None))
        except ValueError as e:
            # Configuración mal cargada: se rechaza ruidosamente en vez de
            # dejar pasar tráfico sin verificar.
            registrar_rechazo(provider_name, env, f"configuración de firma inválida: {e}",
                              "Revisar la autenticación del webhook en el panel.")
            raise HTTPException(status_code=500, detail="Configuración de autenticación inválida.")

        if auth_cfg["modo"] == webhook_auth.MODO_HEADER:
            header_name = config.webhook_auth_header or "x-api-key"
            provided_key = request.headers.get(header_name, "")
            try:
                webhook_auth.verificar_header(provided_key, stored_key, header_name)
            except webhook_auth.FirmaInvalida as e:
                # Agrupado: un 401 sostenido es el síntoma de una integración mal
                # configurada y tiene que ser visible, pero una línea por rechazo
                # bajo carga escondería el problema igual que el silencio.
                registrar_rechazo(
                    provider_name, env, str(e),
                    "Verificar que la clave del proveedor coincida con la del panel.",
                )
                raise HTTPException(status_code=401, detail="API key invalida")
        # En modo HMAC la verificación NO puede hacerse acá: necesita el cuerpo
        # crudo, que se lee en el handler. Se devuelve lo necesario para que la
        # haga ahí, antes de parsear nada.
            
        mapping_schema = config.mapping_schema or {}
        if not mapping_schema:
            raise HTTPException(status_code=400, detail="El proveedor no tiene un esquema visual configurado (mapping_schema).")
        
        # Pasar configuración de dedup junto con mapping_schema
        return {
            "mapping_schema": mapping_schema,
            "enable_state_dedup": bool(getattr(config, 'enable_state_dedup', False)),
            "dict_enabled": bool((getattr(config, 'enrichment_config', None) or {}).get("enabled")),
            "auth_cfg": auth_cfg,
            # Solo se entrega en modo HMAC, que lo necesita para verificar.
            "secreto_hmac": stored_key if auth_cfg["modo"] == webhook_auth.MODO_HMAC else None,
        }
    finally:
        db_global.close()

def _save_dynamic_events(provider_name, env, canonical_events, payload, ingest_id=None):
    """
    Guarda eventos de manera síncrona, diseñado para ser ejecutado en ThreadPool.

    Propaga la excepción si el INSERT falla: quien llama decide si el evento
    va a la red de seguridad. Antes la convertía en un 500, y un proveedor que
    reintenta pocas veces —Tive reintenta dos y abandona— perdía el evento.
    """
    db_provider = get_session(provider_name, env)
    try:
        new_events = []
        for indice, canonical_event in enumerate(canonical_events):
            new_events.append(NormalizedRCEvent(
                # Estable entre reintentos: es lo que impide duplicar si el
                # evento vuelve a entrar desde la red de seguridad.
                ingest_id=f"{ingest_id}-{indice}" if ingest_id else None,
                chassis_number=canonical_event.chassis_number,
                status="pending",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                raw_data=json.dumps(payload),
                provider=provider_name,
                latitude=canonical_event.latitude,
                longitude=canonical_event.longitude,
                speed=canonical_event.speed,
                code=canonical_event.code,
                date=canonical_event.date,
                altitude=canonical_event.altitude,
                battery=canonical_event.battery,
                course=canonical_event.course,
                humidity=canonical_event.humidity,
                ignition=canonical_event.ignition,
                odometer=canonical_event.odometer,
                temperature=canonical_event.temperature,
                serial_number=canonical_event.serial_number,
                shipment=canonical_event.shipment,
                vehicle_type=canonical_event.vehicle_type,
                vehicle_brand=canonical_event.vehicle_brand,
                vehicle_model=canonical_event.vehicle_model,
                retry_count=0,
                next_retry_at=None
            ))
        db_provider.add_all(new_events)
        provider_health.report_events_in(provider_name, env, len(new_events))
        db_provider.commit()
    except Exception:
        db_provider.rollback()
        raise
    finally:
        db_provider.close()

@router.post("/{provider_name}")
async def dynamic_webhook_receive(
    provider_name: str,
    request: Request,
    env: str = Query("prod", description="Entorno de destino: test o prod"),
    auth_config: dict = Depends(_validate_dynamic_auth)
):
    """
    Endpoint iPaaS Universal: Recibe un payload JSON de cualquier proveedor configurado.
    Extrae la data usando su mapping_schema desde la DB, y lo encola.
    Si el proveedor tiene enable_state_dedup=True, filtra eventos de sensor repetidos
    (Anti-State Flooding) antes de persistir.
    """
    mapping_schema = auth_config["mapping_schema"]
    enable_dedup = auth_config["enable_state_dedup"]
    # Si el proveedor usa diccionario, los IDs sin traducción no se envían a RC
    require_dict_match = auth_config.get("dict_enabled", False)

    # 2. Leer el cuerpo CRUDO. En modo HMAC la firma se calcula sobre estos
    # bytes exactos: parsear y reserializar el JSON cambiaría espacios u orden
    # de claves e invalidaría una firma legítima.
    cuerpo_crudo = await request.body()

    auth_cfg = auth_config.get("auth_cfg") or {"modo": webhook_auth.MODO_HEADER}
    if auth_cfg["modo"] == webhook_auth.MODO_HMAC:
        try:
            webhook_auth.verificar_hmac(
                cuerpo_crudo, request.headers, auth_config["secreto_hmac"], auth_cfg
            )
        except webhook_auth.FirmaInvalida as e:
            # Fallo seguro: firma inválida se rechaza Y se registra con motivo.
            registrar_rechazo(
                provider_name, env, f"firma inválida: {e}",
                "Verificar que la clave secreta del webhook coincida con la del panel.",
            )
            raise HTTPException(status_code=401, detail="Firma inválida")

    try:
        payload = json.loads(cuerpo_crudo)
    except Exception as e:
        logger.warning(f"Excepción capturada en dynamic_webhook: {e}")
        raise HTTPException(status_code=400, detail="El cuerpo de la petición debe ser un JSON válido.")

    # 2.2 Rate limiting por integración (transversal a todos los proveedores)
    allowed, remaining, retry_after = check_rate_limit(provider_name, env)
    if not allowed:
        logger.warning(
            f"[{provider_name}-{env}] Rate limit superado ({retry_after}s para reintentar)."
        )
        raise HTTPException(
            status_code=429,
            detail=f"Límite de peticiones superado para {provider_name}/{env}. "
                   f"Reintentar en {retry_after} segundos.",
            headers={"Retry-After": str(retry_after)},
        )

    # 2.5 Auditoría cruda (fire-and-forget asíncrona)
    asyncio.create_task(asyncio.to_thread(log_raw_payload, provider_name, env, payload))

    # 2.6 Filtro de admisión. Va DESPUÉS de la auditoría a propósito: un
    # evento descartado igual queda en el respaldo crudo, y si mañana cambia
    # el filtro se puede reprocesar. Se responde éxito porque el descarte es
    # deliberado: un error haría que el proveedor reintente algo que no
    # queremos, y sumaría ruido a sus métricas de entrega.
    motivo_descarte = admision.evaluar(payload, mapping_schema)
    if motivo_descarte:
        admision.registrar_descarte(provider_name, env, motivo_descarte)
        return {
            "status": "ok",
            "note": "descartado por el filtro de admisión",
            "motivo": motivo_descarte,
            "events_count": 0,
        }

    # 3. Transformación Dinámica al Modelo Canónico (RC)
    try:
        canonical_events = await run_in_threadpool(
            DynamicMapper.map_payload_multi, 
            payload, mapping_schema, provider_name, env, require_dict_match,
            require_dict_match   # sin diccionario configurado no se consulta la tabla
        )
    except Exception as e:
        logger.warning(f"Excepción capturada en dynamic_webhook: {e}")
        logger.error(f"Error en DynamicMapper para {provider_name}: {e}")
        raise HTTPException(status_code=422, detail=f"Fallo al mapear los datos: {e}")

    if require_dict_match and not canonical_events:
        logger.warning(
            f"[{provider_name}-{env}] Payload descartado: el identificador no tiene "
            f"traducción en el diccionario. No se envía a RC."
        )
        return {"status": "accepted", "note": "sin traducción en diccionario, descartado"}

    # 3.5 Deduplicación de Estado (Anti-State Flooding) — solo si el toggle está activo
    # NOTA: schmitz.py NO pasa por aquí (tiene su propio router /Json/Data con dedup interno).
    if enable_dedup and canonical_events:
        from app.core.state_dedup import should_emit_event, get_base_code
        base_code = get_base_code(mapping_schema)
        original_count = len(canonical_events)
        canonical_events = [
            ev for ev in canonical_events
            if should_emit_event(
                provider=provider_name,
                env=env,
                chassis=ev.chassis_number,
                code=ev.code,
                base_code=base_code,
                mapping_schema=mapping_schema,
                enabled=True
            )
        ]
        filtered = original_count - len(canonical_events)
        if filtered > 0:
            logger.info(f"[DEDUP] {provider_name}/{env}: {filtered} evento(s) suprimidos (sin transición de estado).")

    if not canonical_events:
        return {"status": "ok", "message": "Todos los eventos fueron suprimidos por deduplicación de estado.", "events_count": 0}

    # 4. Guardar en Base de Datos Específica / Cola usando ThreadPool para no bloquear
    ingest_id = safety_net.nuevo_ingest_id()
    try:
        await run_in_threadpool(
            _save_dynamic_events, provider_name, env, canonical_events, payload, ingest_id
        )
    except Exception as e:
        # El evento no se pierde: va a la red de seguridad y el reintentador lo
        # persiste cuando la base esté disponible. Se responde 202 porque el
        # evento QUEDÓ a salvo: devolver 500 haría que el proveedor reintente
        # pocas veces y abandone, que es exactamente la pérdida que se evita.
        safety_net.registrar_pendiente(provider_name, env, ingest_id, payload)
        logger.error(
            f"Error guardando eventos de {provider_name}/{env}: {e} | "
            f"{len(canonical_events)} evento(s) quedan en la red de seguridad."
        )
        return {
            "status": "accepted",
            "note": "resguardado para reintento",
            "events_count": len(canonical_events),
        }

    # 5. Despertar al orquestador instantáneamente
    from app.worker.processor import trigger_worker
    trigger_worker(provider_name, env)

    return {
        "status": "ok",
        "message": f"{len(canonical_events)} evento(s) encolado(s) exitosamente.",
        "events_count": len(canonical_events)
    }


async def persistir_desde_red_de_seguridad(provider: str, env: str,
                                           lote: list[tuple[dict, str]]) -> int:
    """
    Reinserta eventos dinámicos que quedaron en la red de seguridad.

    Vuelve a mapear con el esquema ACTUAL de la integración y reinserta con
    ON CONFLICT DO NOTHING sobre `ingest_id`: si ya había entrado, no duplica.

    Propaga la excepción a propósito: el reintentador necesita distinguir un
    lock —que se reintenta— de un error de datos —que va a cuarentena—.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    if not lote:
        return 0

    db_global = get_session("system_config", "global")
    try:
        config = db_global.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider),
            ProviderConfig.env == env,
        ).first()
        mapping_schema = (config.mapping_schema if config else None) or {}
        dict_enabled = bool(((config.enrichment_config if config else None) or {}).get("enabled"))
    finally:
        db_global.close()

    if not mapping_schema:
        # Sin esquema no hay forma de reconstruir los eventos. No es un lock:
        # es configuración, y el reintentador lo manda a cuarentena con motivo.
        raise ValueError(f"{provider}/{env} ya no tiene esquema de mapeo configurado")

    filas = []
    for payload, iid in lote:
        eventos = DynamicMapper.map_payload_multi(
            payload, mapping_schema, provider, env, dict_enabled, dict_enabled
        )
        for indice, ev in enumerate(eventos):
            filas.append({
                "provider": provider, "status": "pending",
                "ingest_id": f"{iid}-{indice}",
                "raw_data": json.dumps(payload),
                "chassis_number": ev.chassis_number, "latitude": ev.latitude,
                "longitude": ev.longitude, "speed": ev.speed, "code": ev.code,
                "date": ev.date, "altitude": ev.altitude, "battery": ev.battery,
                "course": ev.course, "humidity": ev.humidity, "ignition": ev.ignition,
                "odometer": ev.odometer, "temperature": ev.temperature,
                "serial_number": ev.serial_number, "shipment": ev.shipment,
                "vehicle_type": ev.vehicle_type, "vehicle_brand": ev.vehicle_brand,
                "vehicle_model": ev.vehicle_model, "retry_count": 0,
            })

    if not filas:
        # El mapeo no produjo eventos (p. ej. sin traducción de patente). Se
        # informa como uno "persistido" para cerrarlo: no hay nada que enviar,
        # y reintentarlo no cambiaría el resultado.
        return len(lote)

    def _insertar() -> int:
        db = get_session(provider, env)
        try:
            stmt = sqlite_insert(NormalizedRCEvent).values(filas)
            stmt = stmt.on_conflict_do_nothing(index_elements=["ingest_id"])
            db.execute(stmt)
            db.commit()
            # Se cuenta lo que quedó en la base: rowcount no es confiable en
            # un INSERT masivo con ON CONFLICT.
            ids = [f["ingest_id"] for f in filas]
            return db.query(NormalizedRCEvent.ingest_id).filter(
                NormalizedRCEvent.ingest_id.in_(ids)
            ).count()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    insertados = await asyncio.to_thread(_insertar)
    if insertados:
        provider_health.report_events_in(provider, env, insertados)
    return insertados
