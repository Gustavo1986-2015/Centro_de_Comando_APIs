"""
Mapper de datos Protrack365 → RCCanonicalModel.

Combina:
  - Un registro de /api/track (posición en tiempo real)
  - Información del dispositivo de /api/device/list (nombre, patente, tipo)
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.schemas.canonical import RCCanonicalModel


# ────────────────────────────────────────────────────────────────────────────
# Tablas de referencia
# ────────────────────────────────────────────────────────────────────────────

# datastatus → descripción textual
DATA_STATUS_MAP = {
    1: "never_online",
    2: "ok",
    3: "expired",
    4: "offline",
    5: "blocked",
}


def _unix_to_utc(unix_ts: Optional[int]) -> Optional[datetime]:
    """Convierte un UNIX timestamp (int) a datetime UTC-aware."""
    if unix_ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
    except Exception:
        return None


def _parse_ignition(accstatus: Any) -> Optional[bool]:
    """
    accstatus:
      1  → ACC ON  → True
      0  → ACC OFF → False
     -1  → Sin estado → None
    """
    if accstatus == 1:
        return True
    if accstatus == 0:
        return False
    return None  # -1 o None: sin info


def map_protrack_track(
    track: Dict[str, Any],
    device_info: Optional[Dict[str, Any]] = None,
) -> RCCanonicalModel:
    """
    Mapea un registro de /api/track (+ info opcional de /api/device/list)
    al modelo canónico RC.

    Regla de chassis_number:
      1. platenumber del dispositivo (si no está vacío)
      2. IMEI como fallback (cuando platenumber está vacío o ausente)
    """
    device_info = device_info or {}

    # ── Identificadores ──────────────────────────────────────────────────
    imei: str = str(track.get("imei", "")).strip()
    platenumber: str = str(device_info.get("platenumber", "")).strip()

    # chassis_number: patente si disponible, IMEI como fallback
    chassis_number = platenumber if platenumber else imei

    # ── Posición ─────────────────────────────────────────────────────────
    latitude: Optional[float] = track.get("latitude")
    longitude: Optional[float] = track.get("longitude")
    course: Optional[float] = track.get("course")

    # speed en km/h (Protrack entrega int km/h)
    speed_raw = track.get("speed")
    try:
        speed: Optional[float] = float(speed_raw) if speed_raw is not None else None
    except (TypeError, ValueError):
        speed = None

    # ── Tiempo ───────────────────────────────────────────────────────────
    # Preferir gpstime (tiempo GPS real) sobre systemtime (tiempo del servidor)
    gpstime = track.get("gpstime") or track.get("systemtime")
    date = _unix_to_utc(gpstime)

    # ── Estado y batería ─────────────────────────────────────────────────
    ignition = _parse_ignition(track.get("accstatus"))

    battery_raw = track.get("battery")
    try:
        battery: Optional[float] = float(battery_raw) if battery_raw is not None and int(battery_raw) != -1 else None
    except (TypeError, ValueError):
        battery = None

    # datastatus como código de evento (string)
    datastatus = track.get("datastatus")
    code = DATA_STATUS_MAP.get(datastatus, str(datastatus)) if datastatus is not None else None

    # ── Información del dispositivo ───────────────────────────────────────
    vehicle_type: Optional[str] = str(device_info.get("devicetype", "")).strip() or None
    vehicle_model: Optional[str] = str(device_info.get("devicename", "")).strip() or None
    vehicle_brand: Optional[str] = None  # Protrack no expone marca fabricante

    return RCCanonicalModel(
        chassis_number=chassis_number,
        latitude=latitude,
        longitude=longitude,
        speed=speed,
        code=code,
        date=date,
        altitude=None,          # Protrack /api/track no devuelve altitud
        battery=battery,
        course=course,
        humidity=None,          # No disponible en Protrack
        ignition=ignition,
        odometer=None,          # No disponible en /api/track
        temperature=None,       # No disponible en /api/track
        serial_number=imei,     # IMEI siempre como serial_number del hardware
        shipment=None,
        vehicle_type=vehicle_type,
        vehicle_brand=vehicle_brand,
        vehicle_model=vehicle_model,
    )
