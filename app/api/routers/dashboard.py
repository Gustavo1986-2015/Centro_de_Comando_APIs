from fastapi import APIRouter, Request, Query, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import asyncio
import json
import os
import glob

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.models.config_models import ProviderConfig, DailyStat
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, case

from collections import deque
import time as _time

# Tablas que el administrador puede editar desde el Visor de BD.
# Las tablas operativas (normalized_rc_events, etc.) son de SOLO LECTURA siempre.
EDITABLE_TABLES = {
    "provider_config",
    "provider_dictionary",
    "daily_stats",
}

PUSH_SLA_MS   = 250
PUSH_WIN_SECS = 86400  # 24h

push_latency_store: dict[str, deque] = {}
# formato: { "schmitz": deque([(timestamp, latency_sec), ...]) }

def record_push_latency(provider: str, latency: float):
    key = provider.lower()
    if key not in push_latency_store:
        push_latency_store[key] = deque()
    push_latency_store[key].append((_time.time(), latency))
    cutoff = _time.time() - PUSH_WIN_SECS
    while push_latency_store[key] and push_latency_store[key][0][0] < cutoff:
        push_latency_store[key].popleft()

def get_push_stats(provider_key: str | None = None) -> dict:
    """Calcula avg_ms, compliance_pct y count para el provider dado (o todos)."""
    if provider_key and provider_key.lower() != 'all':
        samples = list(push_latency_store.get(provider_key.lower(), []))
    else:
        samples = [s for q in push_latency_store.values() for s in q]
    if not samples:
        return {"avg_ms": 0.0, "compliance_pct": 100.0, "count": 0}
    ms_vals    = [lat * 1000 for _, lat in samples]
    compliant  = sum(1 for v in ms_vals if v <= PUSH_SLA_MS)
    return {
        "avg_ms":          round(sum(ms_vals) / len(ms_vals), 3),
        "compliance_pct":  round(compliant / len(ms_vals) * 100, 1),
        "count":           len(ms_vals),
    }


security = HTTPBasic()

def verify_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = os.getenv("DASHBOARD_USER", "admin")
    correct_pass = os.getenv("DASHBOARD_PASSWORD", "changeme")
    user_ok = secrets.compare_digest(credentials.username.encode(), correct_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), correct_pass.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="app/templates")

class ConfigUpdate(BaseModel):
    id: int
    is_active: bool
    rc_user: str
    rc_password: str
    purge_interval_min: int
    run_interval_sec: int
    queue_backend: str

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request, _: None = Depends(verify_dashboard_auth)):
    """Renderiza el Centro de Comando en Vivo."""
    response = templates.TemplateResponse(
        request=request, 
        name="index.html"
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

async def get_stats_data(
    status_filter: str = None,
    provider_filter: str = None
):
    """
    Retorna las estadísticas en tiempo real sumando los datos de
    TODAS las bases de datos SQLite de los distintos proveedores.
    """
    local_now = datetime.now().astimezone()
    today_start_local = datetime.combine(local_now.date(), datetime.min.time()).replace(tzinfo=local_now.tzinfo)
    today_start = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    
    total_pending = 0
    total_sent = 0
    total_failed = 0
    total_retries = 0
    recent_events_global = []
    
    throughput_per_provider = {}
    provider_tz_offsets = {}
    thirty_secs_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)

    # Obtener proveedores directamente desde la BD de configuración
    config_db = get_session("system_config", "global")
    try:
        providers = config_db.query(ProviderConfig).all()
        # Si aún no hay configuraciones (primer arranque), usar los predeterminados
        if not providers:
            providers = [
                ProviderConfig(provider_name="schmitz", env="prod"),
                ProviderConfig(provider_name="schmitz", env="test")
            ]
    finally:
        config_db.close()

    for p in providers:
        provider_name = p.provider_name
        provider_env = p.env
        
        # Filtrar si se solicitó un proveedor específico
        if provider_filter and provider_filter.lower() != 'all' and provider_name.lower() != provider_filter.lower():
            continue
            
        # Extraer offset horario del proveedor
        tz_offset = 0
        try:
            if p.enrichment_config:
                enrich_data = p.enrichment_config if isinstance(p.enrichment_config, dict) else json.loads(p.enrichment_config)
                tz_offset = int(enrich_data.get('timezone_offset', 0))
        except:
            pass
        provider_tz_offsets[f"{provider_name}_{provider_env}"] = tz_offset
            
        db = get_session(provider_name, provider_env)
        try:
            # DEBT-05: Consolidar las 5 consultas en una sola
            stats = db.query(
                func.sum(case((NormalizedRCEvent.status == "pending", 1), else_=0)).label("pending"),
                func.sum(case((
                    (NormalizedRCEvent.status == "pending") & (NormalizedRCEvent.retry_count > 0), 1
                ), else_=0)).label("retries"),
                func.sum(case((
                    (NormalizedRCEvent.status == "sent") & (NormalizedRCEvent.created_at >= today_start), 1
                ), else_=0)).label("sent"),
                func.sum(case((
                    (NormalizedRCEvent.status == "failed") & (NormalizedRCEvent.created_at >= today_start), 1
                ), else_=0)).label("failed"),
                func.sum(case((
                    NormalizedRCEvent.created_at >= thirty_secs_ago, 1
                ), else_=0)).label("throughput")
            ).first()

            total_pending += int(stats.pending or 0)
            total_retries += int(stats.retries or 0)
            total_sent += int(stats.sent or 0)
            total_failed += int(stats.failed or 0)
            throughput_count = int(stats.throughput or 0)
            throughput_per_provider[f"{provider_name}_{provider_env}"] = throughput_count

            # Base query
            query = db.query(NormalizedRCEvent)
            
            if status_filter and status_filter != 'all':
                query = query.filter(NormalizedRCEvent.status == status_filter)

            # Obtener los 200 más recientes de esta BD particular
            recent = query.order_by(
                NormalizedRCEvent.id.desc()
            ).limit(200).all()
            
            for r in recent:
                # Inyectar dinámicamente estos atributos para la lectura posterior
                r.provider_name = provider_name
                r.env = provider_env
                
            recent_events_global.extend(recent)
        finally:
            db.close()

    # Ordenar los recientes de todas las BDs y quedarnos con los 200 últimos absolutos
    recent_events_global.sort(key=lambda x: x.updated_at or x.created_at, reverse=True)
    recent_events_global = recent_events_global[:200]

    total_latency_seconds = 0
    latency_samples = 0
    total_rc_latency_seconds = 0
    rc_latency_samples = 0
    recent_list = []
    
    for ev in recent_events_global:
        # Tiempos de inicio de envío y recepción a RC
        time_sent_dt = ev.updated_at
        time_received_rc_dt = ev.updated_at
        
        rc_latency_val = getattr(ev, 'rc_latency_sec', None)
        if ev.status in ('sent', 'failed') and ev.updated_at and rc_latency_val is not None:
            # El envío comenzó rc_latency_val segundos antes de completarse (updated_at)
            time_sent_dt = ev.updated_at - timedelta(seconds=rc_latency_val)
            if time_sent_dt < ev.created_at:
                time_sent_dt = ev.created_at
            
        latency_sec = None
        if ev.status in ('sent', 'failed') and time_sent_dt and ev.created_at:
            latency_sec = max(0.0, (time_sent_dt - ev.created_at).total_seconds())
            # Promediar solo si no hubo reintentos (happy path real) y no es un outlier (> 5 min)
            if getattr(ev, 'retry_count', 0) == 0 and latency_sec <= 300.0:
                total_latency_seconds += latency_sec
                latency_samples += 1
            
        if ev.status in ('sent', 'failed') and rc_latency_val is not None:
            total_rc_latency_seconds += rc_latency_val
            rc_latency_samples += 1

        transmission_latency_sec = None
        if ev.date and ev.created_at:
            created_naive = ev.created_at.replace(tzinfo=None)
            transmission_latency_sec = max(0.0, round((created_naive - ev.date).total_seconds(), 2))
            
        # Determinar reintentos directamente desde las columnas de base de datos
        retry_count = ev.retry_count or 0
        next_retry_in_sec = 0
        if ev.next_retry_at:
            now_naive = datetime.now()
            next_retry_naive = ev.next_retry_at.replace(tzinfo=None)
            if next_retry_naive > now_naive:
                next_retry_in_sec = max(0, int((next_retry_naive - now_naive).total_seconds()))
        # Recuperar offset del proveedor
        tz_offset = provider_tz_offsets.get(f"{getattr(ev, 'provider_name', '')}_{getattr(ev, 'env', '')}", 0)
        
        # Calcular fechas locales compensadas
        time_received_local = ev.created_at + timedelta(hours=tz_offset)
        device_date_local = ev.date + timedelta(hours=tz_offset) if getattr(ev, 'date') and ev.date else None
            
        recent_list.append({
            "id": ev.id,
            "chassis": ev.chassis_number,
            "status": ev.status,
            "time": (ev.updated_at or ev.created_at).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (UTC)",
            "time_received": time_received_local.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + (" (Local)" if tz_offset != 0 else " (UTC)"),
            "time_sent": (time_sent_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (UTC)") if ev.status in ('sent', 'failed') and time_sent_dt else "Procesando" if ev.status == 'processing' else "Pendiente" if ev.status == 'pending' else "Fallido",
            "time_received_rc": (time_received_rc_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (UTC)") if ev.status in ('sent', 'failed') and time_received_rc_dt else "Procesando" if ev.status == 'processing' else "Pendiente" if ev.status == 'pending' else "Fallido",
            "latency_sec": round(latency_sec, 3) if latency_sec is not None else None,
            "rc_latency_sec": round(rc_latency_val, 3) if rc_latency_val is not None else None,
            "transmission_latency_sec": transmission_latency_sec,
            "rc_response": getattr(ev, 'rc_response', ""),
            "provider": getattr(ev, 'provider_name', "N/A").upper(),
            "env": getattr(ev, 'env', "N/A").upper(),
            "device_date": device_date_local.strftime("%Y-%m-%d %H:%M:%S") + (" (Local)" if tz_offset != 0 else " (UTC)") if device_date_local else "N/A",
            "speed": getattr(ev, 'speed', 0),
            "coords": f"{ev.latitude}, {ev.longitude}" if getattr(ev, 'latitude') and ev.latitude else "Sin GPS",
            "ignition": "ON" if getattr(ev, 'ignition') else "OFF",
            "code": getattr(ev, 'code', "N/A"),
            "course": getattr(ev, 'course', None),
            "altitude": getattr(ev, 'altitude', None),
            "temperature": getattr(ev, 'temperature', None),
            "battery": getattr(ev, 'battery', None),
            "odometer": getattr(ev, 'odometer', None),
            "humidity": getattr(ev, 'humidity', None),
            "shipment": getattr(ev, 'shipment', None),
            "serial": getattr(ev, 'serial_number', None),
            "job_id": getattr(ev, 'job_id', None),
            "retry_count": retry_count,
            "next_retry_in_sec": next_retry_in_sec,
            
            # Exportación estructurada idéntica a Recurso Confiable
            "rc_format": {
                "asset": ev.chassis_number,
                "altitude": getattr(ev, 'altitude', 0) or 0,
                "battery": getattr(ev, 'battery', 0) or 0,
                "code": getattr(ev, 'code', "1") or "1",
                "customer": {"id": "", "name": ""},
                "date": ev.date.strftime("%Y-%m-%dT%H:%M:%SZ") if getattr(ev, 'date') and ev.date else "",
                "direction": getattr(ev, 'course', 0) or 0,
                "humidity": getattr(ev, 'humidity', 0) or 0,
                "ignition": "true" if getattr(ev, 'ignition') else "false",
                "latitude": getattr(ev, 'latitude', 0) or 0,
                "longitude": getattr(ev, 'longitude', 0) or 0,
                "odometer": getattr(ev, 'odometer', 0) or 0,
                "serialNumber": getattr(ev, 'serial_number', "") or "",
                "shipment": getattr(ev, 'shipment', "") or "",
                "speed": getattr(ev, 'speed', 0) or 0,
                "temperature": getattr(ev, 'temperature', 0) or 0,
                "vehicleType": getattr(ev, 'vehicle_type', "") or "",
                "vehicleBrand": getattr(ev, 'vehicle_brand', "") or "",
                "vehicleModel": getattr(ev, 'vehicle_model', "") or ""
            },
            "raw_data": ev.raw_data
        })

    avg_latency = round(total_latency_seconds / latency_samples, 3) if latency_samples > 0 else 0
    avg_rc_latency = round(total_rc_latency_seconds / rc_latency_samples, 3) if rc_latency_samples > 0 else 0

    push_stats = get_push_stats(provider_filter)
    # Incluir stats por proveedor para filtrado client-side
    push_per_provider = {
        k: get_push_stats(k) for k in push_latency_store
    }

    return {
        "pending": total_pending,
        "sent": total_sent,
        "failed": total_failed,
        "retries": total_retries,
        "avg_latency_sec": avg_latency,
        "avg_rc_latency_sec": avg_rc_latency,
        "push_stats":              push_stats,
        "push_per_provider":       push_per_provider,
        "push_sla_target_ms":      PUSH_SLA_MS,
        "recent": recent_list,
        "throughput": throughput_per_provider,
        "all_providers": list(set([p.provider_name for p in providers]))
    }

@router.get("/api/config/providers")
async def get_providers(_: None = Depends(verify_dashboard_auth)):
    config_db = get_session("system_config", "global")
    try:
        providers = config_db.query(ProviderConfig).all()
        return [{"id": p.id, "provider_name": p.provider_name, "env": p.env} for p in providers]
    finally:
        config_db.close()


@router.get("/api/stats")
async def get_stats(
    status_filter: str = Query(None, alias="status"),
    provider_filter: str = Query(None, alias="provider"),
    _: None = Depends(verify_dashboard_auth)
):
    return await get_stats_data(status_filter, provider_filter)

_sse_clients: list[asyncio.Queue] = []

async def broadcast_loop():
    """Corre en background: 1 query/2s → push a todos los clientes SSE."""
    while True:
        await asyncio.sleep(2)
        if not _sse_clients:
            continue
        try:
            data = await get_stats_data()
            payload = f"data: {json.dumps(data)}\n\n"
            for q in _sse_clients:
                await q.put(payload)
        except Exception as e:
            import traceback
            logger.error(f"Error in broadcast_loop: {e}\n{traceback.format_exc()}")

@router.get("/api/stats/stream")
async def stats_stream(request: Request, _=Depends(verify_dashboard_auth)):
    q = asyncio.Queue()
    _sse_clients.append(q)
    async def event_gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = await asyncio.wait_for(q.get(), timeout=30)
                yield payload
        except Exception:
            pass
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)
    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

@router.post("/api/config/providers")
async def create_provider(payload: dict, _: None = Depends(verify_dashboard_auth)):
    provider_name = payload.get("provider_name")
    if not provider_name:
        return {"status": "error", "message": "Falta el nombre del proveedor."}
        
    config_db = get_session("system_config", "global")
    try:
        provider_name = provider_name.lower().strip()
        # Verificar si ya existe en algun entorno
        exists = config_db.query(ProviderConfig).filter(
            ProviderConfig.provider_name == provider_name
        ).first()
        
        if exists:
            return {"status": "error", "message": f"El proveedor {provider_name} ya existe."}
            
        new_prod = ProviderConfig(
            provider_name=provider_name,
            env="prod",
            is_active=False,
            queue_backend="sqlite",
            mapping_schema={}
        )
        new_test = ProviderConfig(
            provider_name=provider_name,
            env="test",
            is_active=True,
            queue_backend="sqlite",
            mapping_schema={}
        )
        
        config_db.add_all([new_prod, new_test])
        config_db.commit()
        return {"status": "success", "message": "Proveedor creado exitosamente en prod y test."}
    except Exception as e:
        config_db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        config_db.close()

@router.post("/api/config/{provider_name}/{env}/mapping")
async def save_mapping(provider_name: str, env: str, payload: dict, _: None = Depends(verify_dashboard_auth)):
    config_db = get_session("system_config", "global")
    try:
        config = config_db.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider_name),
            ProviderConfig.env == env
        ).first()
        if not config:
            return {"status": "error", "message": "Provider not found"}
            
        # Compatibilidad: si el payload tiene la llave 'mapping', extraerla, si no, asumir que todo es mapping
        if 'mapping' in payload:
            config.mapping_schema = payload.get('mapping', {})
            if 'fetch' in payload:
                config.fetch_config = payload.get('fetch', {})
        else:
            config.mapping_schema = payload
            
        config_db.commit()
        return {"status": "success"}
    except Exception as e:
        config_db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        config_db.close()
        
@router.get("/api/config/{provider_name}/{env}/mapping")
async def get_mapping(provider_name: str, env: str, _: None = Depends(verify_dashboard_auth)):
    config_db = get_session("system_config", "global")
    try:
        config = config_db.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider_name),
            ProviderConfig.env == env
        ).first()
        if not config:
            return {}
        return {
            "mapping": config.mapping_schema or {},
            "fetch": config.fetch_config or {}
        }
    finally:
        config_db.close()

@router.post("/api/config/{provider_name}/{env}/enrichment")
async def save_enrichment(provider_name: str, env: str, payload: dict, _: None = Depends(verify_dashboard_auth)):
    config_db = get_session("system_config", "global")
    try:
        config = config_db.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider_name),
            ProviderConfig.env == env
        ).first()
        if not config:
            return {"status": "error", "message": "Provider not found"}
            
        config.enrichment_config = payload
        config_db.commit()
        return {"status": "success"}
    except Exception as e:
        config_db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        config_db.close()
        
@router.get("/api/config/{provider_name}/{env}/enrichment")
async def get_enrichment(provider_name: str, env: str, _: None = Depends(verify_dashboard_auth)):
    config_db = get_session("system_config", "global")
    try:
        config = config_db.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider_name),
            ProviderConfig.env == env
        ).first()
        if not config:
            return {}
        return config.enrichment_config or {}
    finally:
        config_db.close()

@router.get("/api/config")
async def get_all_configs(_: None = Depends(verify_dashboard_auth)):
    db = get_session("system_config", "global")
    try:
        configs = db.query(ProviderConfig).all()
        # Inicializar si está vacío (auto-poblado en el primer inicio)
        if not configs:
            c1 = ProviderConfig(provider_name="schmitz", env="prod")
            c2 = ProviderConfig(provider_name="schmitz", env="test")
            db.add_all([c1, c2])
            db.commit()
            configs = db.query(ProviderConfig).all()
            
        return [{
            "id": c.id,
            "provider_name": c.provider_name.upper(),
            "env": c.env.upper(),
            "is_active": c.is_active,
            "rc_user": c.rc_user,
            "rc_password": "••••••••" if c.rc_password else "",
            "purge_interval_min": c.purge_interval_min,
            "run_interval_sec": c.run_interval_sec,
            "queue_backend": c.queue_backend if hasattr(c, 'queue_backend') and c.queue_backend else "sqlite"
        } for c in configs]
    finally:
        db.close()

@router.post("/api/config")
async def update_configs(updates: List[ConfigUpdate], _: None = Depends(verify_dashboard_auth)):
    db = get_session("system_config", "global")
    try:
        for u in updates:
            conf = db.query(ProviderConfig).filter(ProviderConfig.id == u.id).first()
            if conf:
                conf.is_active = u.is_active
                conf.rc_user = u.rc_user
                if u.rc_password and u.rc_password != "••••••••":
                    conf.rc_password = u.rc_password
                conf.purge_interval_min = u.purge_interval_min
                conf.run_interval_sec = u.run_interval_sec
                conf.queue_backend = u.queue_backend.lower()
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()

@router.get("/api/logs")
async def get_audit_logs(_: None = Depends(verify_dashboard_auth)):
    """Devuelve los últimos 50 registros de auditoría de los archivos .jsonl"""
    audit_dir = "audit"
    if not os.path.exists(audit_dir):
        return []
    
    # Busca en todos los subdirectorios
    files = glob.glob(f"{audit_dir}/**/*.jsonl", recursive=True)
    all_lines = []
    
    for f in files:
        provider_name = os.path.basename(os.path.dirname(f))
        with open(f, 'r', encoding='utf-8') as file:
            for line in file:
                try:
                    data = json.loads(line)
                    # Compatibilidad con formato nuevo y viejo
                    if "payload" in data and "timestamp" in data:
                        all_lines.append(data)
                    else:
                        all_lines.append({
                            "timestamp": "Formato Antiguo",
                            "provider": provider_name,
                            "payload": data
                        })
                except Exception:
                    pass

    # Ordenar por timestamp descendente
    all_lines.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return all_lines[:50]

@router.delete("/api/logs")
async def clear_audit_logs(_: None = Depends(verify_dashboard_auth)):
    """Borra todos los archivos de auditoría jsonl"""
    audit_dir = "audit"
    if not os.path.exists(audit_dir):
        return {"status": "ok"}
    
    files = glob.glob(f"{audit_dir}/**/*.jsonl", recursive=True)
    for f in files:
        try:
            os.remove(f)
        except Exception:
            pass
    return {"status": "ok"}

@router.get("/api/history")
async def get_daily_history(_: None = Depends(verify_dashboard_auth)):
    """Devuelve los registros históricos de estadísticas diarias consolidando los últimos 30 días."""
    db = get_session("system_config", "global")
    try:
        stats = db.query(DailyStat).order_by(DailyStat.date.desc()).limit(200).all()
        return [{
            "id": s.id,
            "date": s.date.strftime("%Y-%m-%d") if s.date else "",
            "provider": s.provider.upper(),
            "env": s.env.upper(),
            "sent_count": s.sent_count,
            "failed_count": s.failed_count,
            "avg_transmission_latency_sec": round(max(0.0, s.avg_transmission_latency_sec), 2) if s.avg_transmission_latency_sec is not None and s.avg_transmission_latency_sec >= 0 else None,
            "avg_hub_latency_sec": round(max(0.0, s.avg_hub_latency_sec), 2) if s.avg_hub_latency_sec is not None and s.avg_hub_latency_sec >= 0 else None,
            "avg_rc_latency_sec": round(s.avg_rc_latency_sec, 2) if s.avg_rc_latency_sec is not None else None,
            "avg_push_latency_ms": round(s.avg_push_latency_ms, 3) if s.avg_push_latency_ms is not None else None
        } for s in stats]
    finally:
        db.close()

@router.get("/api/db-viewer/databases")
async def get_databases(_: None = Depends(verify_dashboard_auth)):
    """Lista todas las bases de datos SQLite en el directorio db."""
    db_dir = "./db"
    if not os.path.exists(db_dir):
        return []
    files = glob.glob(f"{db_dir}/*.db")
    return [{"name": os.path.basename(f)} for f in files]

@router.get("/api/db-viewer/tables")
async def get_tables(db_name: str = Query(...), _: None = Depends(verify_dashboard_auth)):
    """Lista las tablas de una base de datos específica."""
    import sqlite3
    # Prevención básica de path traversal
    safe_db_name = os.path.basename(db_name)
    db_path = f"./db/{safe_db_name}"
    if not os.path.exists(db_path):
        return []
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        return {"tables": tables}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()

@router.get("/api/db-viewer/query")
async def execute_query(db_name: str = Query(...), table: str = Query(...), limit: int = 50, offset: int = 0, _: None = Depends(verify_dashboard_auth)):
    """Retorna los datos y las columnas de una tabla seleccionada. Incluye rowid para edición."""
    import sqlite3
    safe_db_name = os.path.basename(db_name)
    db_path = f"./db/{safe_db_name}"
    if not os.path.exists(db_path):
        return {"error": "Base de datos no encontrada"}
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        import re
        if not re.match(r'^[a-zA-Z0-9_]+$', table):
            return {"error": "Nombre de tabla inválido"}
        
        # Incluir rowid como identificador único universal de SQLite (funciona aunque no haya PK)
        cursor.execute(f"SELECT rowid, * FROM {table} LIMIT ? OFFSET ?", (limit, offset))
        rows = cursor.fetchall()
        
        # Obtener los nombres de las columnas (prefijado con __rowid__ para el frontend)
        cursor.execute(f"PRAGMA table_info({table})")
        columns = ["__rowid__"] + [col[1] for col in cursor.fetchall()]
        
        # Obtener conteo total
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        total = cursor.fetchone()[0]
        
        return {
            "columns": columns,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "editable": table in EDITABLE_TABLES  # El frontend muestra el modo edición solo si es True
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()


class CellUpdateRequest(BaseModel):
    db_name: str
    table: str
    rowid: int
    column_name: str
    new_value: Optional[str]
    password: str  # Revalidación de DASHBOARD_PASSWORD — seguridad real, no cosmética


@router.post("/api/db-viewer/update_cell")
async def update_cell(body: CellUpdateRequest, _: None = Depends(verify_dashboard_auth)):
    """
    Edita una celda específica de una tabla permitida.
    Requiere revalidar DASHBOARD_PASSWORD para confirmar la operación.
    Las tablas operativas (normalized_rc_events, etc.) son de SOLO LECTURA y siempre serán rechazadas.
    """
    import sqlite3, re

    # Ajuste 1 (Claude): Validar con la contraseña real del .env, no con un PIN cosmético
    correct_pass = os.getenv("DASHBOARD_PASSWORD", "")
    if not secrets.compare_digest(body.password.encode(), correct_pass.encode()):
        raise HTTPException(status_code=403, detail="Contraseña de administrador incorrecta")

    # Ajuste 2 (Claude): Whitelist estricta — rechazo explícito de tablas operativas
    if body.table not in EDITABLE_TABLES:
        raise HTTPException(
            status_code=403,
            detail=f"La tabla '{body.table}' es de solo lectura. Edición no permitida."
        )

    # Validar nombres para prevenir SQL injection
    if not re.match(r'^[a-zA-Z0-9_]+$', body.table):
        raise HTTPException(status_code=400, detail="Nombre de tabla inválido")
    if not re.match(r'^[a-zA-Z0-9_]+$', body.column_name):
        raise HTTPException(status_code=400, detail="Nombre de columna inválido")
    if body.column_name == "__rowid__":
        raise HTTPException(status_code=400, detail="El rowid no es editable")

    safe_db_name = os.path.basename(body.db_name)
    db_path = f"./db/{safe_db_name}"
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="Base de datos no encontrada")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # UPDATE completamente parametrizado — sin f-strings en los valores
        cursor.execute(
            f"UPDATE {body.table} SET {body.column_name} = ? WHERE rowid = ?",
            (body.new_value, body.rowid)
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Fila no encontrada")
        return {"ok": True, "updated_rows": cursor.rowcount}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'conn' in locals():
            conn.close()
