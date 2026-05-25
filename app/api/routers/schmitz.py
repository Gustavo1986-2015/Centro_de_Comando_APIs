from fastapi import APIRouter, Request, Depends, status, Query, HTTPException, Header
from sqlalchemy.orm import Session
import json
import os

from app.database import get_db_provider
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

@router.post("/webhook", status_code=status.HTTP_202_ACCEPTED)
async def schmitz_webhook(
    request: Request, 
    db: Session = Depends(get_db_provider("schmitz")),
    env: str = Query("prod", description="Entorno: test o prod"),
    authorized: bool = Depends(verify_api_key)
):
    """
    Endpoint receptor para webhooks de Schmitz Cargobull.
    Recibe el payload, lo adapta, lo guarda en auditoría dinámica y lo encola en SQLite.
    Devuelve HTTP 202 Accepted.
    """
    try:
        # Parsear JSON crudo
        payload = await request.json()
    except Exception as e:
        return {"error": "Invalid JSON format", "detail": str(e)}

    # 1. Auditoría de evento crudo
    # Guardamos en auditoría (.jsonl rotativo)
    audit_event(provider=f"schmitz_{env}", payload=payload)

    # 2. Mapeo a Canonical Model
    canonical_data = map_schmitz_payload(payload)

    # 3. Guardar en SQLite como 'pending'
    raw_data_str = json.dumps(payload, ensure_ascii=False)
    
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

    return {"status": "accepted"}
