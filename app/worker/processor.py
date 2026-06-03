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
    """Procesa pendientes de un único proveedor y entorno. Cada sub-lote es auto-contenido:
    dispara su SOAP y escribe resultados en BD inmediatamente, sin esperar a los demás."""
    from app.core.queue_factory import QueueFactory
    queue = QueueFactory.get_queue_service(provider, env)
    try:
        # 1. Obtener pendientes usando el servicio de colas abstraído
        limit = 2000
        pendings = await queue.get_pending_batch(provider, env, limit=limit)
                
        if not pendings:
            return False
            
        has_more = len(pendings) >= limit
        # 2. Particionar en sub-lotes de hasta 50 eventos
        batch_size = 50
        batches = [pendings[i:i + batch_size] for i in range(0, len(pendings), batch_size)]
        
        # Contadores compartidos (se acumulan al final)
        batch_metrics = []
        
        async def process_single_batch(batch, batch_idx):
            """Tarea auto-contenida: SOAP + escritura en BD inmediata para un solo sub-lote."""
            metrics = {"sent": 0, "failed": 0, "retry": 0, "soap_ms": 0}
            
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
            
            # Disparar SOAP y medir
            try:
                results, elapsed_sec = await send_batch_and_measure(canonical_events)
                metrics["soap_ms"] = elapsed_sec * 1000
                batch_outcome = (results, elapsed_sec)
            except Exception as e:
                batch_outcome = e
            
            # Clasificar resultados
            updates_to_retry = []
            updates_to_fail = []
            updates_to_sent = []
            
            if isinstance(batch_outcome, Exception):
                logger.error(f"Excepción general en sub-lote {batch_idx + 1} para {provider}_{env}: {batch_outcome}")
                for db_event in batch:
                    current_retries = db_event.retry_count or 0
                    
                    if current_retries < 4:
                        backoff_sec = get_next_retry_delay(current_retries)
                        next_retry = datetime.now() + timedelta(seconds=backoff_sec)
                        updates_to_retry.append({
                            "event_id": db_event.id,
                            "elapsed_sec": None,
                            "rc_response": f"Excepción de transporte: {str(batch_outcome)}",
                            "job_id": f"rc_conn_err_{int(datetime.now().timestamp())}",
                            "retry_count": current_retries + 1,
                            "next_retry_at": next_retry
                        })
                    else:
                        updates_to_fail.append({
                            "event_id": db_event.id,
                            "elapsed_sec": None,
                            "rc_response": f"Excepción de transporte: {str(batch_outcome)}",
                            "job_id": f"rc_conn_err_{int(datetime.now().timestamp())}"
                        })
            else:
                results, elapsed_sec = batch_outcome
                
                for idx, db_event in enumerate(batch):
                    try:
                        success, job_id, rc_response = results[idx] if results and idx < len(results) else (False, f"rc_err_missing_{int(datetime.now().timestamp())}", "No response mapping for event")
                        
                        if success:
                            updates_to_sent.append({
                                "event_id": db_event.id,
                                "elapsed_sec": elapsed_sec,
                                "rc_response": rc_response,
                                "job_id": job_id
                            })
                        else:
                            err_lower = str(rc_response).lower()
                            is_auth_error = any(w in err_lower for w in ["unknown_token", "userunk", "autentica", "token", "incorrecta", "contrase", "conn_err", "connection"])
                            
                            if is_auth_error:
                                current_retries = db_event.retry_count or 0
                                if current_retries < 4:
                                    backoff_sec = get_next_retry_delay(current_retries)
                                    next_retry = datetime.now() + timedelta(seconds=backoff_sec)
                                    updates_to_retry.append({
                                        "event_id": db_event.id,
                                        "elapsed_sec": elapsed_sec,
                                        "rc_response": rc_response,
                                        "job_id": job_id,
                                        "retry_count": current_retries + 1,
                                        "next_retry_at": next_retry
                                    })
                                else:
                                    updates_to_fail.append({
                                        "event_id": db_event.id,
                                        "elapsed_sec": elapsed_sec,
                                        "rc_response": rc_response,
                                        "job_id": job_id
                                    })
                            else:
                                updates_to_fail.append({
                                    "event_id": db_event.id,
                                    "elapsed_sec": elapsed_sec,
                                    "rc_response": rc_response,
                                    "job_id": job_id
                                })
                    except Exception as inner_e:
                        updates_to_fail.append({
                            "event_id": db_event.id,
                            "elapsed_sec": None,
                            "rc_response": f"Excepción interna: {str(inner_e)}",
                            "job_id": f"worker_err_{int(datetime.now().timestamp())}"
                        })
                        
            # Escritura INMEDIATA en BD para este sub-lote (no espera a los demás)
            if updates_to_retry:
                await queue.schedule_batch_retry(provider, env, updates_to_retry)
            if updates_to_fail:
                await queue.mark_batch_as_failed(provider, env, updates_to_fail)
            if updates_to_sent:
                await queue.mark_batch_as_sent(provider, env, updates_to_sent)
                
            metrics["retry"] = len(updates_to_retry)
            metrics["failed"] = len(updates_to_fail)
            metrics["sent"] = len(updates_to_sent)
            return metrics

        # 3. Disparar todos los sub-lotes en paralelo (cada uno auto-contenido con su propio SOAP + DB write)
        logger.info(f"Enviando {len(pendings)} eventos en {len(batches)} sub-lote(s) auto-contenido(s) para {provider}_{env}")
        all_metrics = await asyncio.gather(
            *[process_single_batch(batch, idx) for idx, batch in enumerate(batches)],
            return_exceptions=True
        )
        
        # 4. Consolidar métricas
        total_sent = 0
        total_failed = 0
        total_retry = 0
        soap_ms_total = 0
        
        for m in all_metrics:
            if isinstance(m, Exception):
                logger.error(f"Excepción en sub-lote auto-contenido para {provider}_{env}: {m}")
                continue
            total_sent += m["sent"]
            total_failed += m["failed"]
            total_retry += m["retry"]
            soap_ms_total += m["soap_ms"]
        
        soap_avg_ms = (soap_ms_total / len(batches)) if len(batches) > 0 else 0
        
        # Log de rendimiento estructurado
        logger.info(
            f"batch_processed provider={provider} env={env} "
            f"batch_size={len(pendings)} soap_avg_ms={soap_avg_ms:.0f} "
            f"sent={total_sent} failed={total_failed} retry={total_retry}"
        )
        
        
        # 5. Consolidar estadísticas del día de hoy en la base de datos global de forma asincrónica e independiente
        try:
            await asyncio.to_thread(update_daily_stats, provider, env)
        except Exception as stats_e:
            logger.error(f"Error al actualizar estadísticas diarias para {provider}_{env}: {stats_e}")
            
        return has_more
        
    except Exception as e:
        logger.error(f"Error general en process_provider_events para {provider}_{env}: {str(e)}")

def update_daily_stats(provider: str, env: str):
    """Calcula y actualiza las estadísticas de procesamiento del día de hoy en la BD global mediante agregación SQL."""
    from datetime import datetime, timezone
    from sqlalchemy import func
    local_now = datetime.now().astimezone()
    today_start_local = datetime.combine(local_now.date(), datetime.min.time()).replace(tzinfo=local_now.tzinfo)
    today_start = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    
    db_prov = get_session(provider, env)
    try:
        # Calcular promedios excluyendo eventos con reintentos para tener la latencia real (happy path)
        success_stats = db_prov.query(
            func.avg(NormalizedRCEvent.rc_latency_sec).label('avg_rc'),
            func.avg(
                (func.julianday(NormalizedRCEvent.updated_at) - func.julianday(NormalizedRCEvent.created_at)) * 86400.0 - func.coalesce(NormalizedRCEvent.rc_latency_sec, 0)
            ).label('avg_hub'),
            func.avg(
                (func.julianday(NormalizedRCEvent.created_at) - func.julianday(NormalizedRCEvent.date)) * 86400.0
            ).label('avg_transmission')
        ).filter(
            NormalizedRCEvent.status == "sent",
            NormalizedRCEvent.created_at >= today_start,
            func.coalesce(NormalizedRCEvent.retry_count, 0) == 0
        ).first()

        # Conteos totales (SÍ incluyen los reintentados y fallidos)
        sent_count = db_prov.query(func.count(NormalizedRCEvent.id)).filter(
            NormalizedRCEvent.status == "sent",
            NormalizedRCEvent.created_at >= today_start
        ).scalar() or 0
        
        failed_count = db_prov.query(func.count(NormalizedRCEvent.id)).filter(
            NormalizedRCEvent.status == "failed",
            NormalizedRCEvent.created_at >= today_start
        ).scalar() or 0
        
        avg_hub = success_stats.avg_hub if success_stats else None
        avg_transmission = success_stats.avg_transmission if success_stats else None
        avg_rc = success_stats.avg_rc if success_stats else None
        
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
    """Purga una BD individual eliminando solo los eventos enviados/fallidos anteriores al día de hoy local."""
    db: Session = get_session(provider, env)
    try:
        from datetime import datetime, timezone
        local_now = datetime.now().astimezone()
        today_start_local = datetime.combine(local_now.date(), datetime.min.time()).replace(tzinfo=local_now.tzinfo)
        today_start = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)
        
        deleted_count = db.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.status.in_(["sent", "failed"]),
            NormalizedRCEvent.created_at < today_start
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
    
    # Semáforo para limitar a un máximo de 10 peticiones SOAP concurrentes por cola
    semaphore = asyncio.Semaphore(10)
    
    async def process_with_semaphore():
        async with semaphore:
            return await process_provider_events(provider, env)
    
    # Conjunto para mantener referencias a las tareas en background y evitar que el garbage collector las elimine
    background_tasks = set()
    
    while True:
        run_interval = 5
        try:
            config = await asyncio.to_thread(get_provider_config, provider, env)
            if config:
                is_active = config["is_active"]
                run_interval = config["run_interval_sec"]
                purge_min = config["purge_interval_min"]
                
                if is_active:
                    # El worker simplemente procesa un lote de hasta 2000 eventos.
                    # Internamente `process_provider_events` particiona en sub-lotes de 50 
                    # y los envía en paralelo.
                    has_more = await process_provider_events(provider, env)
                    
                    # Si aún quedan eventos, hacemos que el loop vuelva a ejecutarse casi de inmediato.
                    if has_more:
                        run_interval = 0
                        
                    # Verificar si es tiempo de purga
                    now = datetime.now()
                    minutes_since_purge = (now - last_purge).total_seconds() / 60.0
                    if minutes_since_purge >= purge_min:
                        await purge_provider_events(provider, env)
                        last_purge = now
                        
                else:
                    run_interval = 5
            else:
                run_interval = 5
        except Exception as e:
            logger.error(f"Error en api_worker_loop para {provider}_{env}: {str(e)}")
            run_interval = 5
            
        try:
            if run_interval > 0:
                await asyncio.wait_for(trigger.wait(), timeout=run_interval)
                trigger.clear()
                # Micro-batching: Si un evento nos despertó, esperamos un breve momento (100ms) para recolectar ráfagas sin violar SLA < 250ms
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(0.01) # Pausa mínima para ceder el Event Loop sin retrasar la ráfaga
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
            c3 = ProviderConfig(provider_name="protrack", env="prod")
            c4 = ProviderConfig(provider_name="protrack", env="test")
            db.add_all([c1, c2, c3, c4])
            db.commit()
            configs = db.query(ProviderConfig).all()
        else:
            # Agregar registros de Protrack si aún no existen (migración incremental)
            existing_names = {(c.provider_name, c.env) for c in configs}
            to_add = []
            if ("protrack", "prod") not in existing_names:
                to_add.append(ProviderConfig(provider_name="protrack", env="prod"))
            if ("protrack", "test") not in existing_names:
                to_add.append(ProviderConfig(provider_name="protrack", env="test"))
            if to_add:
                db.add_all(to_add)
                db.commit()
                configs = db.query(ProviderConfig).all()
        
        providers = [(c.provider_name, c.env) for c in configs]
        
        # Recuperación de Desastres: Revertir 'processing' a 'pending' tras reinicio brusco
        for provider, env in providers:
            try:
                db_prov = get_session(provider, env)
                from app.models.db_models import NormalizedRCEvent
                recovered = db_prov.query(NormalizedRCEvent).filter(NormalizedRCEvent.status == "processing").update(
                    {"status": "pending"}, synchronize_session=False
                )
                if recovered > 0:
                    logger.warning(f"Recuperación: {recovered} eventos atascados revertidos a 'pending' en {provider}_{env}")
                db_prov.commit()
                db_prov.close()
            except Exception as rec_err:
                logger.error(f"Error recuperando eventos atascados para {provider}_{env}: {rec_err}")
                
    except Exception as e:
        logger.error(f"Error inicializando proveedores en worker_loop: {e}")
        providers = [("schmitz", "prod"), ("schmitz", "test")]
    finally:
        db.close()
        
    tasks = []
    for provider, env in providers:
        task = asyncio.create_task(api_worker_loop(provider, env))
        tasks.append(task)
        # Lanzar loop de polling PULL para Protrack
        if provider == "protrack":
            poll_task = asyncio.create_task(protrack_poll_loop(provider, env))
            tasks.append(poll_task)
        
    logger.info(f"Registrados {len(tasks)} sub-workers independientes en ejecución paralela.")
    await asyncio.gather(*tasks)


# ============================================================
# PROTRACK — Loop de Polling PULL
# ============================================================

async def poll_protrack(provider: str, env: str):
    """
    Consulta la API Protrack365 (PULL):
      1. Lista dispositivos -> obtiene IMEIs y metadata
      2. Consulta posición actual (/api/track)
      3. Mapea al modelo canónico
      4. Inserta en BD como eventos 'pending'
      5. Despierta el worker de envío a RC
    """
    from app.providers.protrack.client import get_protrack_client
    from app.providers.protrack.mapper import map_protrack_track
    import json
    from app.core.auditor import audit_event
    import threading

    try:
        client = get_protrack_client(env)
    except RuntimeError as e:
        logger.error(f"[Protrack] No se pudo crear cliente para env='{env}': {e}")
        return

    try:
        # 1. Obtener lista de dispositivos (IMEI + metadata)
        devices = await client.get_devices()
        if not devices:
            logger.debug(f"[Protrack] Sin dispositivos para env='{env}'.")
            return

        device_index = {d.get("imei"): d for d in devices if d.get("imei")}
        imeis = list(device_index.keys())

        # 2. Posición actual
        tracks = await client.get_track(imeis)
        if not tracks:
            logger.debug(f"[Protrack] Sin datos de posición para env='{env}'.")
            return

        # 3. Mapear y persistir
        db = get_session(provider, env)
        try:
            events_to_add = []
            for track in tracks:
                imei_key = str(track.get("imei", "")).strip()
                dev_info = device_index.get(imei_key, {})

                try:
                    canonical = map_protrack_track(track, dev_info)
                except Exception as map_err:
                    logger.warning(f"[Protrack] Error de mapping para IMEI {imei_key}: {map_err}")
                    continue

                # Auditoría fire-and-forget
                threading.Thread(
                    target=audit_event,
                    args=(f"protrack_{env}", track),
                    daemon=True
                ).start()

                from app.models.db_models import NormalizedRCEvent
                events_to_add.append(NormalizedRCEvent(
                    provider=provider,
                    status="pending",
                    raw_data=json.dumps(track, ensure_ascii=False),
                    chassis_number=canonical.chassis_number,
                    latitude=canonical.latitude,
                    longitude=canonical.longitude,
                    speed=canonical.speed,
                    code=canonical.code,
                    date=canonical.date,
                    altitude=canonical.altitude,
                    battery=canonical.battery,
                    course=canonical.course,
                    humidity=canonical.humidity,
                    ignition=canonical.ignition,
                    odometer=canonical.odometer,
                    temperature=canonical.temperature,
                    serial_number=canonical.serial_number,
                    shipment=canonical.shipment,
                    vehicle_type=canonical.vehicle_type,
                    vehicle_brand=canonical.vehicle_brand,
                    vehicle_model=canonical.vehicle_model,
                ))

            if events_to_add:
                db.add_all(events_to_add)
                db.commit()
                logger.info(
                    f"[Protrack] {len(events_to_add)} eventos insertados en BD "
                    f"(provider={provider}, env={env})."
                )
                # Despertar el worker de procesamiento (envío a RC)
                trigger_worker(provider, env)

        except Exception as db_err:
            logger.error(f"[Protrack] Error al persistir eventos para env='{env}': {db_err}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"[Protrack] Error en poll_protrack env='{env}': {e}")


async def protrack_poll_loop(provider: str, env: str):
    """
    Loop de polling PULL para Protrack.
    Lee el intervalo desde la configuración activa del proveedor;
    si no hay config, usa el valor de la variable de entorno PROTRACK_POLL_INTERVAL_SEC (default 30s).
    Se detiene (durmiendo) si el proveedor está desactivado en la BD.
    """
    import os
    default_interval = int(os.getenv("PROTRACK_POLL_INTERVAL_SEC", "30"))
    logger.info(f"[Protrack] Iniciando poll_loop para env='{env}' (intervalo default: {default_interval}s).")

    while True:
        interval = default_interval
        try:
            config = await asyncio.to_thread(get_provider_config, provider, env)
            if config:
                if not config["is_active"]:
                    # Proveedor desactivado: dormir y volver a verificar
                    await asyncio.sleep(10)
                    continue
                # Usar run_interval_sec de la config como intervalo de polling
                interval = config["run_interval_sec"] if config["run_interval_sec"] else default_interval

            await poll_protrack(provider, env)

        except Exception as e:
            logger.error(f"[Protrack] Error inesperado en protrack_poll_loop env='{env}': {e}")

        await asyncio.sleep(interval)
