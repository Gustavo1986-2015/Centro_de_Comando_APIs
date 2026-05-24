from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, date

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.worker.processor import ACTIVE_PROVIDERS

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="app/templates")

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """Renderiza el Centro de Comando en Vivo."""
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/api/stats")
async def get_stats():
    """
    Retorna las estadísticas en tiempo real sumando los datos de
    TODAS las bases de datos SQLite de los distintos proveedores.
    """
    today_start = datetime.combine(date.today(), datetime.min.time())
    
    total_pending = 0
    total_sent = 0
    total_failed = 0
    recent_events_global = []

    for p in ACTIVE_PROVIDERS:
        provider_name = p["name"]
        provider_env = p["env"]
        
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

            # Obtener los 5 más recientes de esta BD particular
            recent = db.query(NormalizedRCEvent).order_by(
                NormalizedRCEvent.updated_at.desc()
            ).limit(5).all()
            
            recent_events_global.extend(recent)
        finally:
            db.close()

    # Ordenar los recientes de todas las BDs y quedarnos con los 5 últimos absolutos
    recent_events_global.sort(key=lambda x: x.updated_at or x.created_at, reverse=True)
    recent_events_global = recent_events_global[:5]

    recent_list = []
    for ev in recent_events_global:
        recent_list.append({
            "chassis": ev.chassis_number,
            "status": ev.status,
            "time": ev.updated_at.strftime("%H:%M:%S") if ev.updated_at else ""
        })

    return {
        "pending": total_pending,
        "sent": total_sent,
        "failed": total_failed,
        "recent": recent_list
    }
