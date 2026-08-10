import os
import json
import logging
import secrets
from typing import List
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from app.core.auth import verify_dashboard_auth, get_dashboard_password
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
    # Techo de peticiones por minuto del webhook. None = usar el límite global.
    rate_limit_per_min: int | None = None
    # Contraseña de administrador, requerida solo al ACTIVAR el modo simulado.
    # No se envía en operaciones que no lo activan.
    admin_password: str | None = None

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
        
        # PUSH: dedup off por defecto (ya tienen dedup interno o no lo necesitan)
        # PULL: dedup on por defecto (filtra alarmas repetidas cada ciclo)
        p_type = payload.get("provider_type", "pull").lower()
        if p_type not in ("push", "pull"):
            p_type = "pull"
        dedup_default = (p_type == "pull")
            
        new_prod = ProviderConfig(
            provider_name=provider_name,
            env="prod",
            is_active=False,
            use_mock=True,
            queue_backend="sqlite",
            mapping_schema={},
            provider_type=p_type,
            enable_state_dedup=dedup_default
        )
        new_test = ProviderConfig(
            provider_name=provider_name,
            env="test",
            is_active=True,
            use_mock=True,
            queue_backend="sqlite",
            mapping_schema={},
            provider_type=p_type,
            enable_state_dedup=dedup_default
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
            "provider_type": getattr(c, 'provider_type', 'pull') or 'pull',
            # NULL = usar el límite global; solo aplica a proveedores PUSH
            "rate_limit_per_min": getattr(c, 'rate_limit_per_min', None),
            "enable_state_dedup": bool(getattr(c, 'enable_state_dedup', True))
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
                    
                # ── Modo simulado: activación protegida ──────────────────
                # Con use_mock=True el sistema NO envía a Recurso Confiable:
                # genera job_ids falsos y marca los eventos como enviados. Si eso
                # ocurre sin que nadie lo note, una alarma real (robo, pánico)
                # queda registrada como despachada sin haber salido nunca.
                # Por eso activarlo exige revalidar la contraseña de administrador.
                if u.use_mock and not conf.use_mock:
                    correct_pass = get_dashboard_password()
                    provided = (u.admin_password or "")
                    if not correct_pass or not secrets.compare_digest(
                        provided.encode(), correct_pass.encode()
                    ):
                        raise HTTPException(
                            status_code=403,
                            detail=(
                                f"Activar el modo simulado en {conf.provider_name}/{conf.env} "
                                "requiere la contraseña de administrador. Con el modo simulado "
                                "activo los eventos NO se envían a Recurso Confiable."
                            ),
                        )
                    logger.warning(
                        f"MODO SIMULADO ACTIVADO para {conf.provider_name}/{conf.env}. "
                        f"Los eventos dejarán de enviarse a Recurso Confiable."
                    )
                elif conf.use_mock and not u.use_mock:
                    logger.info(
                        f"Modo simulado desactivado para {conf.provider_name}/{conf.env}. "
                        f"Los eventos vuelven a enviarse a Recurso Confiable."
                    )

                # Un valor <= 0 se interpreta como "sin límite propio": vuelve al global
                nuevo_limite = u.rate_limit_per_min if (u.rate_limit_per_min or 0) > 0 else None
                if nuevo_limite != getattr(conf, 'rate_limit_per_min', None):
                    conf.rate_limit_per_min = nuevo_limite
                    # El limitador cachea este valor 30s: invalidar para que el
                    # cambio tenga efecto inmediato y no a mitad de una prueba.
                    try:
                        from app.core.rate_limit import invalidate_limit_cache
                        invalidate_limit_cache(conf.provider_name)
                    except Exception as e:
                        logger.warning(f"No se pudo invalidar la caché del limitador: {e}")

                conf.use_mock = u.use_mock
                conf.purge_interval_min = u.purge_interval_min
                conf.run_interval_sec = u.run_interval_sec
                conf.queue_backend = u.queue_backend.lower()
                # Guardar toggle de deduplicación de estado
                if hasattr(conf, 'enable_state_dedup'):
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
    expected_password = get_dashboard_password()
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


# ─── Comportamiento ante fallas de Recurso Confiable ────────────────────────

class ComportamientoRCModel(BaseModel):
    """Parámetros que definen cómo reacciona el hub cuando RC falla."""
    rc_liberacion_tanda: int
    rc_max_reintentos: int
    rc_fallos_circuito: int
    rc_recuperacion_umbral_seg: int = 600


@router.get("/api/config/rc-behavior")
def get_rc_behavior(_auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    db = get_session("system_config", "global")
    try:
        cfg = db.query(SystemSettings).first()
        if not cfg:
            raise HTTPException(status_code=500, detail="Configuración no encontrada")
        return {
            "rc_liberacion_tanda": getattr(cfg, "rc_liberacion_tanda", None) or 500,
            "rc_max_reintentos": getattr(cfg, "rc_max_reintentos", None) or 4,
            "rc_fallos_circuito": getattr(cfg, "rc_fallos_circuito", None) or 5,
            "rc_recuperacion_umbral_seg": getattr(cfg, "rc_recuperacion_umbral_seg", None) or 600,
        }
    finally:
        db.close()


@router.put("/api/config/rc-behavior")
def update_rc_behavior(
    body: ComportamientoRCModel,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """
    Ajusta el comportamiento del hub ante fallas de RC sin reiniciar el servicio.

    Los rangos evitan configuraciones que romperían la operación: una tanda
    demasiado grande satura a RC al recuperarse, cero reintentos descarta
    eventos ante el primer tropiezo de red, y un umbral de circuito muy alto
    hace que el hub siga insistiendo sobre un destino caído.
    """
    if not (50 <= body.rc_liberacion_tanda <= 5000):
        raise HTTPException(
            status_code=400,
            detail="La tanda de liberación debe estar entre 50 y 5000 eventos.",
        )
    if not (1 <= body.rc_max_reintentos <= 10):
        raise HTTPException(
            status_code=400,
            detail="Los reintentos por evento deben estar entre 1 y 10.",
        )
    if not (2 <= body.rc_fallos_circuito <= 50):
        raise HTTPException(
            status_code=400,
            detail="Los fallos para abrir el circuito deben estar entre 2 y 50.",
        )
    # El mínimo evita arrebatarle a un worker un lote que sigue en vuelo: el
    # despacho más lento posible ronda los 5 minutos.
    if not (300 <= body.rc_recuperacion_umbral_seg <= 7200):
        raise HTTPException(
            status_code=400,
            detail=(
                "El umbral de rescate debe estar entre 300 y 7200 segundos. "
                "Por debajo de 300 se correría el riesgo de reencolar lotes que "
                "todavía se están enviando."
            ),
        )

    db = get_session("system_config", "global")
    try:
        cfg = db.query(SystemSettings).first()
        if not cfg:
            raise HTTPException(status_code=500, detail="Configuración no encontrada")

        cfg.rc_liberacion_tanda = body.rc_liberacion_tanda
        cfg.rc_max_reintentos = body.rc_max_reintentos
        cfg.rc_fallos_circuito = body.rc_fallos_circuito
        cfg.rc_recuperacion_umbral_seg = body.rc_recuperacion_umbral_seg
        db.commit()

        # El worker cachea estos valores 30s: invalidar para que el cambio
        # tenga efecto de inmediato, que es lo que se necesita en un incidente.
        try:
            from app.worker.processor import invalidar_parametros_rc, _rc_circuit_breaker
            invalidar_parametros_rc()
            _rc_circuit_breaker.failure_threshold = body.rc_fallos_circuito
        except Exception as e:
            logger.warning(f"No se pudo aplicar la configuración en caliente: {e}")

        log_admin_action("update_rc_behavior", body.dict(), request, _auth.username)
        logger.info(
            f"Comportamiento ante fallas de RC actualizado: "
            f"tanda={body.rc_liberacion_tanda}, reintentos={body.rc_max_reintentos}, "
            f"fallos_circuito={body.rc_fallos_circuito}"
        )
        return {"ok": True, "message": "Comportamiento ante fallas actualizado"}
    finally:
        db.close()
