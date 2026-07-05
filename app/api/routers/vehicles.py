from datetime import datetime
import logging
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.security import HTTPBasicCredentials

from app.core.auth import verify_dashboard_auth
from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.models.config_models import ProviderConfig

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Vehicles"])

@router.get("/api/vehicles/unique")
def get_unique_vehicles(
    date: str = Query(None, description="Fecha en formato YYYY-MM-DD. Por defecto: hoy."),
    provider: str = Query(None, description="Filtrar por proveedor específico. Por defecto: todos."),
    search: str = Query(None, description="Búsqueda libre por texto en la patente/chasis."),
    _: None = Depends(verify_dashboard_auth)
):
    """
    Devuelve los vehículos únicos (chassis_number DISTINCT) que generaron eventos
    en la fecha indicada, opcionalmente filtrados por proveedor y búsqueda libre.
    
    Respuesta: { "protrack_test": { "total": 5, "vehicles": ["C131091", ...] }, ... }
    """
    # --- Determinar rango de fechas ---
    try:
        if date:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        else:
            target_date = datetime.now().date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD.")

    day_start = datetime.combine(target_date, datetime.min.time())
    day_end   = datetime.combine(target_date, datetime.max.time())

    # --- Obtener la lista de proveedores configurados ---
    config_db = get_session("system_config", "global")
    try:
        all_providers = config_db.query(ProviderConfig).all()
    finally:
        config_db.close()

    result = {}

    for p in all_providers:
        # Filtrar por proveedor si se indicó
        if provider and provider.lower() not in ("all", "") and p.provider_name.lower() != provider.lower():
            continue

        key = f"{p.provider_name}_{p.env}"
        db = get_session(p.provider_name, p.env)
        try:
            query = db.query(NormalizedRCEvent.chassis_number).filter(
                NormalizedRCEvent.created_at >= day_start,
                NormalizedRCEvent.created_at <= day_end,
                NormalizedRCEvent.chassis_number != None,
                NormalizedRCEvent.chassis_number != ""
            ).distinct()

            # Búsqueda libre de texto parcial (LIKE %search%)
            if search and search.strip():
                query = query.filter(
                    NormalizedRCEvent.chassis_number.ilike(f"%{search.strip()}%")
                )

            vehicles = sorted([row[0] for row in query.all() if row[0]])
            result[key] = {
                "provider": p.provider_name.upper(),
                "env": p.env.upper(),
                "total": len(vehicles),
                "vehicles": vehicles
            }
        except Exception as e:
            logger.warning(f"Excepción capturada en dashboard: {e}")
            result[key] = {"provider": p.provider_name.upper(), "env": p.env.upper(), "total": 0, "vehicles": [], "error": str(e)}
        finally:
            db.close()

    return result

@router.get("/api/vehicles/data")
def get_vehicle_data(
    provider: str = Query(...),
    env: str = Query(...),
    chassis: str = Query(...),
    date: str = Query(None, description="Fecha YYYY-MM-DD. Por defecto: hoy."),
    _: None = Depends(verify_dashboard_auth)
):
    """
    Devuelve todo el historial de eventos de un chasis particular en una fecha dada.
    Útil para descargar/copiar los datos en formato JSON crudo.
    """
    try:
        if date:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        else:
            target_date = datetime.now().date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido.")

    day_start = datetime.combine(target_date, datetime.min.time())
    day_end   = datetime.combine(target_date, datetime.max.time())
    
    db = get_session(provider, env)
    try:
        query = db.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.created_at >= day_start,
            NormalizedRCEvent.created_at <= day_end,
            NormalizedRCEvent.chassis_number == chassis
        ).order_by(NormalizedRCEvent.created_at.desc()).limit(500).all()
        
        # Serializar omitiendo campos internos de SQLAlchemy
        data = []
        for r in query:
            d = {k: v for k, v in r.__dict__.items() if not k.startswith('_')}
            for dt_field in ['date', 'created_at', 'updated_at', 'next_retry_at']:
                if isinstance(d.get(dt_field), datetime):
                    d[dt_field] = d[dt_field].isoformat()
            data.append(d)
            
        return data
    finally:
        db.close()
