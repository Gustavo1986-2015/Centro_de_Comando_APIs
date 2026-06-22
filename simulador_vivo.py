"""
simulador_vivo.py v3 — Schmitz YAML-Compliant — Assistcargo
Genera payloads fieles al spec Schmitz Push API v1.35.
Incluye Events[], IsCoupled, IsDoor1Open, DoorLocking,
AntiTheft.AlarmWire y Tapa.SabotageDetection.
Apunta al endpoint oficial /Json/Data con header X-Data-Type: Status.
"""

import requests, time, random, datetime, json
from concurrent.futures import ThreadPoolExecutor

# ==============================================================================
# ⚙️ CONFIGURACIÓN DEL SIMULADOR (Modificá estos parámetros rápidos)
# ==============================================================================

# 1. MODO DE EJECUCIÓN (True = Ráfagas masivas concurrentes / False = Envío lento normal)
MODO_ESTRES = True

# 2. CONFIGURACIÓN MODO NORMAL (MODO_ESTRES = False)
PLACAS_NORMALES          = ["RHR5776", "GDG8486", "JMC1236", "AB1234"]
SEGUNDOS_ESPERA_NORMAL   = 5  # Tiempo que espera el simulador entre cada ciclo

# 3. CONFIGURACIÓN MODO ESTRÉS (MODO_ESTRES = True)
PLACAS_ESTRES            = [f"TEST-{str(i).zfill(3)}" for i in range(1, 51)]  # 50 eventos por ciclo
SEGUNDOS_ESPERA_ESTRES   = 1   # Bombardeo rápido cada X segundos

# 4. PUNTO DE ENLACE (URL)
WEBHOOK_URL = "http://127.0.0.1:8000/Json/Data?env=test"

# ==============================================================================

SEGUNDOS_ESPERA = SEGUNDOS_ESPERA_ESTRES if MODO_ESTRES else SEGUNDOS_ESPERA_NORMAL
PLACAS_ACTIVAS  = PLACAS_ESTRES if MODO_ESTRES else PLACAS_NORMALES

REASON_CODES = [
    ("Standard",          60),
    ("DoorAlarm",         10),
    ("IgnitionAlarm",      8),
    ("CouplingAlarm",      6),
    ("VelocityAlarm",      5),
    ("TemperatureAlarm",   5),
    ("UndervoltageAlarm",  3),
    ("EBS24NAlarm",        2),
    ("WatchboxAlarm",      1),
]
REASON_NAMES   = [r[0] for r in REASON_CODES]
REASON_WEIGHTS = [r[1] for r in REASON_CODES]
TRAILER_TYPES  = ["BOX_SEMITRAILER", "CURTAINSIDER", "REFRIGERATED", "TIPPER"]

# Estado persistente por placa para simular transiciones de estado reales
_sim_state: dict[str, dict] = {}

def _get_sim_state(placa: str) -> dict:
    if placa not in _sim_state:
        _sim_state[placa] = {
            "is_coupled":    True,
            "is_door1_open": False,
            "door_locking":  "Closed",
            "alarm_wire":    "Closed",
            "trailer_type":  random.choice(TRAILER_TYPES),
        }
    return _sim_state[placa]


def generar_payload(placa: str) -> dict:
    state = _get_sim_state(placa)

    # Simular cambios de estado con probabilidades realistas
    if random.random() < 0.05:    # 5% — enganche/desenganche
        state["is_coupled"] = not state["is_coupled"]
    if random.random() < 0.08:    # 8% — apertura/cierre de puerta fisica
        state["is_door1_open"] = not state["is_door1_open"]
    if random.random() < 0.03:    # 3% — cambio de cerradura electronica
        state["door_locking"] = random.choice(["Closed", "Open", "Intermediate"])
    state["alarm_wire"] = "Open" if random.random() < 0.005 else "Closed"  # 0.5% cable cortado

    seg_pasado  = random.randint(0, 600)
    ahora_utc   = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seg_pasado)
    offset_h    = random.choice([0, 1, 2, 3])
    local_time  = ahora_utc + datetime.timedelta(hours=offset_h)
    offset_str  = "Z" if offset_h == 0 else f"+{offset_h:02d}:00"
    device_time = local_time.strftime(f"%Y-%m-%dT%H:%M:%S.0000000{offset_str}")

    lat    = round(random.uniform(40.0, 58.0), 6)
    lon    = round(random.uniform(-8.0, 25.0), 6)
    speed  = str(random.randint(0, 90))
    course = str(random.randint(0, 359))
    milage = round(random.uniform(5000, 500_000), 3)

    reason_code  = random.choices(REASON_NAMES, weights=REASON_WEIGHTS, k=1)[0]
    trailer_type = state["trailer_type"]
    temp1 = round(
        random.uniform(-25.0, 5.0) if trailer_type == "REFRIGERATED"
        else random.uniform(5.0, 30.0), 1
    )

    # Alarma extra concurrente (15% probabilidad) — distinta al Reason
    other_reasons = [r for r in REASON_NAMES if r != reason_code and r != "Standard"]
    extra_events  = []
    if random.random() < 0.15 and other_reasons:
        extra_events.append({"Type": random.choice(other_reasons), "Value": None})

    ebs_disconnect = random.random() < 0.002   # 0.2% — sabotaje critico, raro

    return {
        # Identificacion
        "ChassisNumber":     placa,
        "Plate":             placa,
        "CtuId":             random.randint(10_000_000, 19_999_999),
        "ReferenceUserName": "ASSISTCARGO",

        # Timestamps
        "ReceiveTime": ahora_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "DeviceTime":  device_time,

        # Motivo principal
        "Reason": {"Item": True, "ItemElementName": reason_code},

        # Alarmas concurrentes (todas las activas en este momento)
        "Events": [
            {"Type": reason_code, "Value": None},
            *extra_events,
        ],

        # Datos de posicion y sensores
        "StatusData": [{
            "Position": {
                "GPSDateTime": ahora_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "Latitude":    lat,
                "Longitude":   lon,
                "GPSHeading":  course,
                "Altitude":    random.randint(0, 1200),
                "GPSSpeed":    {"exists": True, "Value": int(speed)},
                "GPSMilage":   {"exists": True, "Value": milage},
            },
            "EBS": {
                "Velocity": speed,   # string, como lo envia Schmitz real
                "Milage":   milage,
                "Signal":   "Off",
            },
            "SensorStatus": {
                # Estados con change-detection en el mapper
                "IsCoupled":    state["is_coupled"],
                "IsDoor1Open":  state["is_door1_open"],   # sensor fisico de puerta
                "IsIgnitionOn": int(speed) > 0,
                "IsInMotion":   {"exists": True, "Value": int(speed) > 0},
                "Battery": {
                    "ExternalPowerSupplyVoltage":          round(random.uniform(11.5, 14.2), 1),
                    "ExternalPowerSupplyVoltageSpecified": True,
                },
                # Cerradura electronica de carga (distinto del sensor fisico de puerta)
                "DoorLocking": {
                    "State":         state["door_locking"],
                    "ContactSensor": state["door_locking"] == "Closed",
                    "CmdSource":     "Portal",
                },
                # Sistema anti-robo
                "AntiTheft": {
                    "AlarmWire": state["alarm_wire"],
                    "DoorOpenAlarm": {
                        "IsPresent":    reason_code == "DoorAlarm",
                        "EnabledState": "Enabled",
                    },
                },
            },
            "Temp": {
                "Temp1": temp1,
                "Temp2": round(temp1 + random.uniform(-1.0, 1.0), 1),
            },
            "TCI": {
                "FuelLevel": {"FuelLevel": str(random.randint(10, 100))},
            },
            # TAPA: sistema anti-robo europeo
            "Tapa": {
                "Active":          True,
                "TapaVehicleStop": int(speed) == 0,
                "SabotageDetection": {
                    "EbsDisconnect":                    ebs_disconnect,
                    "CisBatteryGuardCanDisconnect":     False,
                    "DoorlockingSystemLinDisconnected": False,
                    "BcuDisconnected":                  False,
                    "AlarmSystemDisconnected":           False,
                    "CoupledSensorDisconnected":         False,
                },
            },
        }],

        # Configuracion del remolque
        "SystemConfig": {
            "TrailerType":       trailer_type,
            "TrailerProducer":   "SCHMITZ_CARGOBULL_AG",
            "TelematicType":     "CTU_3",
            "HasCouplingSensor": True,
            "HasDoorSensor1":    True,
            "HasIgnitionSignal": True,
        },
    }


def enviar_payload(placa: str) -> tuple:
    payload = generar_payload(placa)
    reason  = payload["Reason"]["ItemElementName"]
    extra   = len([e for e in payload.get("Events", []) if e.get("Type") != reason])
    headers = {"X-Data-Type": "Status", "Content-Type": "application/json"}
    try:
        res = requests.post(WEBHOOK_URL, json=payload, headers=headers, timeout=5)
        ok  = res.status_code in [200, 202]
        return ok, None if ok else f"HTTP {res.status_code}", reason, extra
    except Exception as e:
        return False, str(e), reason, extra


placas = PLACAS_ACTIVAS

print(f"{'='*60}")
print(f"  SIMULADOR SCHMITZ v3 — YAML-Compliant")
print(f"  Endpoint: {WEBHOOK_URL}")
print(f"  Modo:     {'ESTRES' if MODO_ESTRES else 'SMOKE TEST'}")
print(f"{'='*60}\n")

while True:
    ts = datetime.datetime.now().strftime("%H:%M:%S")

    if MODO_ESTRES:
        print(f"[{ts}] Rafaga: {len(placas)} vehiculos en paralelo...")
        t0 = time.time()
        ok_c = err_c = 0
        reasons: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=len(placas)) as ex:
            for ok, err, reason, extra in ex.map(enviar_payload, placas):
                if ok:
                    ok_c += 1
                    reasons[reason] = reasons.get(reason, 0) + 1
                else:
                    err_c += 1
                    if err_c <= 3: print(f"  Error: {err}")
        top = ", ".join(f"{r}({n})" for r, n in sorted(reasons.items(), key=lambda x: -x[1])[:3])
        print(f"[{ts}] OK:{ok_c} ERR:{err_c} | {time.time()-t0:.2f}s | {top}")
    else:
        placa = random.choice(placas)
        ok, err, reason, extra = enviar_payload(placa)
        if ok:
            s = _get_sim_state(placa)
            coupled_icon  = "🔗" if s["is_coupled"]    else "⛓️💥"
            door_icon     = "🚪" if s["is_door1_open"] else "🔐"
            lock_icon     = {"Closed": "🔒", "Open": "🔓", "Intermediate": "🔑"}.get(s["door_locking"], "?")
            wire_icon     = "🟢" if s["alarm_wire"] == "Closed" else "🔴CABLE CORTADO"
            extra_str     = f" +{extra}extra" if extra else ""
            print(f"[{ts}] ok {placa:12} | {reason:20}{extra_str} | {coupled_icon}{door_icon}{lock_icon} {wire_icon}")
        else:
            print(f"[{ts}] x  {placa} | {err}")

    time.sleep(SEGUNDOS_ESPERA)
