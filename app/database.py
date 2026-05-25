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

def get_engine(provider: str, env: str = "prod"):
    """Devuelve (y crea si no existe) el motor SQLite para el proveedor y entorno dados."""
    key = f"{provider}_{env}"
    if key not in _engines:
        url = get_db_url(provider, env)
        engine = create_engine(url, connect_args={"check_same_thread": False})
        
        # Crear tablas
        Base.metadata.create_all(bind=engine)
        
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
