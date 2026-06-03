from fastapi import APIRouter, Request, Depends, status, Query, HTTPException, Header
from sqlalchemy.orm import Session
import asyncio
import json
import os
import threading
import logging

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.providers.schmitz.mapper import map_schmitz_payload
from app.core.auditor import audit_event

logger = logging.getLogger(__name__)

REQUIRE_SCHMITZ_AUTH = os.getenv("REQUIRE_SCHMITZ_AUTH", "False").lower() == "true"
SCHMITZ_API_KEY = os.getenv("SCHMITZ_API_KEY", "")

router = APIRouter(prefix="/schmitz", tags=["Schmitz"])

# In-memory queue for webhook batching
_webhook_queue = asyncio.Queue()
_batch_task = None

async def verify_api_key(x_api_key: str = Header(None)):
    if REQUIRE_SCHMITZ_AUTH:
        if not x_api_key or x_api_key != SCHMITZ_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API Key")
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
        
        # 1. Auditoría fire-and-forget
        for payload in items_for_env:
            threading.Thread(target=audit_event, args=(f"schmitz_{current_env}", payload), daemon=True).start()
            
        # 2. Persistir en SQLite en un solo COMMIT
        db = get_session("schmitz", current_env)
        try:
            events_to_add = []
            for payload in items_for_env:
                canonical = map_schmitz_payload(payload)
                events_to_add.append(NormalizedRCEvent(
                    provider="schmitz",
                    status="pending",
                    raw_data=json.dumps(payload, ensure_ascii=False),
                    chassis_number=canonical.chassis_number,
                    latitude=canonical.latitude,
                    longitude=canonical.longitude,
                    speed=canonical.speed,
                    code=canonical.code,
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
                    vehicle_model=canonical.vehicle_model
                ))
            db.add_all(events_to_add)
            db.commit()
        except Exception as e:
            logger.error(f"Error saving batch: {e}")
        finally:
            db.close()
            
        # Despertar worker
        try:
            from app.worker.processor import trigger_worker
            trigger_worker("schmitz", current_env)
        except Exception:
            pass

async def _batch_processor_loop():
    """Consume de la cola y guarda en BD cada segundo o cuando hay 100 items."""
    while True:
        batch = []
        try:
            # Esperar el primer item de forma indefinida
            item = await _webhook_queue.get()
            batch.append(item)
            
            # Recolectar más items durante max 0.5s o hasta llegar a 100
            end_time = asyncio.get_event_loop().time() + 0.5
            while len(batch) < 100:
                timeout = end_time - asyncio.get_event_loop().time()
                if timeout <= 0: break
                try:
                    next_item = await asyncio.wait_for(_webhook_queue.get(), timeout=timeout)
                    batch.append(next_item)
                except asyncio.TimeoutError:
                    break
                    
            # Guardar el lote en un thread aparte
            await asyncio.to_thread(_persist_batch, batch)
            
            for _ in range(len(batch)):
                _webhook_queue.task_done()
                
        except Exception as e:
            logger.error(f"Batch processor error: {e}")
            await asyncio.sleep(1)

async def start_webhook_batch_processor():
    """Inicia el loop de procesamiento por lotes. Llamar desde el startup de la app principal."""
    global _batch_task
    _batch_task = asyncio.create_task(_batch_processor_loop())

@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def schmitz_webhook(
    request: Request,
    env: str = Query("prod", description="Entorno: test o prod"),
    authorized: bool = Depends(verify_api_key)
):
    try:
        payload = await request.json()
    except Exception as e:
        return {"error": "Invalid JSON format", "detail": str(e)}

    # Poner en la cola en memoria (instantáneo, ~0ms)
    _webhook_queue.put_nowait((payload, env))

    return {"status": "accepted"}
