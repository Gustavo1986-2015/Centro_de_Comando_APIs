from sqlalchemy import Column, Integer, String, Boolean
from app.database import Base

class ProviderConfig(Base):
    """Modelo de base de datos para la configuración central del sistema."""
    __tablename__ = "provider_config"
    id = Column(Integer, primary_key=True, index=True)
    provider_name = Column(String, index=True)
    env = Column(String, index=True) # prod o test
    is_active = Column(Boolean, default=True)
    rc_user = Column(String, default="")
    rc_password = Column(String, default="")
    purge_interval_min = Column(Integer, default=15)
