from fastapi import APIRouter, Request, Depends, status, Query, HTTPException, Header
from sqlalchemy.orm import Session
import asyncio
import json
import os
import logging

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.providers.schmitz.mapper import map_schmitz_payload
from app.core.auditor import log_raw_payload

from app.models.config_models import ProviderConfig
from app.core.crypto import decrypt
import secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schmitz", tags=["Schmitz"])

# Router sin prefijo para cumplir el endpoint oficial del spec Schmitz.
# Schmitz hardcodea /Json/Data como destino — no es negociable con el proveedor.
router_spec = APIRouter(tags=["Schmitz"])

# In-memory queue for webhook batching
_webhook_queue = asyncio.Queue()
_batch_task = None

async def _validate_schmitz_auth(request: Request, env: str = Query("prod")):
    """Valida auth del webhook Schmitz contra DB cifrada."""
    db = get_session("system_config", "global")
    try:
        # Buscamos la config global del webhook, o podríamos buscar la config por entorno
        # En el spec, PUSH webhooks podrían tener configs de test/prod. Buscaremos prod por default.
        # Schmitz suele ser provider global o por env.
        provider = db.query(ProviderConfig).filter_by(provider_name="schmitz", env=env).first()
        if not provider:
            # Fallback a prod si llega en test pero solo hay config prod
            provider = db.query(ProviderConfig).filter_by(provider_name="schmitz").first()
            
        if not provider or not provider.webhook_auth_secret_enc:
            raise HTTPException(401, "Schmitz webhook no autenticado. Configure API key en Dashboard.")
        
        stored_key = decrypt(provider.webhook_auth_secret_enc)
        provided_key = request.headers.get("x-api-key", "")
        
        if not stored_key or not provided_key or not secrets.compare_digest(provided_key, stored_key):
            raise HTTPException(401, "API key invalida")
    finally:
        db.close()
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
                # Avisar al worker que hay datos listos, el env es el del primer elemento del batch
                trigger_worker("schmitz", batch[0][1])
            except Exception as e:
                logger.warning(f"Excepción capturada en schmitz: {e}")
                pass
            
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

        _webhook_queue.put_nowait((payload, env))
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

        _webhook_queue.put_nowait((payload, env))
    except Exception as e:
        logger.warning(f"Excepción capturada en schmitz: {e}")
        logger.error(f"Error inesperado en Json/Data: {e}")
        
    return {"status": "accepted"}
