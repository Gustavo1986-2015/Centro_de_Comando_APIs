import asyncio
from datetime import datetime, timezone
from app.database import get_engine, get_session, Base
from app.models.db_models import NormalizedRCEvent
from app.worker.processor import process_pending_events, purge_processed_events, get_active_providers
from app.services.rc_soap import rc_client
from app.schemas.canonical import RCCanonicalModel

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)

async def test_worker_flow():
    # 1. Configurar Mock para no golpear RC real en tests
    import app.services.rc_soap as rc_soap
    rc_soap.RC_USE_MOCK = True

    # Obtener dinámicamente el primer proveedor y entorno activos
    active = get_active_providers()
    if not active:
        provider = "schmitz"
        env = "test"
    else:
        provider = active[0]["name"]
        env = active[0]["env"]
        
    print(f"Probando Worker con Proveedor: {provider}, Entorno: {env}")

    # 2. Asegurar base de datos
    engine = get_engine(provider, env)
    Base.metadata.create_all(bind=engine)
    db = get_session(provider, env)
    
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

    # 3. Probar el procesamiento (cambia status a sent)
    await process_pending_events()
    
    # 4. Verificar status final y simular que el evento fue creado antes de hoy para que la purga lo elimine
    db.refresh(test_event)
    print(f"\nEvento en DB luego de procesar: status='{test_event.status}'")
    
    from datetime import timedelta
    test_event.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    # 5. Probar la purga
    await purge_processed_events()
    deleted_event = db.query(NormalizedRCEvent).filter_by(chassis_number="WORKER-TEST-1").first()
    if not deleted_event:
        print("OK: Evento eliminado físicamente de la base de datos durante la purga.")
    else:
        print("ERROR: Evento no fue eliminado.")
        
    db.close()

if __name__ == "__main__":
    asyncio.run(test_worker_flow())
