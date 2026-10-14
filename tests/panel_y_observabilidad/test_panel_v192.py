# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v192.py (reorganización por tema: panel_y_observabilidad).
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
    # v1.9.8: el descarte lleva también las coordenadas del evento (punto 10).
    assert vistos == [("studio", "prod", "equipo excluido", "patente=864035052734572",
                       (9.913503, -84.679345))]


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
# JavaScript del panel: ejecuta, no solo compila
# ═══════════════════════════════════════════════════════════════════════════

def test_el_js_del_panel_v192_ejecuta():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node no está disponible en este entorno")
    raiz = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    r = subprocess.run(["node", os.path.join("tools", "panel", "verificar_panel_v192.js")],
                       cwd=raiz, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
