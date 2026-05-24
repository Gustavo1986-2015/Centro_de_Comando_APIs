from typing import Any, Dict
from datetime import datetime, timezone
import dateutil.parser
from app.schemas.canonical import RCCanonicalModel

def parse_date_to_utc0(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        # Parsear la fecha y convertirla a UTC explícitamente
        dt = dateutil.parser.isoparse(date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def map_schmitz_payload(payload: Dict[str, Any]) -> RCCanonicalModel:
    """
    Mapea un payload crudo de webhook de Schmitz al modelo canónico de RC.
    """
    # Función auxiliar para extracción segura
    def get_safe(data: Dict, keys: list, default=None):
        curr = data
        for k in keys:
            if isinstance(curr, dict) and k in curr:
                curr = curr[k]
            elif isinstance(curr, list) and isinstance(k, int) and len(curr) > k:
                curr = curr[k]
            else:
                return default
        return curr

    # Extraer el ChassisNumber o Plate
    chassis_number = payload.get("ChassisNumber") or payload.get("Plate", "UNKNOWN")

    status_data_0 = get_safe(payload, ["StatusData", 0], {})
    position = get_safe(status_data_0, ["Position"], {})
    ebs = get_safe(status_data_0, ["EBS"], {})
    sensor_status = get_safe(status_data_0, ["SensorStatus"], {})
    tci = get_safe(status_data_0, ["TCI"], {})
    temp = get_safe(status_data_0, ["Temp"], {})
    reefer_comp1 = get_safe(status_data_0, ["Reefer", "Compartment1"], {})
    system_config = get_safe(payload, ["SystemConfig"], {})
    reason = get_safe(payload, ["Reason"], {})

    # Extracción de valores
    latitude = position.get("Latitude")
    longitude = position.get("Longitude")
    
    speed = get_safe(position, ["GPSSpeed", "Value"]) or ebs.get("Velocity")
    
    code_val = reason.get("ItemElementName") or "1"
    code = str(code_val)
    
    device_time_str = payload.get("DeviceTime")
    date_val = parse_date_to_utc0(device_time_str)

    altitude = position.get("Altitude")
    battery = get_safe(sensor_status, ["Battery", "ExternalPowerSupplyVoltage"])
    course = position.get("GPSHeading")
    humidity = tci.get("Humidity")
    ignition = sensor_status.get("IsIgnitionOn")
    
    odometer = ebs.get("Milage") or get_safe(position, ["GPSMilage", "Value"])
    
    temperature = temp.get("Temp1") or get_safe(reefer_comp1, ["ReturnAirTemp", "Value"])
    
    serial_number = str(payload.get("CtuId")) if payload.get("CtuId") is not None else None
    shipment = str(payload.get("ExternalOrderReference")) if payload.get("ExternalOrderReference") is not None else None
    vehicle_type = str(system_config.get("TrailerType")) if system_config.get("TrailerType") is not None else None
    vehicle_brand = str(system_config.get("TrailerProducer")) if system_config.get("TrailerProducer") is not None else None
    vehicle_model = str(system_config.get("TelematicType")) if system_config.get("TelematicType") is not None else None

    return RCCanonicalModel(
        chassis_number=chassis_number,
        latitude=latitude,
        longitude=longitude,
        speed=speed,
        code=code,
        date=date_val,
        altitude=altitude,
        battery=battery,
        course=course,
        humidity=humidity,
        ignition=ignition,
        odometer=odometer,
        temperature=temperature,
        serial_number=serial_number,
        shipment=shipment,
        vehicle_type=vehicle_type,
        vehicle_brand=vehicle_brand,
        vehicle_model=vehicle_model
    )
