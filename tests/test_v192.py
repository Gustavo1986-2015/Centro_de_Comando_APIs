"""
v1.9.2 — Mejoras transversales: validación única del contrato, sin UNKNOWN,
datos de vehículo, humedad con decimales, velocidad N/A, panel.

Cada cambio de código compartido se prueba con las configuraciones y payloads
REALES de Protrack y Schmitz:
  · PROTRACK_MAPEO es el esquema de protrack/test de la base local (el de
    protrack/prod está vacío). recuperar_protrack.yaml no está en el repo: sus
    cuatro reglas son las mismas que ya prueba test_v191.py.
  · PROTRACK_ITEM es un registro real (audit/protrack_prod/2026-08/, 07/08).
  · SCHMITZ_REAL es un payload real (audit/schmitz_test/2026-08/, 10/08).
"""
import copy
import logging
import math
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import admision, contrato
from app.core.dynamic_mapper import DynamicMapper
from app.services.rc_soap import construir_evento_rc

PROTRACK_MAPEO = {
    "base_mapping": {"chassis_number": "imei", "latitude": "latitude", "longitude": "longitude",
                     "speed": "speed", "date": "gpstime", "ignition": "accstatus",
                     "temperature": "0", "odometer": "odometer", "battery": "battery",
                     "altitude": "0", "course": "course", "humidity": "0"},
    "trigger_rules": [
        {"id": "rule_mqpo30a5", "field": "doorstatus", "operator": "eq", "value": "1", "rc_code": "10",
         "label": "Puerta de Cabina Abierta", "enabled": True, "dedup_key": "doorstatus"},
        {"id": "rule_mqpo6hx2", "field": "accstatus", "operator": "eq", "value": "1", "rc_code": "11",
         "label": "Motor Encendido", "enabled": True, "dedup_key": "accstatus"},
        {"id": "rule_mqpo6yph", "field": "accstatus", "operator": "eq", "value": "0", "rc_code": "12",
         "label": "Motor Apagado", "enabled": True, "dedup_key": "accstatus"},
        {"id": "rule_mrc5czjz", "field": "doorstatus", "operator": "eq", "value": "0", "rc_code": "09",
         "label": "Puerta de Cabina Cerrada", "enabled": True, "event_type": "state", "dedup_key": "doorstatus"},
    ],
    "default_rule": {"enabled": True, "rc_code": "1", "label": "Reporte GPS", "fire_when": "always"},
}

PROTRACK_ITEM = {
    "chargestatus": -1, "fuel": "", "latitude": 9.913503, "battery": -1, "speed": 0,
    "hearttime": 1786102276, "temperature": [], "course": 271, "temperaturetime": 0, "acctime": 415,
    "systemtime": 1786101909, "longitude": -84.679345, "oilpowerstatus": -1, "mileage": -1,
    "todaymileage": -1, "odometer": -1, "externalpower": "", "servertime": 1786102333,
    "accstatus": 0, "datastatus": 2, "fueltime": 0, "doorstatus": 0, "imei": "864035052734572",
    "gpstime": 1786102275, "defencestatus": -1,
}

SCHMITZ_REAL = {
    "ChassisNumber": "TEST-027", "Plate": "TEST-027", "CtuId": 11262712,
    "ReferenceUserName": "ASSISTCARGO", "ReceiveTime": "2026-08-10T17:08:54Z",
    "DeviceTime": "2026-08-10T18:08:54.0000000+01:00",
    "Reason": {"Item": True, "ItemElementName": "IgnitionAlarm"},
    "Events": [{"Type": "IgnitionAlarm", "Value": None}],
    "StatusData": [{
        "Position": {"GPSDateTime": "2026-08-10T17:08:54Z", "Latitude": 46.013432, "Longitude": 7.053484,
                     "GPSHeading": "124", "Altitude": 264, "GPSSpeed": {"exists": True, "Value": 63},
                     "GPSMilage": {"exists": True, "Value": 44611.212}},
        "EBS": {"Velocity": "63", "Milage": 44611.212, "Signal": "Off"},
        "SensorStatus": {"IsCoupled": True, "IsDoor1Open": False, "IsIgnitionOn": True,
                         "IsInMotion": {"exists": True, "Value": True},
                         "Battery": {"ExternalPowerSupplyVoltage": 12.8, "ExternalPowerSupplyVoltageSpecified": True},
                         "DoorLocking": {"State": "Closed", "ContactSensor": True, "CmdSource": "Portal"},
                         "AntiTheft": {"AlarmWire": "Closed",
                                       "DoorOpenAlarm": {"IsPresent": False, "EnabledState": "Enabled"}}},
        "Temp": {"Temp1": 19.7, "Temp2": 19.8}, "TCI": {"FuelLevel": {"FuelLevel": "38"}},
        "Tapa": {"Active": True, "TapaVehicleStop": False,
                 "SabotageDetection": {"EbsDisconnect": False, "CisBatteryGuardCanDisconnect": False,
                                       "DoorlockingSystemLinDisconnected": False, "BcuDisconnected": False,
                                       "AlarmSystemDisconnected": False, "CoupledSensorDisconnected": False}},
    }],
    "SystemConfig": {"TrailerType": "TIPPER", "TrailerProducer": "SCHMITZ_CARGOBULL_AG",
                     "TelematicType": "CTU_3", "HasCouplingSensor": True, "HasDoorSensor1": True,
                     "HasIgnitionSignal": True},
}


def _ev(**campos):
    base = dict(chassis_number="AB123", code="1", date=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
                latitude=-34.6, longitude=-58.4, speed=None, course=None, ignition=None,
                altitude=None, battery=None, humidity=None, odometer=None, temperature=None,
                serial_number=None, shipment=None, vehicle_type=None, vehicle_brand=None,
                vehicle_model=None)
    base.update(campos)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def caches_limpios():
    from app.providers.schmitz import mapper
    admision.reset()
    mapper._STATE_CACHE.clear()
    yield
    admision.reset()
    mapper._STATE_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════════
# 3.2 — Validación única del contrato
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("campos, falta", [
    ({}, []),
    ({"chassis_number": None}, ["patente"]),
    ({"chassis_number": "  "}, ["patente"]),
    ({"chassis_number": "UNKNOWN"}, ["patente"]),
    ({"date": None}, ["fecha"]),
    ({"latitude": None}, ["latitud"]),
    ({"longitude": None}, ["longitud"]),
    ({"latitude": float("nan")}, ["latitud"]),
    ({"chassis_number": None, "date": None, "latitude": None, "longitude": None},
     ["patente", "fecha", "latitud", "longitud"]),
])
def test_los_obligatorios_del_contrato(campos, falta):
    assert contrato.faltantes(_ev(**campos)) == falta


def test_una_coordenada_cero_es_un_dato_real():
    assert contrato.faltantes(_ev(latitude=0.0, longitude=0.0)) == []


def test_la_velocidad_no_entra_en_la_validacion():
    """El contrato pide "0" si no hay dato: lo pone rc_soap."""
    assert contrato.faltantes(_ev(speed=None)) == []
    assert construir_evento_rc(_ev(speed=None))["speed"] == "0"


def test_el_descarte_dice_proveedor_patente_y_campo(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        assert contrato.filtrar_validos([_ev(latitude=None, serial_number="868")], "protrack", "prod") == []
    assert "[PROTRACK-prod]" in caplog.text
    assert "falta latitud" in caplog.text
    assert "patente=AB123" in caplog.text and "serie=868" in caplog.text


# ─── 3.1 — Sin UNKNOWN ─────────────────────────────────────────────────────

def test_sin_identificador_no_sale_unknown_ni_nada():
    """
    El caso real del 02/10 15:44: un contenedor sin nombre de equipo salió
    como UNKNOWN. Sin identificador real, no se envía.
    """
    esquema = {"base_mapping": {"chassis_number": "DeviceName", "latitude": "Location.Latitude",
                                "longitude": "Location.Longitude", "date": "EntryTimeUtc"}}
    payload = {"DeviceName": None, "EntryTimeUtc": "2026-10-02T15:44:03Z",
               "Location": {"Latitude": 3.3092916667, "Longitude": -78.6166416667}}
    assert DynamicMapper.map_payload_multi(payload, esquema, "studio", "prod") == []


def test_map_payload_ya_no_rellena_con_unknown():
    ev = DynamicMapper.map_payload({"lat": 1}, {"chassis_number": "placa", "latitude": "lat"})
    assert ev.chassis_number is None


def test_el_imei_sigue_siendo_respaldo_del_identificador():
    """Compatibilidad: sin patente mapeada se usaba el imei, y se sigue usando."""
    ev = DynamicMapper.map_payload({"imei": "864035052734572"}, {"latitude": "lat"})
    assert ev.chassis_number == "864035052734572"
    assert ev.serial_number == "864035052734572"


# ─── Regresión: Protrack con su configuración real ────────────────────────

def test_protrack_real_sale_igual_que_siempre():
    """
    Protrack no se toca: el registro real con su esquema real produce los
    mismos eventos que en la v1.9.1 (posición + motor apagado + puerta
    cerrada), con los mismos datos.
    """
    eventos = DynamicMapper.map_payload_multi(copy.deepcopy(PROTRACK_ITEM), PROTRACK_MAPEO,
                                              "protrack", "test", False, False)
    assert [e.code for e in eventos] == ["1", "12", "09"]
    for e in eventos:
        assert e.chassis_number == "864035052734572"
        assert (e.latitude, e.longitude) == (9.913503, -84.679345)
        assert e.date == datetime.fromtimestamp(1786102275, tz=timezone.utc)
        assert e.speed == 0.0, "una velocidad medida en 0 sigue siendo 0, no N/A"
        assert e.ignition is False
        assert e.course == 271.0


def test_protrack_sin_posicion_ya_no_sale_frente_a_africa(caplog):
    item = dict(PROTRACK_ITEM, latitude=None, longitude=None)
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        assert DynamicMapper.map_payload_multi(item, PROTRACK_MAPEO, "protrack", "test", False, False) == []
    assert "falta latitud, longitud" in caplog.text and "864035052734572" in caplog.text


def test_protrack_sin_fecha_no_sale_con_la_hora_del_envio():
    item = dict(PROTRACK_ITEM, gpstime=None)
    assert DynamicMapper.map_payload_multi(item, PROTRACK_MAPEO, "protrack", "test", False, False) == []


# ─── Regresión: Schmitz con su mapeo propio y un payload real ─────────────

def test_schmitz_real_sale_igual_que_siempre():
    from app.providers.schmitz.mapper import map_schmitz_payload
    eventos = map_schmitz_payload(copy.deepcopy(SCHMITZ_REAL), env="test")
    codigos = [e.code for e in eventos]
    assert codigos[0] == "IgnitionAlarm"
    assert "IsCoupled.True" in codigos and "Door1.Closed" in codigos
    ev = eventos[0]
    assert ev.chassis_number == "TEST027"
    assert ev.date == datetime(2026, 8, 10, 17, 8, 54, tzinfo=timezone.utc)
    assert (ev.latitude, ev.longitude, ev.speed) == (46.013432, 7.053484, 63.0)
    assert (ev.vehicle_type, ev.vehicle_brand, ev.vehicle_model) == ("TIPPER", "SCHMITZ_CARGOBULL_AG", "CTU_3")


def test_schmitz_sin_fecha_se_descarta_y_no_consume_el_cambio_de_estado(caplog):
    """
    Si el pulso inválido actualizara la caché, el cambio de puerta no saldría
    con el siguiente pulso válido: se perdería un evento real.
    """
    from app.providers.schmitz.mapper import map_schmitz_payload
    invalido = copy.deepcopy(SCHMITZ_REAL)
    invalido["DeviceTime"] = None
    invalido["StatusData"][0]["SensorStatus"]["IsDoor1Open"] = True
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        assert map_schmitz_payload(invalido, env="prod") == []
    assert "[SCHMITZ-prod]" in caplog.text and "falta fecha" in caplog.text

    valido = copy.deepcopy(SCHMITZ_REAL)
    valido["StatusData"][0]["SensorStatus"]["IsDoor1Open"] = True
    assert "Door1.Open" in [e.code for e in map_schmitz_payload(valido, env="prod")]


def test_schmitz_sin_posicion_se_descarta():
    from app.providers.schmitz.mapper import map_schmitz_payload
    sin_pos = copy.deepcopy(SCHMITZ_REAL)
    sin_pos["StatusData"][0]["Position"] = {}
    assert map_schmitz_payload(sin_pos) == []


# ─── Cableado: TODOS los caminos de entrada validan al ingresar ───────────

@pytest.fixture
def bases_temporales(tmp_path, monkeypatch):
    from app import database
    from app.core import rate_limit, safety_net

    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()
    monkeypatch.setattr(safety_net, "DIRECTORIO_BASE", str(tmp_path / "red"))
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()
    yield database
    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()


def _filas(database, provider, env):
    from app.models.db_models import NormalizedRCEvent
    database.check_and_migrate_provider_db(provider, env)
    db = database.get_session(provider, env)
    try:
        return [(f.chassis_number, f.code, f.latitude) for f in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()


def test_cableado_pull(bases_temporales):
    import asyncio
    from app.worker import pull_engine
    sin_pos = dict(PROTRACK_ITEM, imei="864035052733962", latitude=None, longitude=None)
    asyncio.run(pull_engine.process_and_enqueue("protrack", "test", [copy.deepcopy(PROTRACK_ITEM), sin_pos],
                                                PROTRACK_MAPEO, enable_state_dedup=False))
    filas = _filas(bases_temporales, "protrack", "test")
    assert {f[0] for f in filas} == {"864035052734572"}, "Entró a la cola un evento sin posición"


def test_cableado_schmitz(bases_temporales):
    from app.api.routers import schmitz
    sin_fecha = dict(copy.deepcopy(SCHMITZ_REAL), DeviceTime=None, ChassisNumber="OTRO1", Plate="OTRO1")
    schmitz._persist_batch([(copy.deepcopy(SCHMITZ_REAL), "test", "iid-1"), (sin_fecha, "test", "iid-2")])
    assert {f[0] for f in _filas(bases_temporales, "schmitz", "test")} == {"TEST027"}


def test_cableado_schmitz_red_de_seguridad(bases_temporales):
    import asyncio
    from app.api.routers import schmitz
    sin_fecha = dict(copy.deepcopy(SCHMITZ_REAL), DeviceTime=None)
    asyncio.run(schmitz.persistir_desde_red_de_seguridad("schmitz", "test", [(sin_fecha, "iid-3")]))
    assert _filas(bases_temporales, "schmitz", "test") == []


def test_cableado_webhook_dinamico_y_su_red_de_seguridad(bases_temporales):
    import asyncio
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig

    database = bases_temporales
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="studio", env="prod", provider_type="push", is_active=True,
                          use_mock=True, webhook_auth_secret_enc=encrypt("CLAVE"),
                          mapping_schema=PROTRACK_MAPEO, enable_state_dedup=False))
    db.commit()
    db.close()
    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    cliente = TestClient(app)

    r = cliente.post("/webhook/dynamic/studio", json=dict(PROTRACK_ITEM, gpstime=None),
                     headers={"x-api-key": "CLAVE"})
    assert r.status_code == 200 and r.json()["events_count"] == 0
    assert _filas(database, "studio", "prod") == []

    asyncio.run(dynamic_webhook.persistir_desde_red_de_seguridad(
        "studio", "prod", [(dict(PROTRACK_ITEM, latitude=None), "iid-4")]))
    assert _filas(database, "studio", "prod") == []

    r = cliente.post("/webhook/dynamic/studio", json=PROTRACK_ITEM, headers={"x-api-key": "CLAVE"})
    assert r.json()["events_count"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# 3.3 y 3.4 — Datos de vehículo y humedad, contra el esquema REAL de RC
# ═══════════════════════════════════════════════════════════════════════════

def test_los_datos_de_vehiculo_se_envian_si_existen():
    ev = construir_evento_rc(_ev(vehicle_type="TIPPER", vehicle_brand="SCHMITZ_CARGOBULL_AG",
                                 vehicle_model="CTU_3"))
    assert (ev["vehicleType"], ev["vehicleBrand"], ev["vehicleModel"]) == (
        "TIPPER", "SCHMITZ_CARGOBULL_AG", "CTU_3")


def test_sin_datos_de_vehiculo_no_se_mandan_vacios():
    ev = construir_evento_rc(_ev())
    assert not {"vehicleType", "vehicleBrand", "vehicleModel"} & set(ev)


def test_la_humedad_conserva_los_decimales():
    assert construir_evento_rc(_ev(humidity=55.2))["humidity"] == 55.2


def test_el_evento_real_de_schmitz_serializa_contra_el_esquema_real_de_rc():
    """
    Copia del WSDL y los XSD de RCService.svc bajados el 02/10/2026
    (tests/fixtures/rc_wsdl/). El tipo Event declara vehicleType,
    vehicleBrand, vehicleModel y humidity como xs:string. Se serializa el
    lote exactamente como lo manda _send_batch_sync.
    """
    from lxml import etree
    from zeep import Client

    from app.providers.schmitz.mapper import map_schmitz_payload

    wsdl = os.path.join(os.path.dirname(__file__), "fixtures", "rc_wsdl", "wsdl.xml")
    cliente = Client(wsdl)
    eventos = [construir_evento_rc(e) for e in map_schmitz_payload(copy.deepcopy(SCHMITZ_REAL))]
    eventos.append(construir_evento_rc(_ev(humidity=55.2)))
    sobre = cliente.create_message(cliente.service, "GPSAssetTracking", "token", {"Event": eventos})

    # Por namespace y nombre, no por texto: zeep elige el prefijo (ns1, ns6...)
    # según el orden en que cargó los esquemas.
    ns = {"rc": "http://schemas.datacontract.org/2004/07/IronTracking"}
    nodos = sobre.findall(".//rc:Event", ns)
    assert len(nodos) == len(eventos)
    primero = nodos[0]
    assert primero.findtext("rc:vehicleType", namespaces=ns) == "TIPPER"
    assert primero.findtext("rc:vehicleBrand", namespaces=ns) == "SCHMITZ_CARGOBULL_AG"
    assert primero.findtext("rc:vehicleModel", namespaces=ns) == "CTU_3"
    assert nodos[-1].findtext("rc:humidity", namespaces=ns) == "55.2"
    assert etree.tostring(sobre)  # el sobre completo se serializa sin error


# ═══════════════════════════════════════════════════════════════════════════
# 3.5 — El descarte dice qué equipo era
# ═══════════════════════════════════════════════════════════════════════════

def test_el_descarte_por_admision_nombra_al_equipo(caplog):
    esquema = {"base_mapping": {"chassis_number": "DeviceName", "serial_number": "DeviceId"}}
    payload = {"DeviceName": "Q48548", "DeviceId": "869267076302806"}
    with caplog.at_level(logging.INFO, logger="app.core.admision"):
        admision.registrar_descarte("studio", "prod", "solo eventos con envío",
                                    admision.identidad(payload, esquema))
    assert "patente=Q48548" in caplog.text and "serie=869267076302806" in caplog.text


def test_el_resumen_por_minuto_tambien_nombra_a_los_equipos(caplog, monkeypatch):
    for placa in ("A1", "A2", "A3"):
        admision.registrar_descarte("studio", "prod", "motivo", f"patente={placa}")
    monkeypatch.setattr(admision.time, "time", lambda: 10 ** 10)
    with caplog.at_level(logging.INFO, logger="app.core.admision"):
        admision.registrar_descarte("studio", "prod", "motivo", "patente=A4")
    assert "patente=A2" in caplog.text and "patente=A4" in caplog.text


def test_sin_identificador_lo_dice():
    assert admision.identidad({"x": 1}, {"base_mapping": {"chassis_number": "placa"}}) == \
        "sin identificador en el payload"


def test_el_webhook_registra_al_equipo_descartado(bases_temporales, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig

    database = bases_temporales
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="studio", env="prod", provider_type="push", is_active=True,
                          use_mock=True, webhook_auth_secret_enc=encrypt("CLAVE"),
                          mapping_schema={**PROTRACK_MAPEO, "admision": [
                              {"field": "imei", "operator": "neq", "value": "864035052734572",
                               "label": "equipo excluido"}]}))
    db.commit()
    db.close()
    vistos = []
    monkeypatch.setattr(admision, "registrar_descarte", lambda *a: vistos.append(a))
    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    TestClient(app).post("/webhook/dynamic/studio", json=PROTRACK_ITEM, headers={"x-api-key": "CLAVE"})
    assert vistos == [("studio", "prod", "equipo excluido", "patente=864035052734572")]


# ═══════════════════════════════════════════════════════════════════════════
# 3.6 — Velocidad N/A cuando no se mide
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_velocidad_queda_en_none_y_a_rc_le_llega_cero():
    esquema = {"base_mapping": {"chassis_number": "placa", "latitude": "lat", "longitude": "lon",
                                "date": "fecha", "speed": "velocidad"}}
    (ev,) = DynamicMapper.map_payload_multi(
        {"placa": "AB1", "lat": 1.0, "lon": 2.0, "fecha": "2026-10-02T10:00:00Z"}, esquema)
    assert ev.speed is None
    assert construir_evento_rc(ev)["speed"] == "0"


def test_la_columna_de_velocidad_admite_nulo():
    from app.models.db_models import NormalizedRCEvent
    assert NormalizedRCEvent.__table__.c.speed.nullable


# ═══════════════════════════════════════════════════════════════════════════
# Panel: etiqueta de evento, "solo eventos", velocidad, coordenadas en 0
# ═══════════════════════════════════════════════════════════════════════════

def test_el_panel_etiqueta_y_filtra_los_eventos(bases_temporales):
    import asyncio
    from app.api.routers.dashboard import get_stats_data
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent

    database = bases_temporales
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add_all([ProviderConfig(provider_name="schmitz", env="prod", mapping_schema={}),
                ProviderConfig(provider_name="studio", env="prod", mapping_schema=PROTRACK_MAPEO),
                ProviderConfig(provider_name="tive", env="prod", mapping_schema={})])
    db.commit()
    db.close()
    filas = {
        "schmitz": [("TEST027", "Standard"), ("TEST027", "DoorAlarm")],
        "studio": [("AB1", "1"), ("AB1", "12")],
        "tive": [("K1234567", "1"), ("K393478", "ShockEvents"), ("K393478", "TemperatureMin-FIN")],
    }
    for prov, eventos in filas.items():
        database.check_and_migrate_provider_db(prov, "prod")
        db = database.get_session(prov, "prod")
        for placa, codigo in eventos:
            db.add(NormalizedRCEvent(provider=prov, status="pending", chassis_number=placa, code=codigo,
                                     latitude=0.0, longitude=0.0, speed=None, raw_data="{}",
                                     date=datetime(2026, 10, 2, 15, 0)))
        db.commit()
        db.close()

    todos = asyncio.run(get_stats_data())["recent"]
    eventos = {(e["chassis"], e["code"]) for e in todos if e["es_evento"]}
    assert eventos == {("TEST027", "DoorAlarm"), ("AB1", "12"), ("K393478", "ShockEvents"),
                       ("K393478", "TemperatureMin-FIN")}
    assert all(e["speed"] is None for e in todos), "la velocidad no medida no es 0"
    assert all(e["coords"] == "0.0, 0.0" for e in todos), "una coordenada 0 no es 'Sin GPS'"

    solo = asyncio.run(get_stats_data(solo_eventos=True))["recent"]
    assert {(e["chassis"], e["code"]) for e in solo} == eventos


# ═══════════════════════════════════════════════════════════════════════════
# Persistencia de los interruptores: migración y respaldo YAML
# ═══════════════════════════════════════════════════════════════════════════

def test_la_migracion_agrega_module_options_a_una_base_vieja(tmp_path, monkeypatch):
    import sqlite3
    from app import database

    monkeypatch.chdir(tmp_path)
    os.makedirs("db")
    con = sqlite3.connect("db/system_config_global.db")
    con.execute("CREATE TABLE provider_config (id INTEGER PRIMARY KEY, provider_name TEXT, env TEXT)")
    con.commit()
    con.close()
    database.check_and_migrate_db()
    con = sqlite3.connect("db/system_config_global.db")
    columnas = {r[1] for r in con.execute("PRAGMA table_info(provider_config)")}
    con.close()
    assert "module_options" in columnas


def test_el_respaldo_lleva_los_interruptores_y_valida_al_importar():
    from fastapi import HTTPException
    from app.api.routers import config_backup

    assert config_backup.CAMPOS_SIMPLES["opciones_modulo"] == "module_options"
    config_backup._validar_proveedor({"nombre": "tive", "entorno": "prod",
                                      "opciones_modulo": {"alertas_trackers": True}}, 0)
    with pytest.raises(HTTPException):
        config_backup._validar_proveedor({"nombre": "tive", "entorno": "prod",
                                          "opciones_modulo": {"alertas_trackers": "si"}}, 0)
    with pytest.raises(HTTPException):
        config_backup._validar_proveedor({"nombre": "tive", "entorno": "prod",
                                          "opciones_modulo": "todo"}, 0)


# ═══════════════════════════════════════════════════════════════════════════
# JavaScript del panel: ejecuta, no solo compila
# ═══════════════════════════════════════════════════════════════════════════

def test_el_js_del_panel_v192_ejecuta():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node no está disponible en este entorno")
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(["node", os.path.join("tools", "verificar_panel_v192.js")],
                       cwd=raiz, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
