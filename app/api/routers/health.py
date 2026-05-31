from fastapi import APIRouter
from fastapi.responses import JSONResponse
from datetime import datetime, timezone

from app.database import get_engine

router = APIRouter(prefix="/health", tags=["Monitoring"])

__version__ = "1.2.0"

@router.get("")
async def health_check():
    """
    Endpoint de monitoreo Liveness/Readiness para orquestadores (K8s / Render / AWS).
    Valida en tiempo real la conexión a las bases de datos requeridas.
    """
    status = "healthy"
    checks = {
        "sqlite_global": False,
        "redis": False
    }
    
    # Validar SQLite Global
    try:
        engine = get_engine("system_config", "global")
        # El context manager de engine asegura que cerramos la conexión de prueba rápido
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("SELECT 1"))
        checks["sqlite_global"] = True
    except Exception as e:
        status = "unhealthy"
        
    # Validar soporte Redis
    try:
        import redis
        checks["redis"] = True
    except ImportError:
        checks["redis"] = False

    response = {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "checks": checks
    }
    
    # 503 Service Unavailable alertará a los Load Balancers si la DB está inaccesible.
    if status != "healthy":
        return JSONResponse(status_code=503, content=response)
        
    return response
