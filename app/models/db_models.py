from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base

class NormalizedRCEvent(Base):
    """
    Modelo Central de Eventos Telemáticos (Hub).
    Almacena el evento normalizado junto con el JSON crudo del proveedor original.
    """
    __tablename__ = "normalized_rc_events"

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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
