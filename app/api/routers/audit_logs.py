import os
import glob
import json
import logging
from fastapi import APIRouter, Depends
from fastapi.security import HTTPBasicCredentials

from app.core.auth import verify_dashboard_auth
from app.database import get_session
from app.models.config_models import DailyStat

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Audit Logs"])

@router.get("/api/logs")
def get_audit_logs(_: None = Depends(verify_dashboard_auth)):
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
                except Exception as e:
                    logger.warning(f"Excepción capturada en dashboard: {e}")
                    pass

    # Ordenar por timestamp descendente
    all_lines.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return all_lines[:50]

@router.delete("/api/logs")
def clear_audit_logs(_: None = Depends(verify_dashboard_auth)):
    """Borra todos los archivos de auditoría jsonl"""
    audit_dir = "audit"
    if not os.path.exists(audit_dir):
        return {"status": "ok"}
    
    files = glob.glob(f"{audit_dir}/**/*.jsonl", recursive=True)
    for f in files:
        try:
            os.remove(f)
        except Exception as e:
            logger.debug(f"No se pudo eliminar archivo: {e}")
            pass
    return {"status": "ok"}

@router.get("/api/history")
def get_daily_history(_: None = Depends(verify_dashboard_auth)):
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
