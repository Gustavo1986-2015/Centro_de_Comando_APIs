from sqlalchemy import Column, Integer, String, Boolean, Date, Float, JSON
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
    use_mock = Column(Boolean, default=True)
    purge_interval_min = Column(Integer, default=15)
    run_interval_sec = Column(Integer, default=5)
    queue_backend = Column(String, default="sqlite") # sqlite, redis, postgres
    mapping_schema = Column(JSON, default={})
    fetch_config = Column(JSON, default={})        # Guarda URL, auth_type, user, pass para extraer telemetría
    enrichment_config = Column(JSON, default={})   # Guarda URL y reglas para extraer el diccionario (IMEI -> Placa)
    
    # NUEVOS campos cifrados para Envelope Encryption
    rc_password_enc = Column(String, nullable=True)
    fetch_config_enc = Column(String, nullable=True) # Text en el spec, pero String funciona igual o TEXT
    webhook_auth_secret_enc = Column(String, nullable=True)
    webhook_auth_header = Column(String, default="x-api-key")
    enable_state_dedup = Column(Boolean, default=True)

class ProviderDictionary(Base):
    """Almacena pares Key-Value del diccionario de metadatos (Ej. IMEI -> Placa)."""
    __tablename__ = "provider_dictionary"
    id = Column(Integer, primary_key=True, index=True)
    provider_name = Column(String, index=True)
    env = Column(String, index=True)
    dict_key = Column(String, index=True)  # Ej. '512345678901234' (IMEI)
    dict_value = Column(String)            # Ej. 'ABC1234' (Placa)

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
    avg_rc_latency_sec = Column(Float, nullable=True)
    avg_push_latency_ms = Column(Float, nullable=True)

class SystemSettings(Base):
    """Modelo para configuración global e infraestructura del Hub."""
    __tablename__ = "system_settings"
    id = Column(Integer, primary_key=True, index=True)
    active_queue_backend = Column(String, default="sqlite") # 'sqlite', 'redis' o 'postgres'
    audit_retention_days = Column(Integer, default=30)
    processed_retention_days = Column(Integer, default=30)
    processed_logs_enabled = Column(Boolean, default=True)
