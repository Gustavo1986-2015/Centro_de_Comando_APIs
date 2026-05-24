import asyncio
import logging
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.schemas.canonical import RCCanonicalModel
from app.services.rc_soap import rc_client

logger = logging.getLogger(__name__)

# Configuraciones activas por proveedor y entorno
# (Luego se leerá de system_config.db)
ACTIVE_PROVIDERS = [
    {"name": "schmitz", "env": "prod"},
    {"name": "schmitz", "env": "test"}
]

async def process_provider_events(provider: str, env: str):
    """Procesa pendientes de un único proveedor y entorno."""
    db: Session = get_session(provider, env)
    try:
        pendings = db.query(NormalizedRCEvent).filter(NormalizedRCEvent.status == "pending").limit(50).all()
        
        if not pendings:
            return

        for db_event in pendings:
            try:
                canonical_event = RCCanonicalModel(
                    chassis_number=db_event.chassis_number,
                    latitude=db_event.latitude,
                    longitude=db_event.longitude,
                    speed=db_event.speed,
                    code=db_event.code,
                    date=db_event.date.replace(tzinfo=timezone.utc) if db_event.date else None,
                    altitude=db_event.altitude,
                    battery=db_event.battery,
                    course=db_event.course,
                    humidity=db_event.humidity,
                    ignition=db_event.ignition,
                    odometer=db_event.odometer,
                    temperature=db_event.temperature,
                    serial_number=db_event.serial_number,
                    shipment=db_event.shipment,
                    vehicle_type=db_event.vehicle_type,
                    vehicle_brand=db_event.vehicle_brand,
                    vehicle_model=db_event.vehicle_model
                )

                # Si es test, podríamos NO enviar a RC realmente, o enviarlo a un endpoint de test
                # Por ahora, usamos el comportamiento normal
                success = await rc_client.send_event(canonical_event)
                
                if success:
                    db_event.status = "sent"
                else:
                    db_event.status = "failed"
                    
            except Exception as e:
                logger.error(f"Error procesando evento {db_event.id} en {provider}_{env}: {str(e)}")
                db_event.status = "failed"

        db.commit()

    except Exception as e:
        logger.error(f"Error general en process_provider_events para {provider}_{env}: {str(e)}")
        db.rollback()
    finally:
        db.close()

async def process_pending_events():
    """Ejecuta el procesamiento concurrente (en paralelo) de todas las APIs activas."""
    tasks = []
    for p in ACTIVE_PROVIDERS:
        tasks.append(process_provider_events(p["name"], p["env"]))
    
    # asyncio.gather dispara todas las tareas al mismo tiempo y espera que terminen
    await asyncio.gather(*tasks)


async def purge_provider_events(provider: str, env: str):
    """Purga una BD individual."""
    db: Session = get_session(provider, env)
    try:
        deleted_count = db.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.status.in_(["sent", "failed"])
        ).delete(synchronize_session=False)
        
        db.commit()
        if deleted_count > 0:
            logger.info(f"Purga Automática completada para {provider}_{env}: {deleted_count} eliminados.")
    except Exception as e:
        logger.error(f"Error en purga para {provider}_{env}: {str(e)}")
        db.rollback()
    finally:
        db.close()

async def purge_processed_events():
    """Ejecuta la purga concurrente de todas las APIs."""
    tasks = []
    for p in ACTIVE_PROVIDERS:
        tasks.append(purge_provider_events(p["name"], p["env"]))
        
    await asyncio.gather(*tasks)

async def worker_loop():
    logger.info("Iniciando Worker Background de Telemática (Modo Concurrente)...")
    loop_count = 0
    purge_interval = 180 
    
    while True:
        try:
            await process_pending_events()
            
            loop_count += 1
            if loop_count >= purge_interval:
                await purge_processed_events()
                loop_count = 0
                
        except Exception as e:
            logger.error(f"Error fatal en el loop concurrente del worker: {str(e)}")
            
        await asyncio.sleep(5)
