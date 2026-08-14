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

    # Tipo de ingesta y deduplicación de estado
    provider_type = Column(String, default="pull")       # "push" | "pull"
    enable_state_dedup = Column(Boolean, default=True)   # Anti-State Flooding (PULL ON, PUSH OFF por migración)

    # Techo de peticiones por minuto del webhook de este proveedor.
    # NULL = usar el límite global. Solo aplica a proveedores PUSH: los PULL no
    # reciben peticiones entrantes, es el Hub quien sale a consultarlos.
    rate_limit_per_min = Column(Integer, nullable=True)

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

    # ── Comportamiento ante fallas de Recurso Confiable ──────────────────────
    # Estaban fijos en el código o en variables de entorno del servidor, sin
    # forma de ajustarlos durante un incidente sin acceso a la máquina.
    rc_liberacion_tanda = Column(Integer, default=500)   # eventos por ciclo al recuperarse RC
    rc_max_reintentos = Column(Integer, default=4)       # intentos por evento antes de darlo por fallido
    rc_fallos_circuito = Column(Integer, default=5)      # llamadas fallidas seguidas para dejar de insistir
    rc_recuperacion_umbral_seg = Column(Integer, default=600)  # antigüedad para rescatar un lote atascado

    # Horas que un evento ya despachado permanece en la base antes de purgarse.
    # La base es un colchón de tránsito: lo purgado queda en los respaldos JSONL.
    retencion_horas_db = Column(Integer, default=2)

    # Tope de días por descarga en las exportaciones de crudos y enviado-a-RC.
    # Un solo día de crudos a caudal de certificación son ~2 GB: sin techo, el
    # primer clic se lleva puesto el navegador. Se frena con un 400 explícito.
    export_max_days = Column(Integer, default=7)
