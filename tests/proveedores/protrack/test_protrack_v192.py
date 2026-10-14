# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v192.py (reorganización por tema: proveedores/protrack).
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


@pytest.fixture(autouse=True)
def caches_limpios():
    from app.providers.schmitz import mapper
    admision.reset()
    mapper._STATE_CACHE.clear()
    yield
    admision.reset()
    mapper._STATE_CACHE.clear()


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
