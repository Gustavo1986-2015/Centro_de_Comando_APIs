import asyncio
import logging
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.db_models import NormalizedRCEvent
from app.schemas.canonical import RCCanonicalModel
from app.services.rc_soap import rc_client

logger = logging.getLogger(__name__)

async def process_pending_events():
    """
    Extrae eventos en estado 'pending' de SQLite y los envía a RC.
    """
    db: Session = SessionLocal()
    try:
        # Tomar un lote de pendientes (ej. 50 para no ahogar la DB)
        pendings = db.query(NormalizedRCEvent).filter(NormalizedRCEvent.status == "pending").limit(50).all()
        
        if not pendings:
            return

        for db_event in pendings:
            try:
                # Reconstruir modelo canónico desde la DB
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

                # Enviar a RC
                success = await rc_client.send_event(canonical_event)
                
                if success:
                    db_event.status = "sent"
                else:
                    db_event.status = "failed"
                    
            except Exception as e:
                logger.error(f"Error procesando evento {db_event.id}: {str(e)}")
                db_event.status = "failed"

        # Guardar cambios (commits)
        db.commit()

    except Exception as e:
        logger.error(f"Error general en process_pending_events: {str(e)}")
        db.rollback()
    finally:
        db.close()

async def purge_processed_events():
    """
    Purga física (DELETE) de registros procesados ('sent', 'failed') cada cierto tiempo.
    """
    db: Session = SessionLocal()
    try:
        # Borrar todo lo que ya fue enviado o falló. Podríamos dejar un margen de tiempo,
        # pero la consigna indica eliminar los registros procesados.
        deleted_count = db.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.status.in_(["sent", "failed"])
        ).delete(synchronize_session=False)
        
        db.commit()
        if deleted_count > 0:
            logger.info(f"Purga Automática completada: {deleted_count} registros eliminados físicamente.")

    except Exception as e:
        logger.error(f"Error en purga: {str(e)}")
        db.rollback()
    finally:
        db.close()

async def worker_loop():
    """
    Bucle principal del worker background.
    """
    logger.info("Iniciando Worker Background de Telemática...")
    
    # Llevar la cuenta para saber cuándo purgar (cada 15 min)
    # Por ejemplo, si iteramos cada 5 segundos, son 12 iteraciones por min * 15 min = 180
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
            logger.error(f"Error fatal en el loop del worker: {str(e)}")
            
        await asyncio.sleep(5)  # Esperar 5 segundos antes de buscar más pendientes
