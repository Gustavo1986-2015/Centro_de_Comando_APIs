from fastapi import APIRouter, Request, Query, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import asyncio
import json
import os
import glob
import sqlite3
import re
import logging
import traceback

logger = logging.getLogger(__name__)

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.models.config_models import ProviderConfig, DailyStat, SystemSettings
from app.worker.processor import _rc_circuit_breaker
from app.core import config_cache
from app.core.auditor import log_admin_action
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, case

from collections import deque
import time as _time



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


from app.core.auth import verify_dashboard_auth, security

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="frontend/templates")



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
        except Exception as e:
            logger.warning(f"Error al parsear JSON: {e}")
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
        "all_providers": list(set([p.provider_name for p in providers])),
        "rc_circuit_state": _rc_circuit_breaker.state,
        "rc_failure_count": _rc_circuit_breaker._failure_count
    }


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
        except Exception as e:
            logger.warning(f"Excepción capturada en dashboard: {e}")
            pass
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)
    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


