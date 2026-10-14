# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v192.py (reorganización por tema: rc).
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
    # v1.9.9 (B-8): sin patente ni identificador, el evento se descarta en el
    # mapeo (antes salía con patente None y lo descartaba el contrato). Nunca "UNKNOWN".
    assert DynamicMapper.map_payload({"lat": 1}, {"chassis_number": "placa", "latitude": "lat"}) is None


def test_sin_patente_el_imei_ya_no_sale_como_patente():
    """
    v1.9.9 (B-8). Antes: sin el campo de patente se usaba el IMEI como patente
    (este test se llamaba test_el_imei_sigue_siendo_respaldo_del_identificador
    y aseguraba ese comportamiento). Ahora el evento se descarta.
    """
    assert DynamicMapper.map_payload({"imei": "864035052734572"}, {"latitude": "lat"}) is None


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
