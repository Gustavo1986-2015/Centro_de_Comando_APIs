from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import json
import logging
import asyncio

from app.database import get_db_provider, get_session
from app.models.config_models import ProviderConfig
from app.core.dynamic_mapper import DynamicMapper
from app.core.queue_factory import QueueFactory
from app.models.db_models import NormalizedRCEvent
from app.core.auditor import log_raw_payload
from app.core.crypto import decrypt
import secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook/dynamic", tags=["iPaaS Dynamic Webhook"])

@router.post("/{provider_name}")
async def dynamic_webhook_receive(
    provider_name: str,
    request: Request,
    env: str = Query("prod", description="Entorno de destino: test o prod")
):
    """
    Endpoint iPaaS Universal: Recibe un payload JSON de cualquier proveedor configurado.
    Extrae la data usando su mapping_schema desde la DB, y lo encola.
    """
    # 1. Validar que el proveedor exista y esté activo
    db_global = get_session("system_config", "global")
    try:
        config = db_global.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider_name),
            ProviderConfig.env == env
        ).first()
        
        if not config:
            raise HTTPException(status_code=404, detail=f"Proveedor '{provider_name}' en entorno '{env}' no está registrado.")
        if not config.is_active:
            raise HTTPException(status_code=403, detail=f"El proveedor '{provider_name}' está desactivado temporalmente.")
            
        # FAIL-CLOSED: sin auth configurada, rechazar
        if not config.webhook_auth_secret_enc:
            raise HTTPException(
                status_code=401,
                detail=f"Webhook no autenticado. Configure API key para {provider_name} en el Dashboard."
            )
        
        # Descifrar clave almacenada
        stored_key = decrypt(config.webhook_auth_secret_enc)
        if not stored_key:
            raise HTTPException(status_code=500, detail="Error interno de autenticacion.")
        
        # Validar header
        header_name = config.webhook_auth_header or "x-api-key"
        provided_key = request.headers.get(header_name, "")
        
        if not secrets.compare_digest(provided_key, stored_key):
            raise HTTPException(status_code=401, detail="API key invalida")
            
        mapping_schema = config.mapping_schema or {}
        if not mapping_schema:
            raise HTTPException(status_code=400, detail="El proveedor no tiene un esquema visual configurado (mapping_schema).")
    finally:
        db_global.close()

    # 2. Atrapar el Payload JSON
    try:
        payload = await request.json()
    except Exception as e:
        logger.warning(f"Excepción capturada en dynamic_webhook: {e}")
        raise HTTPException(status_code=400, detail="El cuerpo de la petición debe ser un JSON válido.")

    # 2.5 Auditoría cruda (fire-and-forget asíncrona)
    asyncio.create_task(asyncio.to_thread(log_raw_payload, provider_name, env, payload))

    # 3. Transformación Dinámica al Modelo Canónico (RC)
    try:
        canonical_events = await run_in_threadpool(
            DynamicMapper.map_payload_multi, 
            payload, mapping_schema, provider_name, env
        )
    except Exception as e:
        logger.warning(f"Excepción capturada en dynamic_webhook: {e}")
        logger.error(f"Error en DynamicMapper para {provider_name}: {e}")
        raise HTTPException(status_code=422, detail=f"Fallo al mapear los datos: {e}")

    # 4. Guardar en Base de Datos Específica / Cola (Patrón Repository)
    db_provider = get_session(provider_name, env)
    try:
        new_events = []
        for canonical_event in canonical_events:
            new_events.append(NormalizedRCEvent(
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
        db_provider.commit()
    except Exception as e:
        logger.warning(f"Excepción capturada en dynamic_webhook: {e}")
        db_provider.rollback()
        logger.error(f"Error DB guardando eventos de {provider_name}: {e}")
        raise HTTPException(status_code=500, detail="Error interno al guardar eventos.")
    finally:
        db_provider.close()

    # 5. Despertar al orquestador instantáneamente
    from app.worker.processor import trigger_worker
    trigger_worker(provider_name, env)

    return {
        "status": "ok",
        "message": f"{len(canonical_events)} evento(s) encolado(s) exitosamente.",
        "events_count": len(canonical_events)
    }
