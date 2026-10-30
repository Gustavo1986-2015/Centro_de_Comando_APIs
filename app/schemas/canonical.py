from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import Optional
from datetime import datetime
import re

class RCCanonicalModel(BaseModel):
    """
    Modelo canónico para datos de Recurso Confiable (RC).
    Todos los adaptadores de proveedores deben mapear a este esquema.
    """
    model_config = ConfigDict(extra='ignore', from_attributes=True)
    
    # Opcional en el modelo para poder representar un evento SIN patente y
    # descartarlo con aviso (app/core/contrato.py). Antes era obligatorio y el
    # mapeador dinámico lo rellenaba con "UNKNOWN", que viajaba a RC.
    chassis_number: Optional[str] = Field(None, description="ChassisNumber o Plate")

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
    # v1.9.10: detalle de la velocidad estimada (solo Tive, con su interruptor).
    # No viaja a RC (a RC va solo `speed`) y no sale en model_dump: se guarda
    # en la cola para el panel, el visor y el JSON RC.
    detalle_velocidad: Optional[dict] = Field(None, exclude=True, description="Detalle de la velocidad estimada")
    # v1.9.11: evento sin posición (Tive no pudo ubicar el equipo). El módulo
    # que lo permite marca `admite_sin_posicion`; el contrato lo acepta sin
    # latitud ni longitud y marca `sin_posicion`. En la cola quedan NULL; a RC
    # va "0" porque el contrato exige el campo. Ninguno de los dos viaja.
    admite_sin_posicion: bool = Field(False, exclude=True, description="El módulo admite enviarlo sin posición")
    sin_posicion: bool = Field(False, exclude=True, description="Aceptado sin posición")
    # v1.9.11: el diccionario EXACTO que el cliente SOAP le entregó a zeep para
    # este evento (JSON), tomado en rc_soap al enviarlo. En simulado, el que se
    # habría enviado. El worker lo guarda en la columna rc_payload.
    rc_payload: Optional[str] = Field(None, exclude=True, description="Lo entregado a zeep (JSON)")
