from sqlalchemy import Column, Integer, String, Boolean, Date, Float
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
    run_interval_sec = Column(Integer, default=5)

class DailyStat(Base):
    """Modelo de base de datos para almacenar el histórico de eventos procesados por día calendario."""
    __tablename__ = "daily_stats"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, index=True)
    provider = Column(String, index=True)
    env = Column(String, index=True)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    avg_transmission_latency_sec = Column(Float, nullable=True)
    avg_hub_latency_sec = Column(Float, nullable=True)
