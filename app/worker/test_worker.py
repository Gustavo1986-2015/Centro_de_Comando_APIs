import asyncio
from datetime import datetime, timezone
from app.database import engine, Base, SessionLocal
from app.models.db_models import NormalizedRCEvent
from app.worker.processor import process_pending_events, purge_processed_events
from app.services.rc_soap import rc_client
from app.schemas.canonical import RCCanonicalModel

async def test_worker_flow():
    # 1. Asegurar base de datos
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    
    # 2. Limpiar e Insertar evento de prueba con algunos valores nulos
    db.query(NormalizedRCEvent).delete()
    db.commit()
    
    test_event = NormalizedRCEvent(
        provider="schmitz",
        status="pending",
        raw_data="{}",
        chassis_number="WORKER-TEST-1",
        latitude=10.0,
        longitude=None,  # Debería omitirse en el XML
        speed=55.0,
        code="EventCode",
        date=datetime.now(timezone.utc)
    )
    db.add(test_event)
    db.commit()

    print(f"Evento en DB inicial: status='{test_event.status}', ID={test_event.id}")

    # 3. Probar el formateo XML del SOAP client
    token = await rc_client.get_token()
    canonical = RCCanonicalModel(
        chassis_number="WORKER-TEST-1", 
        latitude=10.0, 
        longitude=None, # Este es nulo, no debe estar en XML
        speed=55.0, 
        code="EventCode", 
        date=datetime.now(timezone.utc)
    )
    xml_str = rc_client._build_xml(token, canonical)
    print("\nXML Generado (Revisa que falte 'longitude' y 'altitude'):")
    print(xml_str)
    
    if "longitude" in xml_str.lower() or "altitude" in xml_str.lower():
        print("ERROR: Los valores nulos están en el XML.")
    else:
        print("OK: Valores nulos correctamente omitidos.")

    # 4. Probar el procesamiento (cambia status a sent)
    await process_pending_events()
    
    # 5. Verificar status final
    db.refresh(test_event)
    print(f"\nEvento en DB luego de procesar: status='{test_event.status}'")

    # 6. Probar la purga
    await purge_processed_events()
    deleted_event = db.query(NormalizedRCEvent).filter_by(chassis_number="WORKER-TEST-1").first()
    if not deleted_event:
        print("OK: Evento eliminado físicamente de la base de datos durante la purga.")
    else:
        print("ERROR: Evento no fue eliminado.")
        
    db.close()

if __name__ == "__main__":
    asyncio.run(test_worker_flow())
