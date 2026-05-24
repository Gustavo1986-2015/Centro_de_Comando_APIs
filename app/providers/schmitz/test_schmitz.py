import json
import os
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.database import engine, Base, SessionLocal
from app.models.db_models import NormalizedRCEvent
from app.api.routers.schmitz import router

# 1. Configurar DB de prueba
Base.metadata.create_all(bind=engine)
app = FastAPI()
app.include_router(router)
client = TestClient(app)

def test_schmitz_flow():
    # Payload de prueba estilo Schmitz
    payload = {
        "ChassisNumber": "TEST-123",
        "DeviceTime": "2026-05-23T14:30:00+02:00",
        "Reason": {"ItemElementName": "Code99"},
        "StatusData": [
            {
                "Position": {
                    "Latitude": 45.123,
                    "Longitude": 9.456,
                    "GPSSpeed": {"Value": 85.5}
                }
            }
        ]
    }

    # Llamar al endpoint
    response = client.post("/schmitz/webhook", json=payload)
    print("Response Status:", response.status_code)
    print("Response JSON:", response.json())
    
    # Verificar BD
    db = SessionLocal()
    event = db.query(NormalizedRCEvent).filter_by(chassis_number="TEST-123").first()
    if event:
        print(f"DB Event: Chassis={event.chassis_number}, Status={event.status}, Speed={event.speed}, Code={event.code}")
    else:
        print("DB Event NO ENCONTRADO!")
    db.close()

    # Verificar Auditoría
    today_str = datetime.now().strftime('%Y-%m-%d')
    audit_file = f"audit/schmitz/schmitz.jsonl"
    if os.path.exists(audit_file):
        with open(audit_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            print(f"Líneas en Auditoría ({audit_file}): {len(lines)}")
            print("Última línea:", lines[-1].strip())
    else:
        print(f"Archivo de auditoría {audit_file} NO ENCONTRADO!")

if __name__ == "__main__":
    test_schmitz_flow()
