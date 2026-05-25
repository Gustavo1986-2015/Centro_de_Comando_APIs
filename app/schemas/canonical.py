from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime
import re

class RCCanonicalModel(BaseModel):
    """
    Modelo canónico para datos de Recurso Confiable (RC).
    Todos los adaptadores de proveedores deben mapear a este esquema.
    """
    chassis_number: str = Field(..., description="ChassisNumber o Plate")

    @field_validator('chassis_number', mode='before')
    @classmethod
    def sanitize_asset(cls, v: str) -> str:
        """
        Garantiza que el asset enviado a RC siempre esté en MAYÚSCULAS
        y no contenga guiones, espacios ni caracteres especiales.
        Ej: 'AB-1234' -> 'AB1234'
        """
        if not v:
            return v
        return re.sub(r'[^A-Z0-9]', '', str(v).upper())
    latitude: Optional[float] = Field(None, description="Latitud")
    longitude: Optional[float] = Field(None, description="Longitud")
    speed: Optional[float] = Field(None, description="Velocidad")
    code: Optional[str] = Field(None, description="Código de evento o motivo")
    date: Optional[datetime] = Field(None, description="Fecha y hora del evento en UTC")
    altitude: Optional[float] = Field(None, description="Altitud")
    battery: Optional[float] = Field(None, description="Voltaje de batería externa")
    course: Optional[float] = Field(None, description="Rumbo GPS")
    humidity: Optional[float] = Field(None, description="Humedad relativa")
    ignition: Optional[bool] = Field(None, description="Estado de ignición")
    odometer: Optional[float] = Field(None, description="Kilometraje")
    temperature: Optional[float] = Field(None, description="Temperatura")
    serial_number: Optional[str] = Field(None, description="Identificador único de hardware")
    shipment: Optional[str] = Field(None, description="Referencia de carga externa")
    vehicle_type: Optional[str] = Field(None, description="Tipo de trailer/vehículo")
    vehicle_brand: Optional[str] = Field(None, description="Marca del tráiler")
    vehicle_model: Optional[str] = Field(None, description="Modelo de telemática")

    class Config:
        from_attributes = True

class EventCreate(BaseModel):
    """
    Esquema para creación inicial de evento en la BD.
    """
    provider: str
    status: str = "pending"
    raw_data: str
    canonical_data: RCCanonicalModel
