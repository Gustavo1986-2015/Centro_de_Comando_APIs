# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v192.py (reorganización por tema: proveedores/schmitz).
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


@pytest.fixture(autouse=True)
def caches_limpios():
    from app.providers.schmitz import mapper
    admision.reset()
    mapper._STATE_CACHE.clear()
    yield
    admision.reset()
    mapper._STATE_CACHE.clear()


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
