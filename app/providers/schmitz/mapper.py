from typing import Any, Dict
from datetime import datetime, timezone
import dateutil.parser
from app.schemas.canonical import RCCanonicalModel

# Cache de estados para deteccion de cambios entre payloads del mismo remolque.
# Formato: { "chassis_number": { "IsCoupled": True, "IsDoor1Open": False, ... } }
# Formato: { "chassis_number": { "IsCoupled": True, "IsDoor1Open": False, ... } }
# Se reinicia al reiniciar el servidor. El primer payload siempre genera estados nuevos.
_STATE_CACHE: Dict[str, Dict[str, Any]] = {}

def parse_date_to_utc0(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = dateutil.parser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def unwrap_ex(obj, default=None):
    """
    Desenvuelve el patron Schmitz {exists: bool, Value: any}.
    NO modifica get_safe() — se usa como wrapper explicito.

    Correcto:   unwrap_ex(get_safe(position, ["GPSSpeed"]))
    Incorrecto: modificar get_safe() internamente con auto-unwrap
    (get_safe(pos, ["GPSSpeed", "Value"]) ya funciona sin modificar nada).
    """
    if isinstance(obj, dict) and 'exists' in obj and 'Value' in obj:
        return obj['Value']
    return obj if obj is not None else default


def map_schmitz_payload(payload: Dict[str, Any], headers: Dict[str, str] = None) -> list:
    """
    Mapea un payload crudo de Schmitz a UNA LISTA de RCCanonicalModel.
    Retorna entre 1 y N eventos segun las reglas del hub:

      1. Evento base: prioriza array Events[], fallback a Reason.
      2. Cambios de estado de sensores (IsCoupled, IsDoor1Open, DoorLocking, AlarmWire)
      4. Sabotaje TAPA cuando alguno de sus campos booleanos es True

    El caller (_persist_batch) debe iterar la lista resultante.
    """

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

    # ── Extraer bloques del payload ───────────────────────────────────────────
    chassis_number = payload.get("Plate") or payload.get("ChassisNumber")
    if not chassis_number:
        raise ValueError("Activo desconocido: ChassisNumber y Plate estan vacios o no existen en el payload")
        
    status_data_0  = get_safe(payload, ["StatusData", 0], {})
    position       = get_safe(status_data_0, ["Position"], {})
    ebs            = get_safe(status_data_0, ["EBS"], {})
    sensor_status  = get_safe(status_data_0, ["SensorStatus"], {})
    tci            = get_safe(status_data_0, ["TCI"], {})
    temp           = get_safe(status_data_0, ["Temp"], {})
    system_config  = get_safe(payload, ["SystemConfig"], {})
    reason         = get_safe(payload, ["Reason"], {})
    events_array   = payload.get("Events") or []

    # ── Extraer valores base ──────────────────────────────────────────────────
    latitude  = position.get("Latitude")
    longitude = position.get("Longitude")
    altitude  = position.get("Altitude")
    course    = position.get("GPSHeading")

    # Velocidad: GPSSpeed como LongEx {exists, Value} o fallback a EBS.Velocity (string)
    speed_raw = unwrap_ex(get_safe(position, ["GPSSpeed"])) or ebs.get("Velocity")
    try:
        speed = float(speed_raw) if speed_raw is not None and str(speed_raw).strip().lower() not in ("", "null", "none") else 0.0
    except Exception:
        speed = 0.0

    # Odometro: EBS.Milage (prioridad) o GPSMilage.Value (fallback)
    odometer    = ebs.get("Milage") or unwrap_ex(get_safe(position, ["GPSMilage"]))
    battery     = get_safe(sensor_status, ["Battery", "ExternalPowerSupplyVoltage"])

    # Ignicion: IsIgnitionOn (bool directo del spec) o IsInMotion.Value como proxy
    ignition    = sensor_status.get("IsIgnitionOn")
    if ignition is None:
        ignition = unwrap_ex(sensor_status.get("IsInMotion"))

    humidity    = tci.get("Humidity")

    # Temperatura: Temp1 como primera opcion, Temp2 como fallback
    # NOTA: DoorTemp NO existe en el spec real de Schmitz — era un bug del mapper anterior
    temperature = temp.get("Temp1") or temp.get("Temp2")

    date_val      = parse_date_to_utc0(payload.get("DeviceTime"))
    serial_number = str(payload.get("CtuId")) if payload.get("CtuId") is not None else None
    shipment      = str(payload.get("ExternalOrderReference")) if payload.get("ExternalOrderReference") else None
    vehicle_type  = str(system_config.get("TrailerType"))     if system_config.get("TrailerType")     else None
    vehicle_brand = str(system_config.get("TrailerProducer")) if system_config.get("TrailerProducer") else None
    vehicle_model = str(system_config.get("TelematicType"))   if system_config.get("TelematicType")   else None

    # ── Constructor de evento canonico ────────────────────────────────────────
    def build_event(code: str) -> RCCanonicalModel:
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
            vehicle_model=vehicle_model,
        )

    result = []

    # ── 1. Evento base: priorizar Events, fallback a Reason ───────────────────
    events_to_process = []
    if events_array:
        for ev in events_array:
            if isinstance(ev, dict) and ev.get("Type"):
                events_to_process.append(str(ev.get("Type")).strip())
    
    if not events_to_process:
        reason_code = str(reason.get("ItemElementName") or "Standard")
        events_to_process.append(reason_code)

    for ev_type in events_to_process:
        result.append(build_event(ev_type))

    # ── 2. Estados de sensores fisicos (solo cuando cambian) ─────────────────
    # El cache persiste en memoria entre payloads del mismo remolque.
    # El primer payload de cada remolque siempre genera eventos (cache vacio).
    cache = _STATE_CACHE.setdefault(chassis_number, {})

    # IsCoupled: enganche/desenganche con la tractora
    is_coupled = sensor_status.get("IsCoupled")
    if is_coupled is not None:
        if cache.get("IsCoupled") != is_coupled:
            cache["IsCoupled"] = is_coupled
            result.append(build_event(f"IsCoupled.{is_coupled}"))

    # IsDoor1Open: sensor fisico de puerta de carga
    # Cubre TODAS las aperturas, no solo las que Schmitz clasifica como alarma.
    # DoorAlarm y Door1.Open son complementarios — no se duplican.
    is_door1_open = sensor_status.get("IsDoor1Open")
    if is_door1_open is None:
        is_door1_open = sensor_status.get("IsDoorOpen")   # campo generico como fallback
    if is_door1_open is not None:
        if cache.get("IsDoor1Open") != is_door1_open:
            cache["IsDoor1Open"] = is_door1_open
            result.append(build_event("Door1.Open" if is_door1_open else "Door1.Closed"))

    # DoorLocking.State: cerradura electronica de las puertas de carga
    # Distinto de IsDoor1Open: este es el estado de la cerradura, no de la puerta fisica.
    door_locking = get_safe(sensor_status, ["DoorLocking"], {})
    dl_state = door_locking.get("State") if isinstance(door_locking, dict) else None
    if dl_state is not None:
        if cache.get("DoorLocking.State") != dl_state:
            cache["DoorLocking.State"] = dl_state
            result.append(build_event(f"DoorLocking.State.{dl_state}"))

    # AntiTheft.AlarmWire: cable fisico de alarma
    # AlarmWire.Open = cable cortado = posible robo en curso
    anti_theft = get_safe(sensor_status, ["AntiTheft"], {})
    alarm_wire = anti_theft.get("AlarmWire") if isinstance(anti_theft, dict) else None
    if alarm_wire is not None:
        if cache.get("AlarmWire") != alarm_wire:
            cache["AlarmWire"] = alarm_wire
            result.append(build_event(f"AlarmWire.{alarm_wire}"))

    # ── 4. Sabotaje TAPA: solo cuando el campo es True ────────────────────────
    # Nunca se genera evento cuando es False — ese es el estado normal y esperado.
    sabotage = get_safe(status_data_0, ["Tapa", "SabotageDetection"], {})
    if isinstance(sabotage, dict):
        sabotage_map = {
            "EbsDisconnect":                    "Sabotaje.EbsDesconectado",
            "CisBatteryGuardCanDisconnect":     "Sabotaje.BateriaBloqueada",
            "DoorlockingSystemLinDisconnected": "Sabotaje.CerraduraDesconectada",
            "BcuDisconnected":                  "Sabotaje.BcuDesconectado",
            "AlarmSystemDisconnected":           "Sabotaje.SistemaAlarmaDesconectado",
            "CoupledSensorDisconnected":         "Sabotaje.SensorEngancheDesconectado",
        }
        for field, rc_code in sabotage_map.items():
            if sabotage.get(field) is True:
                result.append(build_event(rc_code))

    return result
