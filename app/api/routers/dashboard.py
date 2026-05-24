from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date

from app.database import get_db
from app.models.db_models import NormalizedRCEvent

router = APIRouter(tags=["Dashboard"])
templates = Jinja2Templates(directory="app/templates")

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """Renderiza el Centro de Comando en Vivo."""
    return templates.TemplateResponse("index.html", {"request": request})

@router.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    """
    Retorna las estadísticas en tiempo real para el Dashboard.
    Cuenta pendientes, enviados (hoy) y fallidos (hoy).
    """
    today_start = datetime.combine(date.today(), datetime.min.time())
    
    # Eventos en cola (pending) sin importar la fecha
    pending_count = db.query(NormalizedRCEvent).filter(
        NormalizedRCEvent.status == "pending"
    ).count()

    # Eventos enviados hoy
    sent_count = db.query(NormalizedRCEvent).filter(
        NormalizedRCEvent.status == "sent",
        NormalizedRCEvent.created_at >= today_start
    ).count()

    # Eventos fallidos hoy
    failed_count = db.query(NormalizedRCEvent).filter(
        NormalizedRCEvent.status == "failed",
        NormalizedRCEvent.created_at >= today_start
    ).count()

    # Últimos 5 eventos procesados
    recent_events = db.query(NormalizedRCEvent).order_by(
        NormalizedRCEvent.updated_at.desc()
    ).limit(5).all()

    recent_list = []
    for ev in recent_events:
        recent_list.append({
            "chassis": ev.chassis_number,
            "status": ev.status,
            "time": ev.updated_at.strftime("%H:%M:%S") if ev.updated_at else ""
        })

    return {
        "pending": pending_count,
        "sent": sent_count,
        "failed": failed_count,
        "recent": recent_list
    }
