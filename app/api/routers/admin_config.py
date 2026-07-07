import os
import json
import logging
from typing import List
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Body, Query, Request
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from app.core.auth import verify_dashboard_auth
from app.database import get_session
from app.models.config_models import ProviderConfig, SystemSettings
from app.core import config_cache
from app.core.auditor import log_admin_action
from app.core.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin Config"])

# =====================================================================
# MODELOS PYDANTIC
# =====================================================================
class ConfigUpdate(BaseModel):
    id: int
    is_active: bool
    rc_user: str
    rc_password: str | None = None
    use_mock: bool
    purge_interval_min: int
    run_interval_sec: int
    queue_backend: str
    webhook_auth_secret: str | None = None
    webhook_auth_header: str | None = None
    fetch_config: str | None = None
    enable_state_dedup: bool = True

class RetentionUpdateModel(BaseModel):
    audit_retention_days: int
    processed_retention_days: int

class ProcessedLogsToggleModel(BaseModel):
    enabled: bool

class PurgeLogsModel(BaseModel):
    category: str  # "crudos" | "procesados" | "ambos"
    days: int
    confirm_text: str
    admin_password: str

# =====================================================================
# ENDPOINTS DE PROVEEDORES Y MAPEOS
# =====================================================================

@router.get("/api/config/providers")
def get_providers(_: None = Depends(verify_dashboard_auth)):
    config_db = get_session("system_config", "global")
    try:
        providers = config_db.query(ProviderConfig).all()
        return [{"id": p.id, "provider_name": p.provider_name, "env": p.env} for p in providers]
    finally:
        config_db.close()

@router.post("/api/config/providers")
def create_provider(payload: dict, _: None = Depends(verify_dashboard_auth)):
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
            use_mock=True,
            queue_backend="sqlite",
            mapping_schema={},
            enable_state_dedup=True
        )
        new_test = ProviderConfig(
            provider_name=provider_name,
            env="test",
            is_active=True,
            use_mock=True,
            queue_backend="sqlite",
            mapping_schema={},
            enable_state_dedup=True
        )
        
        config_db.add_all([new_prod, new_test])
        config_db.commit()
        return {"status": "success", "message": "Proveedor creado exitosamente en prod y test."}
    except Exception as e:
        logger.warning(f"Excepción capturada en admin_config: {e}")
        config_db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        config_db.close()

@router.post("/api/config/{provider_name}/{env}/mapping")
def save_mapping(provider_name: str, env: str, payload: dict, _: None = Depends(verify_dashboard_auth)):
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
                config.fetch_config_enc = encrypt(json.dumps(payload.get('fetch', {})))
                config.fetch_config = None
        else:
            config.mapping_schema = payload
            
        config_db.commit()
        return {"status": "success"}
    except Exception as e:
        logger.warning(f"Excepción capturada en admin_config: {e}")
        config_db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        config_db.close()
        
@router.get("/api/config/{provider_name}/{env}/mapping")
def get_mapping(provider_name: str, env: str, _: None = Depends(verify_dashboard_auth)):
    config_db = get_session("system_config", "global")
    try:
        config = config_db.query(ProviderConfig).filter(
            ProviderConfig.provider_name.ilike(provider_name),
            ProviderConfig.env == env
        ).first()
        fetch_c = {}
        if config.fetch_config_enc:
            dec_str = decrypt(config.fetch_config_enc)
            if dec_str:
                try:
                    fetch_c = json.loads(dec_str)
                except Exception:
                    pass
        else:
            fetch_c = config.fetch_config or {}

        return {
            "mapping": config.mapping_schema or {},
            "fetch": fetch_c
        }
    finally:
        config_db.close()

@router.post("/api/config/{provider_name}/{env}/enrichment")
def save_enrichment(provider_name: str, env: str, payload: dict, _: None = Depends(verify_dashboard_auth)):
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
        logger.warning(f"Excepción capturada en admin_config: {e}")
        config_db.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        config_db.close()
        
@router.get("/api/config/{provider_name}/{env}/enrichment")
def get_enrichment(provider_name: str, env: str, _: None = Depends(verify_dashboard_auth)):
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
def get_all_configs(_: None = Depends(verify_dashboard_auth)):
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
            "has_rc_password": bool(c.rc_password_enc or c.rc_password),
            "has_webhook_auth": bool(c.webhook_auth_secret_enc),
            "has_fetch_config": bool(c.fetch_config_enc or c.fetch_config),
            "webhook_auth_header": c.webhook_auth_header or "x-api-key",
            "use_mock": c.use_mock,
            "purge_interval_min": c.purge_interval_min,
            "run_interval_sec": c.run_interval_sec,
            "queue_backend": c.queue_backend if hasattr(c, 'queue_backend') and c.queue_backend else "sqlite",
            "enable_state_dedup": getattr(c, 'enable_state_dedup', True)
        } for c in configs]
    finally:
        db.close()

@router.post("/api/config")
def update_configs(updates: List[ConfigUpdate], _: None = Depends(verify_dashboard_auth)):
    db = get_session("system_config", "global")
    try:
        for u in updates:
            conf = db.query(ProviderConfig).filter(ProviderConfig.id == u.id).first()
            if conf:
                conf.is_active = u.is_active
                conf.rc_user = u.rc_user
                
                # Se envían desde un nuevo endpoint o modelo extendido. 
                # El modelo ConfigUpdate necesita soportar estos campos opcionales.
                if hasattr(u, 'rc_password') and u.rc_password and u.rc_password != "••••••••" and u.rc_password.strip() != "":
                    conf.rc_password_enc = encrypt(u.rc_password)
                    conf.rc_password = None # borrar plaintext si existía
                    
                if hasattr(u, 'webhook_auth_secret') and u.webhook_auth_secret and u.webhook_auth_secret != "••••••••" and u.webhook_auth_secret.strip() != "":
                    conf.webhook_auth_secret_enc = encrypt(u.webhook_auth_secret)
                    
                if hasattr(u, 'webhook_auth_header') and u.webhook_auth_header:
                    conf.webhook_auth_header = u.webhook_auth_header
                    
                if hasattr(u, 'fetch_config') and u.fetch_config and u.fetch_config != "••••••••" and u.fetch_config.strip() != "":
                    conf.fetch_config_enc = encrypt(u.fetch_config)
                    conf.fetch_config = None # borrar plaintext
                    
                conf.use_mock = u.use_mock
                conf.purge_interval_min = u.purge_interval_min
                conf.run_interval_sec = u.run_interval_sec
                conf.queue_backend = u.queue_backend.lower()
                
                if hasattr(u, 'enable_state_dedup'):
                    conf.enable_state_dedup = u.enable_state_dedup
                    
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()

# =====================================================================
# ENDPOINTS DE CONFIGURACIÓN Y PURGA DE LOGS
# =====================================================================

@router.get("/api/config/retention")
def get_retention_config(request: Request, _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    settings = config_cache.get_settings()
    return {
        "audit_retention_days": settings.audit_retention_days,
        "processed_retention_days": settings.processed_retention_days,
        "processed_logs_enabled": settings.processed_logs_enabled
    }

@router.put("/api/config/retention")
def update_retention_config(
    body: RetentionUpdateModel,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)
):
    if not (7 <= body.audit_retention_days <= 90):
        raise HTTPException(status_code=400, detail="Retención de auditoría debe estar entre 7 y 90 días")
    if not (7 <= body.processed_retention_days <= 30):
        raise HTTPException(status_code=400, detail="Retención de procesados debe estar entre 7 y 30 días")
        
    db = get_session("system_config", "global")
    try:
        settings = db.query(SystemSettings).first()
        if settings:
            settings.audit_retention_days = body.audit_retention_days
            settings.processed_retention_days = body.processed_retention_days
            db.commit()
            config_cache.invalidate()
            log_admin_action("update_retention", body.dict(), request, _auth.username)
            return {"ok": True, "message": "Retención actualizada correctamente"}
        raise HTTPException(status_code=500, detail="Configuración no encontrada en base de datos")
    except Exception as e:
        logger.error(f"Error actualizando retención: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@router.put("/api/config/processed-logs-toggle")
def toggle_processed_logs(
    body: ProcessedLogsToggleModel,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)
):
    db = get_session("system_config", "global")
    try:
        settings = db.query(SystemSettings).first()
        if settings:
            settings.processed_logs_enabled = body.enabled
            db.commit()
            config_cache.invalidate()
            log_admin_action("toggle_processed_logs", body.dict(), request, _auth.username)
            return {"ok": True, "message": f"Backups de procesados {'activados' if body.enabled else 'desactivados'}"}
        raise HTTPException(status_code=500, detail="Configuración no encontrada en base de datos")
    except Exception as e:
        logger.error(f"Error en toggle_processed_logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@router.post("/api/config/purge-logs")
def manual_purge_logs(
    body: PurgeLogsModel,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)
):
    expected_password = os.getenv("DASHBOARD_PASSWORD", "admin")
    if body.admin_password != expected_password:
        raise HTTPException(status_code=401, detail="Contraseña incorrecta")
        
    if body.days < 7:
        raise HTTPException(status_code=400, detail="Mínimo de purga es 7 días")
        
    if body.confirm_text != "PURGAR":
        raise HTTPException(status_code=400, detail="Texto de confirmación inválido")
        
    if body.category not in ("crudos", "procesados", "ambos"):
        raise HTTPException(status_code=400, detail="Categoría inválida")

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(days=body.days)
    
    today_start_ts = today_start.timestamp()
    cutoff_ts = cutoff.timestamp()
    
    dirs_to_clean = []
    if body.category in ("crudos", "ambos"):
        dirs_to_clean.append("audit")
    if body.category in ("procesados", "ambos"):
        dirs_to_clean.append(os.path.join("db", "backups_diarios"))
        
    deleted_files = 0
    freed_bytes = 0
    
    for d in dirs_to_clean:
        if os.path.exists(d):
            for root, dirs, files in os.walk(d):
                for file in files:
                    if file.endswith(".json") or file.endswith(".jsonl"):
                        filepath = os.path.join(root, file)
                        try:
                            mtime = os.path.getmtime(filepath)
                            if mtime < cutoff_ts and mtime < today_start_ts:
                                size = os.path.getsize(filepath)
                                os.remove(filepath)
                                deleted_files += 1
                                freed_bytes += size
                        except Exception as e:
                            logger.warning(f"No se pudo eliminar {filepath}: {e}")
                            
    log_admin_action(
        "manual_purge", 
        {"category": body.category, "days": body.days, "deleted_files": deleted_files, "freed_bytes": freed_bytes}, 
        request, 
        _auth.username
    )
    
    return {
        "ok": True,
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
        "cutoff_date": cutoff.isoformat()
    }
