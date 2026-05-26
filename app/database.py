from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from fastapi import Query
import os

Base = declarative_base()

_engines = {}
_sessions = {}

def get_db_url(provider: str, env: str) -> str:
    """Retorna la URL de conexión de SQLite según proveedor y entorno."""
    os.makedirs("./db", exist_ok=True)
    if provider == "system_config":
        return "sqlite:///./db/telematics_hub.db"
    return f"sqlite:///./db/{provider}_{env}.db"

def check_and_migrate_db():
    """Ejecuta una migración automática para agregar run_interval_sec a provider_config en sqlite."""
    import sqlite3
    db_path = "./db/telematics_hub.db"
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(provider_config)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "run_interval_sec" not in columns:
            cursor.execute("ALTER TABLE provider_config ADD COLUMN run_interval_sec INTEGER DEFAULT 5")
            conn.commit()
    except Exception:
        pass

def get_engine(provider: str, env: str = "prod"):
    """Devuelve (y crea si no existe) el motor SQLite para el proveedor y entorno dados."""
    key = f"{provider}_{env}"
    if key not in _engines:
        url = get_db_url(provider, env)
        engine = create_engine(url, connect_args={"check_same_thread": False})
        
        # Asegurar que los modelos estén registrados en Base.metadata
        if provider == "system_config":
            from app.models.config_models import ProviderConfig, DailyStat
        else:
            from app.models.db_models import NormalizedRCEvent
            
        # Crear tablas
        Base.metadata.create_all(bind=engine)
        
        # Migración automática si es la base de datos de configuración global
        if provider == "system_config":
            check_and_migrate_db()
            
        _engines[key] = engine
        _sessions[key] = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        
    return _engines[key]

def get_session(provider: str, env: str = "prod"):
    """Devuelve una nueva sesión para la BD del proveedor/entorno."""
    key = f"{provider}_{env}"
    get_engine(provider, env)
    return _sessions[key]()

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
