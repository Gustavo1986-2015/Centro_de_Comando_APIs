from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, date
import os
import glob
import json

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.models.config_models import ProviderConfig
from pydantic import BaseModel
from typing import List

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="app/templates")

class ConfigUpdate(BaseModel):
    id: int
    is_active: bool
    rc_user: str
    rc_password: str
    purge_interval_min: int
    run_interval_sec: int

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """Renderiza el Centro de Comando en Vivo."""
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/api/stats")
async def get_stats(
    status_filter: str = Query(None, alias="status"),
    provider_filter: str = Query(None, alias="provider")
):
    """
    Retorna las estadísticas en tiempo real sumando los datos de
    TODAS las bases de datos SQLite de los distintos proveedores.
    """
    today_start = datetime.combine(date.today(), datetime.min.time())
    
    total_pending = 0
    total_sent = 0
    total_failed = 0
    recent_events_global = []

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
            
        db = get_session(provider_name, provider_env)
        try:
            total_pending += db.query(NormalizedRCEvent).filter(NormalizedRCEvent.status == "pending").count()
            
            total_sent += db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.status == "sent",
                NormalizedRCEvent.created_at >= today_start
            ).count()
            
            total_failed += db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.status == "failed",
                NormalizedRCEvent.created_at >= today_start
            ).count()

            # Base query
            query = db.query(NormalizedRCEvent)
            
            if status_filter and status_filter != 'all':
                query = query.filter(NormalizedRCEvent.status == status_filter)

            # Obtener los 200 más recientes de esta BD particular
            recent = query.order_by(
                NormalizedRCEvent.updated_at.desc()
            ).limit(200).all()
            
            for r in recent:
                # Inyectar dinámicamente estos atributos para la lectura posterior
                r.provider_name = provider_name
                r.env = provider_env
                
            recent_events_global.extend(recent)
        finally:
            db.close()

    # Ordenar los recientes de todas las BDs y quedarnos con los 50 últimos absolutos
    recent_events_global.sort(key=lambda x: x.updated_at or x.created_at, reverse=True)
    recent_events_global = recent_events_global[:200]

    total_latency_seconds = 0
    latency_samples = 0
    recent_list = []
    
    for ev in recent_events_global:
        latency_sec = None
        if ev.status == 'sent' and ev.updated_at and ev.created_at:
            latency_sec = (ev.updated_at - ev.created_at).total_seconds()
            total_latency_seconds += latency_sec
            latency_samples += 1
            
        recent_list.append({
            "chassis": ev.chassis_number,
            "status": ev.status,
            "time": ev.updated_at.strftime("%Y-%m-%d %H:%M:%S (UTC)") if ev.updated_at else ev.created_at.strftime("%Y-%m-%d %H:%M:%S (UTC)"),
            "time_received": ev.created_at.strftime("%Y-%m-%d %H:%M:%S (UTC)"),
            "time_sent": ev.updated_at.strftime("%Y-%m-%d %H:%M:%S (UTC)") if ev.updated_at else "Pendiente",
            "latency_sec": round(latency_sec, 2) if latency_sec is not None else None,
            "rc_response": getattr(ev, 'rc_response', ""),
            "provider": getattr(ev, 'provider_name', "N/A").upper(),
            "env": getattr(ev, 'env', "N/A").upper(),
            "device_date": ev.date.strftime("%Y-%m-%d %H:%M:%S") + " (UTC)" if getattr(ev, 'date') and ev.date else "N/A",
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
            }
        })

    avg_latency = round(total_latency_seconds / latency_samples, 2) if latency_samples > 0 else 0

    return {
        "pending": total_pending,
        "sent": total_sent,
        "failed": total_failed,
        "avg_latency_sec": avg_latency,
        "recent": recent_list
    }

@router.get("/api/config")
async def get_all_configs():
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
            "rc_password": c.rc_password,
            "purge_interval_min": c.purge_interval_min,
            "run_interval_sec": c.run_interval_sec
        } for c in configs]
    finally:
        db.close()

@router.post("/api/config")
async def update_configs(updates: List[ConfigUpdate]):
    db = get_session("system_config", "global")
    try:
        for u in updates:
            conf = db.query(ProviderConfig).filter(ProviderConfig.id == u.id).first()
            if conf:
                conf.is_active = u.is_active
                conf.rc_user = u.rc_user
                conf.rc_password = u.rc_password
                conf.purge_interval_min = u.purge_interval_min
                conf.run_interval_sec = u.run_interval_sec
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()

@router.get("/api/logs")
async def get_audit_logs():
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
async def clear_audit_logs():
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

