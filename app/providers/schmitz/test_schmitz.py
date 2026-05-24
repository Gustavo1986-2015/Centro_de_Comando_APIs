import json
import os
from datetime import datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import get_session, get_engine, Base
from app.models.db_models import NormalizedRCEvent
from app.api.routers.schmitz import router

# 1. Configurar DB de prueba para schmitz
engine = get_engine("schmitz", "test")
app = FastAPI()
app.include_router(router)
client = TestClient(app)

def test_schmitz_flow():
    payload = {
        "ChassisNumber": "TEST-MULTI-DB",
        "DeviceTime": "2026-05-23T14:30:00+02:00",
        "Reason": {"ItemElementName": "Code99"},
        "StatusData": [{"Position": {"Latitude": 45.123, "Longitude": 9.456, "GPSSpeed": {"Value": 85.5}}}]
    }

    # Llamar al endpoint indicando entorno de prueba
    response = client.post("/schmitz/webhook?env=test", json=payload)
    print("Response Status:", response.status_code)
    
    # Verificar BD específica
    db = get_session("schmitz", "test")
    event = db.query(NormalizedRCEvent).filter_by(chassis_number="TEST-MULTI-DB").first()
    if event:
        print(f"DB Event in schmitz_test.db: Chassis={event.chassis_number}, Status={event.status}")
    db.close()

if __name__ == "__main__":
    test_schmitz_flow()
