import time
from app.database import get_session
from app.models.config_models import SystemSettings

_cache = {"data": None, "expires_at": 0}

def get_settings() -> SystemSettings:
    """Obtiene la configuración global (SystemSettings) cacheada por 60 segundos."""
    if time.time() > _cache["expires_at"]:
        db = get_session("system_config", "global")
        try:
            settings = db.query(SystemSettings).first()
            # Desasociar el objeto de la sesión para poder usarlo de forma segura tras el cierre
            if settings:
                db.expunge(settings)
            else:
                # Si no existe, crear valores default en memoria
                settings = SystemSettings(
                    audit_retention_days=30,
                    processed_retention_days=30,
                    processed_logs_enabled=True
                )
            _cache["data"] = settings
            _cache["expires_at"] = time.time() + 60
        finally:
            db.close()
    return _cache["data"]

def invalidate():
    """Invalida el caché forzando una lectura a DB en la próxima llamada."""
    _cache["expires_at"] = 0
