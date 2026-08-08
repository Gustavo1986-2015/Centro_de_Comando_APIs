import os
import re
from fastapi import APIRouter, Request, Query, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.models.config_models import ProviderConfig
from app.worker.processor import _rc_circuit_breaker
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


from app.core.auth import verify_dashboard_auth
from app.core.provider_health import get_health_snapshot
from app.core.resync import request_resync
from app.core.log_stream import read_recent, tail_log
from app.core.logging_config import get_current_levels, set_runtime_level
from app.version import __version__, static_version

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="frontend/templates")



@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request, _: None = Depends(verify_dashboard_auth)):
    """Renderiza el Centro de Comando en Vivo."""
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            # Cache busting derivado del contenido: el navegador descarga el
            # archivo nuevo solo cuando cambió de verdad.
            "css_v": static_version("dashboard.css"),
            "js_v":  static_version("dashboard.js"),
            "app_version": __version__,
        },
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

def _fetch_providers_sync() -> list:
    """Carga proveedores desde la BD de configuración. Sync, para ejecutar en ThreadPool."""
    config_db = get_session("system_config", "global")
    try:
        providers = config_db.query(ProviderConfig).all()
        if not providers:
            return [
                ProviderConfig(provider_name="schmitz", env="prod"),
                ProviderConfig(provider_name="schmitz", env="test")
            ]
        return providers
    finally:
        config_db.close()

def _fetch_events_for_provider_sync(provider_name, provider_env, status_filter, today_start, thirty_secs_ago):
    """Queries SQLite de un proveedor específico. Sync, para ejecutar en ThreadPool."""
    db = get_session(provider_name, provider_env)
    try:
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

        query = db.query(NormalizedRCEvent)
        if status_filter and status_filter != 'all':
            query = query.filter(NormalizedRCEvent.status == status_filter)
        recent = query.order_by(NormalizedRCEvent.id.desc()).limit(200).all()
        for r in recent:
            r.provider_name = provider_name
            r.env = provider_env
        return stats, recent
    finally:
        db.close()

def _get_mock_providers() -> list[str]:
    """
    Integraciones con el modo simulado activo.

    En ese modo el sistema genera job_ids falsos y marca los eventos como
    enviados sin llamar a Recurso Confiable. El dashboard debe advertirlo de
    forma permanente y visible: un operador que vea "ENVIADO" tiene que poder
    saber si ese envío fue real.
    """
    try:
        from app.models.config_models import ProviderConfig
        db = get_session("system_config", "global")
        try:
            filas = db.query(ProviderConfig).filter_by(use_mock=True).all()
            return [f"{c.provider_name}/{c.env}" for c in filas]
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"No se pudo determinar el estado de modo simulado: {e}")
        return []


async def get_stats_data(
    status_filter: str = None,
    provider_filter: str = None
):
    """
    Retorna las estadísticas en tiempo real sumando los datos de
    TODAS las bases de datos SQLite de los distintos proveedores.
    Las operaciones bloqueantes de SQLite se despachan al ThreadPool vía asyncio.to_thread.
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

    # Obtener proveedores desde ThreadPool (operación bloqueante)
    providers = await asyncio.to_thread(_fetch_providers_sync)

    for p in providers:
        provider_name = p.provider_name
        provider_env = p.env
        
        if provider_filter and provider_filter.lower() != 'all' and provider_name.lower() != provider_filter.lower():
            continue
            
        tz_offset = 0
        try:
            if p.enrichment_config:
                enrich_data = p.enrichment_config if isinstance(p.enrichment_config, dict) else json.loads(p.enrichment_config)
                tz_offset = int(enrich_data.get('timezone_offset', 0))
        except Exception as e:
            logger.warning(f"Error al parsear JSON: {e}")
        provider_tz_offsets[f"{provider_name}_{provider_env}"] = tz_offset

        # Query por proveedor en ThreadPool (operación bloqueante)
        stats, recent = await asyncio.to_thread(
            _fetch_events_for_provider_sync,
            provider_name, provider_env, status_filter, today_start, thirty_secs_ago
        )

        total_pending += int(stats.pending or 0)
        total_retries += int(stats.retries or 0)
        total_sent += int(stats.sent or 0)
        total_failed += int(stats.failed or 0)
        throughput_count = int(stats.throughput or 0)
        throughput_per_provider[f"{provider_name}_{provider_env}"] = throughput_count
        recent_events_global.extend(recent)

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
        "provider_health":         get_health_snapshot(),
        # Proveedores en modo simulado: el frontend muestra un banner permanente.
        # Sin esto, el dashboard informa "ENVIADO" para eventos que nunca salieron.
        "mock_providers":          _get_mock_providers(),
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
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)
    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})




@router.post("/api/integrations/{provider}/{env}/resync")
async def force_resync(provider: str, env: str, _=Depends(verify_dashboard_auth)):
    """
    Fuerza la resincronización inmediata del diccionario de una integración.

    Evita tener que esperar el intervalo de reintento (o reiniciar el servicio)
    después de corregir credenciales o URLs desde el panel de configuración.
    Es transversal: sirve para cualquier proveedor con diccionario habilitado.
    """
    ok = request_resync(provider, env)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"No hay un worker activo para {provider}/{env}. "
                   f"Verificá que la integración esté habilitada."
        )
    return {"status": "ok", "message": f"Resincronización solicitada para {provider}/{env}."}


# ─── Consola de logs en vivo ────────────────────────────────────────────────
# Evita tener que entrar por SSH al servidor para diagnosticar. Los valores
# sensibles (tokens, firmas, contraseñas) se enmascaran en log_stream antes de
# salir del backend.

@router.get("/api/logs/recent")
async def logs_recent(
    n: int = Query(200, ge=1, le=2000),
    level: str = Query(None, description="Nivel mínimo: DEBUG, INFO, WARNING, ERROR"),
    _=Depends(verify_dashboard_auth),
):
    """
    Últimas N líneas del log, opcionalmente filtradas por nivel mínimo.

    El filtro se aplica sobre el archivo completo: pedir los últimos errores
    debe encontrarlos aunque hayan ocurrido hace rato y ya no estén entre las
    últimas líneas escritas.
    """
    return {"logs": read_recent(n, min_level=level)}


@router.get("/api/logs/stream")
async def logs_stream(request: Request, _=Depends(verify_dashboard_auth)):
    """Stream SSE de las líneas nuevas del log a medida que se escriben."""

    async def event_generator():
        try:
            async for registro in tail_log():
                if await request.is_disconnected():
                    break
                yield f"data: {json.dumps(registro, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Mantenimiento de bases de datos ────────────────────────────────────────
# La cola SQLite crece durante el día y solo se purga en el intervalo
# configurado. Bajo carga sostenida (una prueba de certificación de 24h genera
# millones de eventos) conviene poder ver el tamaño real y forzar la purga sin
# esperar al próximo ciclo.

@router.get("/api/maintenance/db-stats")
async def db_stats(_=Depends(verify_dashboard_auth)):
    """Tamaño en disco y conteo de eventos por integración."""
    import glob
    from app.models.db_models import NormalizedRCEvent

    resultado = []
    for ruta in sorted(glob.glob("./db/*/*.db")):
        rel = os.path.relpath(ruta, "./db").replace("\\", "/")
        partes = rel.split("/")
        if len(partes) != 2:
            continue
        provider, archivo = partes[0], partes[1]
        env = archivo.replace(".db", "")

        # Los archivos -wal y -shm también ocupan disco y pueden ser grandes
        tamano = 0
        for sufijo in ("", "-wal", "-shm"):
            try:
                tamano += os.path.getsize(ruta + sufijo)
            except OSError:
                pass

        conteos = {"pending": 0, "processing": 0, "sent": 0, "failed": 0}
        total = 0
        try:
            db = get_session(provider, env)
            try:
                filas = (
                    db.query(NormalizedRCEvent.status, func.count(NormalizedRCEvent.id))
                    .group_by(NormalizedRCEvent.status)
                    .all()
                )
                for estado, n in filas:
                    conteos[estado] = n
                    total += n
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"No se pudo contar eventos de {rel}: {e}")

        resultado.append({
            "provider": provider,
            "env": env,
            "path": rel,
            "size_mb": round(tamano / (1024 * 1024), 2),
            "total_events": total,
            "by_status": conteos,
            # Lo purgable es lo ya despachado: pendientes y en proceso no se tocan
            "purgeable": conteos["sent"] + conteos["failed"],
        })

    resultado.sort(key=lambda x: x["size_mb"], reverse=True)
    return {
        "databases": resultado,
        "total_mb": round(sum(d["size_mb"] for d in resultado), 2),
    }


@router.post("/api/maintenance/purge/{provider}/{env}")
async def purge_now(provider: str, env: str, _=Depends(verify_dashboard_auth)):
    """
    Fuerza la purga de una integración sin esperar el intervalo configurado.

    Respeta exactamente las mismas reglas que la purga automática: solo elimina
    eventos ya despachados (sent/failed) y respalda a JSONL antes de borrar.
    Los pendientes y los que están en proceso no se tocan nunca.
    """
    if not re.match(r"^[a-zA-Z0-9_]+$", provider) or not re.match(r"^[a-zA-Z0-9_]+$", env):
        raise HTTPException(status_code=400, detail="Proveedor o entorno inválido")

    from app.worker.processor import purge_provider_events

    ruta = os.path.join("db", provider, f"{env}.db")
    antes_mb = _tamano_db_mb(ruta)

    logger.info(f"Purga manual solicitada para {provider}/{env} desde el panel.")
    try:
        await purge_provider_events(provider, env)
    except Exception as e:
        logger.error(f"Error en purga manual de {provider}/{env}: {e}")
        raise HTTPException(status_code=500, detail=f"Error durante la purga: {e}")

    # SQLite no devuelve al sistema el espacio de las filas borradas: marca las
    # páginas como reutilizables y el archivo conserva su tamaño. Tras una purga
    # grande eso deja el disco ocupado sin motivo, y en una prueba de 24 horas
    # de carga sostenida esa diferencia son varios GB.
    try:
        await asyncio.to_thread(_vacuum_db, ruta, provider, env)
    except Exception as e:
        # El VACUUM es una optimización: si falla, la purga ya ocurrió igual.
        logger.warning(f"No se pudo compactar {provider}/{env}: {e}")

    despues_mb = _tamano_db_mb(ruta)
    return {
        "status": "ok",
        "message": f"Purga ejecutada para {provider}/{env}.",
        "size_before_mb": antes_mb,
        "size_after_mb": despues_mb,
        "freed_mb": round(max(antes_mb - despues_mb, 0), 2),
    }


def _tamano_db_mb(ruta: str) -> float:
    """Tamaño del archivo más sus auxiliares -wal y -shm."""
    total = 0
    for sufijo in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(ruta + sufijo)
        except OSError:
            pass
    return round(total / (1024 * 1024), 2)


def _vacuum_db(ruta: str, provider: str, env: str) -> float:
    """
    Compacta el archivo para devolver al sistema el espacio de las filas
    borradas. Bloquea la base mientras corre, por eso se ejecuta en un hilo
    aparte y solo tras una purga manual, nunca en el ciclo automático.
    """
    import sqlite3

    if not os.path.exists(ruta):
        return 0.0

    antes = _tamano_db_mb(ruta)
    conn = sqlite3.connect(ruta)
    try:
        # Con WAL activo, los cambios viven en el archivo -wal hasta que se
        # fusionan. Sin este checkpoint el VACUUM compacta el .db pero el -wal
        # queda intacto, y el espacio total en disco no baja (puede incluso
        # subir, porque el contenido termina duplicado en ambos archivos).
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    liberado = antes - _tamano_db_mb(ruta)
    if liberado > 0.01:
        logger.info(f"VACUUM en {provider}/{env}: {liberado:.2f} MB liberados en disco.")
    return round(max(liberado, 0), 2)


@router.get("/api/logs/level")
async def logs_level_get(_=Depends(verify_dashboard_auth)):
    """Nivel de logging vigente en el proceso."""
    return get_current_levels()


@router.post("/api/logs/level")
async def logs_level_set(
    level: str = Query(..., description="DEBUG, INFO, WARNING o ERROR"),
    libs: str = Query(None, description="Nivel de librerías de terceros"),
    _=Depends(verify_dashboard_auth),
):
    """
    Cambia el nivel de logging del proceso sin reiniciar el contenedor.

    Permite diagnosticar en producción sin acceso al servidor. El cambio no se
    persiste: al reiniciar vuelve a lo configurado en el entorno, de modo que un
    DEBUG olvidado no quede activo indefinidamente llenando el disco.
    """
    try:
        return set_runtime_level(level, libs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
