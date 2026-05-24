from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os

Base = declarative_base()

# Almacenamiento en caché de engines y sessionmakers por proveedor
_engines = {}
_sessions = {}

def get_engine(provider: str):
    """Devuelve (y crea si no existe) el motor SQLite para el proveedor dado."""
    if provider not in _engines:
        os.makedirs("db", exist_ok=True)
        url = f"sqlite:///./db/{provider}.db"
        engine = create_engine(url, connect_args={"check_same_thread": False})
        
        # Crear tablas si no existen en esta base de datos específica
        Base.metadata.create_all(bind=engine)
        
        _engines[provider] = engine
        _sessions[provider] = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        
    return _engines[provider]

def get_session(provider: str):
    """Devuelve una nueva sesión para la BD del proveedor."""
    get_engine(provider) # Asegurarse de que el engine está inicializado
    return _sessions[provider]()

def get_db_provider(provider: str):
    """
    Fábrica de dependencias para FastAPI.
    Ejemplo de uso: Depends(get_db_provider("schmitz"))
    """
    def _get_db():
        db = get_session(provider)
        try:
            yield db
        finally:
            db.close()
    return _get_db
