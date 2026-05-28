import asyncio
import logging
import time
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta, date

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.schemas.canonical import RCCanonicalModel
from app.services.rc_soap import rc_client

from app.models.config_models import ProviderConfig, DailyStat

logger = logging.getLogger(__name__)

# Caché en memoria para rastrear reintentos por eventos fallidos debido a errores de autenticación o red
RETRIES_CACHE = {}

# Registro global de eventos para despertar a los workers de forma instantánea
WORKER_TRIGGERS = {}

def trigger_worker(provider: str, env: str):
    """Despierta el worker correspondiente al proveedor y entorno de forma inmediata."""
    key = f"{provider.lower()}_{env.lower()}"
    if key in WORKER_TRIGGERS:
        try:
            WORKER_TRIGGERS[key].set()
        except Exception:
            pass

async def send_batch_and_measure(canonical_events):
    start_time = time.time()
    results = await rc_client.send_events_batch(canonical_events)
    elapsed = time.time() - start_time
    return results, elapsed

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

def get_next_retry_delay(retry_count: int) -> int:
    """Calcula el retroceso exponencial con Jitter (Full Jitter modificado)."""
    import random
    max_delay = min(300, 10 * int(3.5 ** retry_count))
    min_delay = max(10, max_delay // 2)
    return random.randint(min_delay, max_delay)

async def process_provider_events(provider: str, env: str):
    """Procesa pendientes de un único proveedor y entorno en lotes concurrentes en paralelo y aplica backoff."""
    from app.core.queue_factory import QueueFactory
    queue = QueueFactory.get_queue_service()
    try:
        # 1. Obtener pendientes usando el servicio de colas abstraído
        pendings = await queue.get_pending_batch(provider, env, limit=150)
                
        if not pendings:
            return
            
        # 2. Particionar en sub-lotes de hasta 50 eventos
        batch_size = 50
        batches = [pendings[i:i + batch_size] for i in range(0, len(pendings), batch_size)]
        
        soap_tasks = []
        for batch in batches:
            canonical_events = []
            for db_event in batch:
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
            
            # Agregar tarea de despacho SOAP asíncrona midiendo su tiempo
            soap_tasks.append(send_batch_and_measure(canonical_events))
            
        # 3. Disparar todos los sub-lotes en paralelo (concurrencia de red)
        logger.info(f"Enviando {len(pendings)} eventos en {len(batches)} sub-lote(s) en paralelo para {provider}_{env}")
        batches_results = await asyncio.gather(*soap_tasks, return_exceptions=True)
        
        # 4. Procesar y guardar resultados de forma secuencial
        for batch_idx, batch in enumerate(batches):
            batch_outcome = batches_results[batch_idx]
            
            # Manejar excepciones completas de red/transporte para todo el sub-lote
            if isinstance(batch_outcome, Exception):
                logger.error(f"Excepción general en sub-lote {batch_idx + 1} para {provider}_{env}: {batch_outcome}")
                for db_event in batch:
                    current_retries = db_event.retry_count or 0
                    
                    # Backoff exponencial con Jitter: 4 intentos máx.
                    if current_retries < 4:
                        backoff_sec = get_next_retry_delay(current_retries)
                        next_retry = datetime.now() + timedelta(seconds=backoff_sec)
                        await queue.schedule_retry(
                            provider=provider,
                            env=env,
                            event_id=db_event.id,
                            elapsed_sec=None,
                            rc_response=f"Excepción de transporte: {str(batch_outcome)}",
                            job_id=f"rc_conn_err_{int(datetime.now().timestamp())}",
                            retry_count=current_retries + 1,
                            next_retry_at=next_retry
                        )
                        logger.warning(f"Fallo de red en RC para evento {db_event.id}. Reintento {current_retries + 1}/4 programado en {backoff_sec}s. Queda PENDING.")
                    else:
                        await queue.mark_as_failed(
                            provider=provider,
                            env=env,
                            event_id=db_event.id,
                            elapsed_sec=None,
                            rc_response=f"Excepción de transporte: {str(batch_outcome)}",
                            job_id=f"rc_conn_err_{int(datetime.now().timestamp())}"
                        )
                        logger.error(f"Excedidos los 4 reintentos de red para evento {db_event.id}. Marcado FAILED definitivo.")
                continue
                
            results, elapsed_sec = batch_outcome
            
            # Procesar acuse de recibo de eventos individuales dentro del sub-lote
            for idx, db_event in enumerate(batch):
                try:
                    success, job_id, rc_response = results[idx] if results and idx < len(results) else (False, f"rc_err_missing_{int(datetime.now().timestamp())}", "No response mapping for event")
                    
                    if success:
                        await queue.mark_as_sent(
                            provider=provider,
                            env=env,
                            event_id=db_event.id,
                            elapsed_sec=elapsed_sec,
                            rc_response=rc_response,
                            job_id=job_id
                        )
                    else:
                        err_lower = str(rc_response).lower()
                        is_auth_error = any(w in err_lower for w in ["unknown_token", "userunk", "autentica", "token", "incorrecta", "contrase", "conn_err", "connection"])
                        
                        if is_auth_error:
                            current_retries = db_event.retry_count or 0
                            
                            if current_retries < 4:
                                backoff_sec = get_next_retry_delay(current_retries)
                                next_retry = datetime.now() + timedelta(seconds=backoff_sec)
                                await queue.schedule_retry(
                                    provider=provider,
                                    env=env,
                                    event_id=db_event.id,
                                    elapsed_sec=elapsed_sec,
                                    rc_response=rc_response,
                                    job_id=job_id,
                                    retry_count=current_retries + 1,
                                    next_retry_at=next_retry
                                )
                                logger.warning(f"Fallo de autenticación/token en RC para evento {db_event.id}. Reintento {current_retries + 1}/4 programado en {backoff_sec}s. Queda PENDING.")
                            else:
                                await queue.mark_as_failed(
                                    provider=provider,
                                    env=env,
                                    event_id=db_event.id,
                                    elapsed_sec=elapsed_sec,
                                    rc_response=rc_response,
                                    job_id=job_id
                                )
                                logger.error(f"Excedidos los 4 reintentos de autenticación para evento {db_event.id}. Marcado FAILED definitivo.")
                        else:
                            await queue.mark_as_failed(
                                provider=provider,
                                env=env,
                                event_id=db_event.id,
                                elapsed_sec=elapsed_sec,
                                rc_response=rc_response,
                                job_id=job_id
                            )
                except Exception as inner_e:
                    logger.error(f"Error al guardar resultado de evento individual {db_event.id}: {str(inner_e)}")
                    try:
                        await queue.mark_as_failed(
                            provider=provider,
                            env=env,
                            event_id=db_event.id,
                            elapsed_sec=None,
                            rc_response=f"Excepción interna del worker: {str(inner_e)}",
                            job_id=f"worker_err_{int(datetime.now().timestamp())}"
                        )
                    except Exception:
                        pass
        
        # 5. Consolidar estadísticas del día de hoy en la base de datos global de forma asincrónica e independiente
        try:
            update_daily_stats(provider, env)
        except Exception as stats_e:
            logger.error(f"Error al actualizar estadísticas diarias para {provider}_{env}: {stats_e}")
        
    except Exception as e:
        logger.error(f"Error general en process_provider_events para {provider}_{env}: {str(e)}")

def update_daily_stats(provider: str, env: str):
    """Calcula y actualiza las estadísticas de procesamiento del día de hoy en la BD global."""
    from datetime import datetime
    today_start = datetime.combine(datetime.now().date(), datetime.min.time())
    
    db_prov = get_session(provider, env)
    try:
        sent_events = db_prov.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.status == "sent",
            NormalizedRCEvent.created_at >= today_start
        ).all()
        
        failed_events = db_prov.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.status == "failed",
            NormalizedRCEvent.created_at >= today_start
        ).all()
        
        sent_count = len(sent_events)
        failed_count = len(failed_events)
        
        hub_latencies = []
        transmission_latencies = []
        rc_latencies = []
        
        for ev in sent_events:
            if ev.updated_at and ev.created_at:
                rc_lat = getattr(ev, 'rc_latency_sec', None) or 0.0
                hub_lat = max(0.0, (ev.updated_at - ev.created_at).total_seconds() - rc_lat)
                hub_latencies.append(hub_lat)
            if ev.date and ev.created_at:
                created_naive = ev.created_at.replace(tzinfo=None)
                transmission_latencies.append(max(0.0, (created_naive - ev.date).total_seconds()))
            if getattr(ev, 'rc_latency_sec', None) is not None:
                rc_latencies.append(ev.rc_latency_sec)
                
        for ev in failed_events:
            if ev.date and ev.created_at:
                created_naive = ev.created_at.replace(tzinfo=None)
                transmission_latencies.append(max(0.0, (created_naive - ev.date).total_seconds()))
            if getattr(ev, 'rc_latency_sec', None) is not None:
                rc_latencies.append(ev.rc_latency_sec)
                
        avg_hub = sum(hub_latencies) / len(hub_latencies) if hub_latencies else None
        avg_transmission = sum(transmission_latencies) / len(transmission_latencies) if transmission_latencies else None
        avg_rc = sum(rc_latencies) / len(rc_latencies) if rc_latencies else None
        
    except Exception as e:
        logger.error(f"Error al contar estadísticas de hoy para {provider}_{env}: {e}")
        return
    finally:
        db_prov.close()
        
    db_global = get_session("system_config", "global")
    try:
        today_date = datetime.now().date()
        stat = db_global.query(DailyStat).filter(
            DailyStat.date == today_date,
            DailyStat.provider == provider,
            DailyStat.env == env
        ).first()
        
        if not stat:
            stat = DailyStat(
                date=today_date,
                provider=provider,
                env=env,
                sent_count=sent_count,
                failed_count=failed_count,
                avg_transmission_latency_sec=avg_transmission,
                avg_hub_latency_sec=avg_hub,
                avg_rc_latency_sec=avg_rc
            )
            db_global.add(stat)
        else:
            stat.sent_count = sent_count
            stat.failed_count = failed_count
            stat.avg_transmission_latency_sec = avg_transmission
            stat.avg_hub_latency_sec = avg_hub
            stat.avg_rc_latency_sec = avg_rc
            
        db_global.commit()
    except Exception as e:
        logger.error(f"Error al guardar DailyStat en system_config para {provider}_{env}: {e}")
        db_global.rollback()
    finally:
        db_global.close()

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
    
    # Registrar el evento trigger para despertar de forma instantánea
    key = f"{provider.lower()}_{env.lower()}"
    trigger = asyncio.Event()
    WORKER_TRIGGERS[key] = trigger
    
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
            
        try:
            # Esperar run_interval segundos O despertar instantáneamente si se recibe una notificación
            await asyncio.wait_for(trigger.wait(), timeout=run_interval)
            trigger.clear()
        except asyncio.TimeoutError:
            pass

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
