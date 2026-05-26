import asyncio
import logging
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.schemas.canonical import RCCanonicalModel
from app.services.rc_soap import rc_client

from app.models.config_models import ProviderConfig

logger = logging.getLogger(__name__)

def get_active_providers():
    db = get_session("system_config", "global")
    try:
        configs = db.query(ProviderConfig).filter(ProviderConfig.is_active == True).all()
        return [{"name": c.provider_name, "env": c.env, "purge_min": c.purge_interval_min} for c in configs]
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        return []
    finally:
        db.close()

async def process_provider_events(provider: str, env: str):
    """Procesa pendientes de un único proveedor y entorno en lotes (batching)."""
    db: Session = get_session(provider, env)
    try:
        pendings = db.query(NormalizedRCEvent).filter(NormalizedRCEvent.status == "pending").limit(50).all()
        
        if not pendings:
            return
            
        canonical_events = []
        for db_event in pendings:
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
            canonical_events.append(canonical_event)
            
        # Despachar lote completo a RC
        logger.info(f"Enviando lote de {len(canonical_events)} eventos a RC para {provider}_{env}")
        results = await rc_client.send_events_batch(canonical_events)
        
        # Mapear los resultados posicionalmente
        for idx, db_event in enumerate(pendings):
            try:
                success, job_id, rc_response = results[idx] if idx < len(results) else (False, f"rc_err_missing_{int(datetime.now().timestamp())}", "No response mapping for event")
                
                db_event.rc_response = rc_response
                db_event.job_id = job_id
                
                if success:
                    db_event.status = "sent"
                else:
                    db_event.status = "failed"
            except Exception as inner_e:
                logger.error(f"Error al guardar resultado de evento individual {db_event.id}: {str(inner_e)}")
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
    active = get_active_providers()
    for p in active:
        tasks.append(process_provider_events(p["name"], p["env"]))
    
    if tasks:
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
    active = get_active_providers()
    for p in active:
        tasks.append(purge_provider_events(p["name"], p["env"]))
        
    if tasks:
        await asyncio.gather(*tasks)

def get_provider_config(provider_name: str, env: str):
    """Obtiene la configuración actual de un proveedor específico desde la BD global."""
    db = get_session("system_config", "global")
    try:
        conf = db.query(ProviderConfig).filter(
            ProviderConfig.provider_name == provider_name,
            ProviderConfig.env == env
        ).first()
        if conf:
            return {
                "is_active": conf.is_active,
                "run_interval_sec": conf.run_interval_sec if conf.run_interval_sec is not None else 5,
                "purge_interval_min": conf.purge_interval_min if conf.purge_interval_min is not None else 180
            }
        return None
    except Exception as e:
        logger.error(f"Error al leer configuración en el worker para {provider_name}_{env}: {e}")
        return None
    finally:
        db.close()

async def api_worker_loop(provider: str, env: str):
    """Loop asíncrono e independiente para procesar eventos de un proveedor y entorno específicos."""
    logger.info(f"Iniciando sub-worker independiente para {provider}_{env}")
    last_purge = datetime.now()
    
    while True:
        run_interval = 5
        try:
            config = get_provider_config(provider, env)
            if config:
                is_active = config["is_active"]
                run_interval = config["run_interval_sec"]
                purge_min = config["purge_interval_min"]
                
                if is_active:
                    # 1. Procesar eventos pendientes
                    await process_provider_events(provider, env)
                    
                    # 2. Verificar si es tiempo de purga
                    now = datetime.now()
                    minutes_since_purge = (now - last_purge).total_seconds() / 60.0
                    if minutes_since_purge >= purge_min:
                        await purge_provider_events(provider, env)
                        last_purge = now
                else:
                    # Si no está activo, dormimos un intervalo corto por defecto para reevaluar
                    run_interval = 5
            else:
                # Si no encontramos configuración en la BD, dormimos por defecto
                run_interval = 5
        except Exception as e:
            logger.error(f"Error en api_worker_loop para {provider}_{env}: {str(e)}")
            run_interval = 5
            
        await asyncio.sleep(run_interval)

async def worker_loop():
    """Inicia y gestiona las corrutinas independientes para cada proveedor registrado."""
    logger.info("Iniciando Worker Background de Telemática (Modo Multitarea Dinámico)...")
    
    db = get_session("system_config", "global")
    try:
        configs = db.query(ProviderConfig).all()
        # Si está vacío (primer inicio), poblar con los registros predeterminados
        if not configs:
            c1 = ProviderConfig(provider_name="schmitz", env="prod")
            c2 = ProviderConfig(provider_name="schmitz", env="test")
            db.add_all([c1, c2])
            db.commit()
            configs = db.query(ProviderConfig).all()
        
        providers = [(c.provider_name, c.env) for c in configs]
    except Exception as e:
        logger.error(f"Error inicializando proveedores en worker_loop: {e}")
        providers = [("schmitz", "prod"), ("schmitz", "test")]
    finally:
        db.close()
        
    tasks = []
    for provider, env in providers:
        task = asyncio.create_task(api_worker_loop(provider, env))
        tasks.append(task)
        
    logger.info(f"Registrados {len(tasks)} sub-workers independientes en ejecución paralela.")
    await asyncio.gather(*tasks)
