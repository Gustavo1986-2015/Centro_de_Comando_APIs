from fastapi import APIRouter, Request, Depends, status, Query, HTTPException, Header, BackgroundTasks
from sqlalchemy.orm import Session
import asyncio
import json
import os
import threading

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.providers.schmitz.mapper import map_schmitz_payload
from app.core.auditor import audit_event

# Leer configuracion global
REQUIRE_SCHMITZ_AUTH = os.getenv("REQUIRE_SCHMITZ_AUTH", "False").lower() == "true"
SCHMITZ_API_KEY = os.getenv("SCHMITZ_API_KEY", "")

async def verify_api_key(x_api_key: str = Header(None)):
    """Verifica el API Key entrante si la seguridad está habilitada en .env."""
    if REQUIRE_SCHMITZ_AUTH:
        if not x_api_key or x_api_key != SCHMITZ_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API Key"
            )
    return True

router = APIRouter(prefix="/schmitz", tags=["Schmitz"])


def _persist_webhook_event(payload: dict, env: str):
    """
    Ejecuta TODO el trabajo pesado de I/O.
    Al usarse como BackgroundTask, no penaliza el tiempo de respuesta HTTP.
    """
    # 1. Auditoría fire-and-forget: se dispara en su propio hilo daemon sin bloquear
    threading.Thread(
        target=audit_event,
        args=(f"schmitz_{env}", payload),
        daemon=True
    ).start()

    # 2. Mapeo a Canonical Model (CPU puro, ~0.1ms)
    canonical_data = map_schmitz_payload(payload)

    # 3. Persistir en SQLite (camino crítico real)
    raw_data_str = json.dumps(payload, ensure_ascii=False)
    db = get_session("schmitz", env)
    try:
        new_event = NormalizedRCEvent(
            provider="schmitz",
            status="pending",
            raw_data=raw_data_str,

            # Volcamos los datos canónicos
            chassis_number=canonical_data.chassis_number,
            latitude=canonical_data.latitude,
            longitude=canonical_data.longitude,
            speed=canonical_data.speed,
            code=canonical_data.code,
            date=canonical_data.date,
            altitude=canonical_data.altitude,
            battery=canonical_data.battery,
            course=canonical_data.course,
            humidity=canonical_data.humidity,
            ignition=canonical_data.ignition,
            odometer=canonical_data.odometer,
            temperature=canonical_data.temperature,
            serial_number=canonical_data.serial_number,
            shipment=canonical_data.shipment,
            vehicle_type=canonical_data.vehicle_type,
            vehicle_brand=canonical_data.vehicle_brand,
            vehicle_model=canonical_data.vehicle_model
        )
        db.add(new_event)
        db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error persisting webhook: {e}")
    finally:
        db.close()

    # 4. Despertar al worker de forma instantánea
    try:
        from app.worker.processor import trigger_worker
        trigger_worker("schmitz", env)
    except Exception:
        pass


@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def schmitz_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    env: str = Query("prod", description="Entorno: test o prod"),
    authorized: bool = Depends(verify_api_key)
):
    """
    Endpoint receptor para webhooks de Schmitz Cargobull.
    Recibe el payload crudo y responde inmediatamente con HTTP 202.
    El procesamiento y guardado se delegan a un BackgroundTask de FastAPI.
    """
    try:
        payload = await request.json()
    except Exception as e:
        return {"error": "Invalid JSON format", "detail": str(e)}

    # Delegar la persistencia a un background task.
    # Esto asegura que el response se envía al instante (< 50ms)
    background_tasks.add_task(_persist_webhook_event, payload, env)

    return {"status": "accepted"}
