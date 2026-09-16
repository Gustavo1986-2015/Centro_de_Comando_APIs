from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, Index
from sqlalchemy.sql import func
from app.database import Base

class NormalizedRCEvent(Base):
    """
    Modelo Central de Eventos Telemáticos (Hub).
    Almacena el evento normalizado junto con el JSON crudo del proveedor original.
    """
    __tablename__ = "normalized_rc_events"
    __table_args__ = (
        Index('idx_retry_status', 'retry_count', 'status'),
        # La purga filtra por (status, created_at) y las estadísticas por
        # created_at. Sin este índice ambas hacían recorrido completo: el de
        # status no sirve porque prácticamente todas las filas son 'sent'.
        Index('idx_status_created', 'status', 'created_at'),
    )

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, index=True)  # Ej. 'schmitz'
    status = Column(String, default="pending", index=True) # pending, sent, failed
    raw_data = Column(Text) # JSON crudo almacenado como texto
    rc_response = Column(Text, nullable=True) # Respuesta de Recurso Confiable
    job_id = Column(String, nullable=True, index=True) # ID de acuse de recibo

    # Datos normalizados (RC Canonical Model)
    chassis_number = Column(String, index=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    speed = Column(Float, nullable=True)
    code = Column(String, nullable=True)
    date = Column(DateTime, nullable=True) # ISO8601 UTC 0
    altitude = Column(Float, nullable=True)
    battery = Column(Float, nullable=True)
    course = Column(Float, nullable=True)
    humidity = Column(Float, nullable=True)
    ignition = Column(Boolean, nullable=True)
    odometer = Column(Float, nullable=True)
    temperature = Column(Float, nullable=True)
    serial_number = Column(String, nullable=True)
    shipment = Column(String, nullable=True)
    vehicle_type = Column(String, nullable=True)
    vehicle_brand = Column(String, nullable=True)
    vehicle_model = Column(String, nullable=True)

    # Trazabilidad
    # Identificador asignado en la RECEPCIÓN, antes de responder 202.
    #
    # Es lo que hace idempotente al reintentador: sin una clave natural, un
    # evento reinsertado desde la red de seguridad se duplicaría, y el
    # duplicado viajaría a Recurso Confiable. La tabla no tenía ninguna
    # restricción única y el id es autoincremental, así que no había forma de
    # distinguir "este ya entró" de "este es otro evento igual".
    #
    # Admite NULL: las filas anteriores a la migración no lo tienen, y SQLite
    # permite múltiples NULL en un índice único. Por eso no hace falta
    # rellenar nada hacia atrás.
    ingest_id = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    rc_latency_sec = Column(Float, nullable=True)
    retry_count = Column(Integer, default=0)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
