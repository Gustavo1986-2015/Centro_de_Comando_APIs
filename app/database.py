from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from fastapi import Query
from contextlib import contextmanager
import os
import logging
logger = logging.getLogger(__name__)


Base = declarative_base()

_engines = {}
_sessions = {}

def get_db_url(provider: str, env: str) -> str:
    """Retorna la URL de conexión de SQLite según proveedor y entorno.
    
    Estructura de carpetas:
      db/system_config_global.db          ← archivo maestro, siempre en raíz
      db/{provider}/{env}.db              ← colas operativas por AVL
    """
    if provider == "system_config":
        os.makedirs("./db", exist_ok=True)
        return "sqlite:///./db/system_config_global.db"
    os.makedirs(f"./db/{provider}", exist_ok=True)
    return f"sqlite:///./db/{provider}/{env}.db"

def check_and_migrate_db():
    """Ejecuta una migración automática para agregar campos que falten en sqlite."""
    import sqlite3
    db_path = "./db/system_config_global.db"
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 1. Migración para provider_config
        cursor.execute("PRAGMA table_info(provider_config)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns:
            if "run_interval_sec" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN run_interval_sec INTEGER DEFAULT 5")
                conn.commit()
            if "queue_backend" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN queue_backend TEXT DEFAULT 'sqlite'")
                conn.commit()
            if "use_mock" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN use_mock BOOLEAN DEFAULT 1")
                conn.commit()
            if "mapping_schema" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN mapping_schema JSON DEFAULT '{}'")
                conn.commit()
            if "fetch_config" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN fetch_config TEXT DEFAULT '{}'")
                conn.commit()
            if "enrichment_config" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN enrichment_config TEXT DEFAULT '{}'")
                conn.commit()
            
            # Nuevas columnas Envelope Encryption
            if "rc_password_enc" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN rc_password_enc TEXT")
                conn.commit()
            if "fetch_config_enc" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN fetch_config_enc TEXT")
                conn.commit()
            if "webhook_auth_secret_enc" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN webhook_auth_secret_enc TEXT")
                conn.commit()
            if "webhook_auth_header" not in columns:
                cursor.execute("ALTER TABLE provider_config ADD COLUMN webhook_auth_header TEXT DEFAULT 'x-api-key'")
                conn.commit()
                
            # Migración: cifrar campos legacy plaintext
            cursor.execute("PRAGMA table_info(provider_config)")
            updated_columns = [row[1] for row in cursor.fetchall()]
            
            if all(col in updated_columns for col in ["rc_password_enc", "fetch_config_enc", "webhook_auth_secret_enc"]):
                from app.core.crypto import encrypt, is_encrypted
                
                rows = cursor.execute("""
                    SELECT id, rc_password, fetch_config, provider_name 
                    FROM provider_config
                """).fetchall()
                
                migrated = 0
                for row_id, rc_pass, fetch_cfg, p_name in rows:
                    updates = {}
                    if rc_pass and not is_encrypted(rc_pass):
                        updates["rc_password_enc"] = encrypt(rc_pass)
                        updates["rc_password"] = None  # borrar plaintext
                    if fetch_cfg and not is_encrypted(fetch_cfg):
                        updates["fetch_config_enc"] = encrypt(fetch_cfg)
                        updates["fetch_config"] = None
                    
                    if updates:
                        set_clauses = ", ".join([f"{k} = ?" for k in updates])
                        values = list(updates.values()) + [row_id]
                        cursor.execute(
                            f"UPDATE provider_config SET {set_clauses} WHERE id = ?",
                            values
                        )
                        migrated += 1
                
                if migrated:
                    logger.info(f"Migracion cifrado: {migrated} proveedores migrados a ciphertext.")
                    conn.commit()
                    
                # Migración legacy Schmitz (.env -> DB)
                schmitz_key = os.getenv("SCHMITZ_API_KEY")
                if schmitz_key:
                    row = cursor.execute(
                        "SELECT id, webhook_auth_secret_enc FROM provider_config WHERE provider_name = 'schmitz'"
                    ).fetchone()
                    if row and not row[1]:
                        cursor.execute(
                            "UPDATE provider_config SET webhook_auth_secret_enc = ? WHERE id = ?",
                            (encrypt(schmitz_key), row[0])
                        )
                        logger.info("Schmitz: SCHMITZ_API_KEY migrada a DB cifrada.")
                        conn.commit()
                        
                # Migración: añadir enable_state_dedup a provider_config
                cursor.execute("PRAGMA table_info(provider_config)")
                columns_prov = [row[1] for row in cursor.fetchall()]
                if "enable_state_dedup" not in columns_prov:
                    cursor.execute("ALTER TABLE provider_config ADD COLUMN enable_state_dedup BOOLEAN DEFAULT 1")
                    conn.commit()
            
            cursor.execute("CREATE TABLE IF NOT EXISTS provider_dictionary (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_name TEXT, env TEXT, dict_key TEXT, dict_value TEXT)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_provider_dictionary_provider_name ON provider_dictionary (provider_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_provider_dictionary_env ON provider_dictionary (env)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_provider_dictionary_dict_key ON provider_dictionary (dict_key)")
            conn.commit()
            
        # 2. Migración para daily_stats
        cursor.execute("PRAGMA table_info(daily_stats)")
        columns_stats = [row[1] for row in cursor.fetchall()]
        if columns_stats:
            if "avg_transmission_latency_sec" not in columns_stats:
                cursor.execute("ALTER TABLE daily_stats ADD COLUMN avg_transmission_latency_sec REAL")
                conn.commit()
            if "avg_hub_latency_sec" not in columns_stats:
                cursor.execute("ALTER TABLE daily_stats ADD COLUMN avg_hub_latency_sec REAL")
                conn.commit()
            if "avg_rc_latency_sec" not in columns_stats:
                cursor.execute("ALTER TABLE daily_stats ADD COLUMN avg_rc_latency_sec REAL")
                conn.commit()
            if "avg_push_latency_ms" not in columns_stats:
                cursor.execute("ALTER TABLE daily_stats ADD COLUMN avg_push_latency_ms REAL")
                conn.commit()
                
        # 3. Migración para system_settings
        cursor.execute("PRAGMA table_info(system_settings)")
        columns_sys = [row[1] for row in cursor.fetchall()]
        if columns_sys:
            if "audit_retention_days" not in columns_sys:
                cursor.execute("ALTER TABLE system_settings ADD COLUMN audit_retention_days INTEGER DEFAULT 30")
                conn.commit()
            if "processed_retention_days" not in columns_sys:
                cursor.execute("ALTER TABLE system_settings ADD COLUMN processed_retention_days INTEGER DEFAULT 30")
                conn.commit()
            if "processed_logs_enabled" not in columns_sys:
                cursor.execute("ALTER TABLE system_settings ADD COLUMN processed_logs_enabled BOOLEAN DEFAULT 1")
                conn.commit()

            cursor.execute("SELECT COUNT(*) FROM system_settings")
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    INSERT INTO system_settings 
                    (audit_retention_days, processed_retention_days, processed_logs_enabled)
                    VALUES (30, 30, 1)
                """)
                conn.commit()
    except Exception as e:
        logger.debug(f"Migración idempotente omitida o error esperado BD: {e}")
        pass

def check_and_migrate_provider_db(provider: str, env: str):
    """Ejecuta una migración automática para agregar campos faltantes en normalized_rc_events."""
    import sqlite3
    url = get_db_url(provider, env)
    db_path = url.replace("sqlite:///./", "./")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(normalized_rc_events)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns:
            if "rc_latency_sec" not in columns:
                cursor.execute("ALTER TABLE normalized_rc_events ADD COLUMN rc_latency_sec REAL")
                conn.commit()
            if "retry_count" not in columns:
                cursor.execute("ALTER TABLE normalized_rc_events ADD COLUMN retry_count INTEGER DEFAULT 0")
                conn.commit()
            if "next_retry_at" not in columns:
                cursor.execute("ALTER TABLE normalized_rc_events ADD COLUMN next_retry_at DATETIME")
                conn.commit()
            
            # Crear índices optimizados para selección rápida de lotes y búsquedas
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_retry ON normalized_rc_events(status, next_retry_at, id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_chassis_status ON normalized_rc_events(chassis_number, status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_updated_processing ON normalized_rc_events(status, updated_at)")
            conn.commit()
    except Exception as e:
        logger.warning(f"Migracion idempotente omitida: {e}")
        pass

def get_engine(provider: str, env: str = "prod"):
    """Devuelve (y crea si no existe) el motor SQLite para el proveedor y entorno dados."""
    key = f"{provider}_{env}"

    if key not in _engines:
        url = get_db_url(provider, env)
        engine = create_engine(url, connect_args={"check_same_thread": False})
        
        # Habilitar SQLite WAL Mode (Write-Ahead Logging) nativo por conexión
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            # WAL mode es persistente — no necesita setearse en cada conexión.
            # Solo setear si no está ya en WAL (evita race condition bajo carga en Docker).
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass  # Ya está en WAL o transient lock — ignorar
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=3000")
            cursor.close()
        
        # Asegurar que los modelos estén registrados en Base.metadata
        if provider == "system_config":
            from app.models.config_models import ProviderConfig, DailyStat, SystemSettings, ProviderDictionary
            target_tables = [ProviderConfig.__table__, DailyStat.__table__, SystemSettings.__table__, ProviderDictionary.__table__]
        else:
            from app.models.db_models import NormalizedRCEvent
            target_tables = [NormalizedRCEvent.__table__]
            
        # Crear tablas
        Base.metadata.create_all(bind=engine, tables=target_tables)
        
        # Migración automática
        if provider == "system_config":
            check_and_migrate_db()
        else:
            check_and_migrate_provider_db(provider, env)
            
        _engines[key] = engine
        _sessions[key] = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        
    return _engines[key]

def get_session(provider: str, env: str = "prod"):
    """Devuelve una nueva sesión para la BD del proveedor/entorno."""
    key = f"{provider}_{env}"
    get_engine(provider, env)
    session = _sessions[key]()
    # Forzar el reinicio de la transacción para evitar snapshots viejos en SQLite WAL
    session.rollback()
    return session

@contextmanager
def session_context(provider: str, env: str = "prod"):
    """
    Gestiona el ciclo de vida de una sesión de base de datos de forma atómica.
    Efectúa commit() automáticamente si no hay excepciones.
    Efectúa rollback() ante cualquier excepción.
    Siempre ejecuta close() liberando la conexión.
    """
    db = get_session(provider, env)
    try:
        yield db
        db.commit()
    except Exception as e:
        logger.warning(f"Migracion idempotente omitida: {e}")
        db.rollback()
        raise
    finally:
        db.close()

def get_db_provider(provider: str):
    """
    Fábrica de dependencias para FastAPI.
    Lee automáticamente el query param ?env= (por defecto 'prod').
    """
    def _get_db(env: str = Query("prod", description="Entorno: test o prod")):
        db = get_session(provider, env)
        try:
            yield db
        finally:
            db.close()
    return _get_db
