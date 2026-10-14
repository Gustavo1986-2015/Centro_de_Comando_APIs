# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v191.py (reorganización por tema: rc).
"""
v1.9.1 — Reglas anidadas, filtro de admisión e integridad del payload a RC.

Todo sale de la prueba real de Tive del 01/10/2026:

  · El webhook trajo 171 eventos y solo 6 tenían envío: el filtro de admisión
    deja entrar solo lo que la integración necesita.
  · Dos eventos eran los ejemplos del botón de prueba de Tive (cuenta -1,
    equipo SAMPLEDEVICEID): sin filtro, llegarían a RC como un camión real.
  · El envío de las alertas viene anidado en Alert.ShipmentId, y las reglas
    solo leían el primer nivel (deuda B1).
  · El hub mandaba a RC "ignition: false" para trackers que no miden
    ignición. El contrato D-TI-15 v14 la define OPCIONAL y pide "en blanco o
    0" cuando no hay dato: "false" afirma algo que nadie midió.
  · El botón "JSON RC" del panel no mostraba lo que se envía de verdad.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import admision
from app.core.dynamic_mapper import DynamicMapper
from app.services.rc_soap import construir_evento_rc


# ═══════════════════════════════════════════════════════════════════════════
# Filtro de admisión
# ═══════════════════════════════════════════════════════════════════════════

FILTRO_TIVE = {"admision": [
    {"field": "ShipmentId || Shipment.Id || Alert.ShipmentId", "operator": "exists",
     "label": "solo eventos con envío"},
    {"field": "AccountId", "operator": "neq", "value": "-1",
     "label": "descartar muestras del botón de prueba"},
]}


@pytest.fixture(autouse=True)
def contadores_limpios():
    admision.reset()
    yield
    admision.reset()


# ─── Cableado: que el filtro se aplique de verdad en PUSH y PULL ────────────

@pytest.fixture
def webhook_tive(tmp_path, monkeypatch):
    """
    Router del webhook dinámico con una integración del Studio filtrada, sin firma.

    Desde la v1.9.2 Tive tiene módulo dedicado; el filtro de admisión sigue
    siendo una capacidad genérica del Integration Studio, así que se prueba con
    un proveedor genérico configurado como estaba Tive.
    """
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import database
    from app.core import crypto, rate_limit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASTER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()

    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent

    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="studio", env="prod", provider_type="push", is_active=True,
        use_mock=True, webhook_auth_secret_enc=encrypt("CLAVE"),
        mapping_schema={
            "base_mapping": {"chassis_number": "DeviceName", "latitude": "Location.Latitude",
                             "longitude": "Location.Longitude", "date": "EntryTimeUtc",
                             "shipment": "ShipmentId || Shipment.Id || Alert.ShipmentId"},
            **FILTRO_TIVE,
        },
        rc_user="u", rc_password_enc=encrypt("p"),
    ))
    db.commit()
    db.close()
    NormalizedRCEvent.metadata.create_all(bind=database.get_engine("studio", "prod"))
    database.check_and_migrate_provider_db("studio", "prod")

    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    yield TestClient(app)

    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()


BASE = {"EntryTimeUtc": "2026-10-01T15:29:39Z", "Location": {"Latitude": 20.6, "Longitude": -100.1}}


def test_el_json_rc_del_panel_es_lo_que_se_envia():
    """
    Antes el panel armaba su propia versión: odómetro, altitud y humedad en 0
    cuando se omitían, fecha con Z, ignición "false" y campos de vehículo que
    nunca se mandan. Ahora delega en la misma función que el envío.
    """
    from app.api.routers.dashboard import _formato_rc
    from app.models.db_models import NormalizedRCEvent

    fila = NormalizedRCEvent(
        chassis_number="Q92951", code="1", date=datetime(2026, 10, 1, 15, 29, 39),
        latitude=20.6, longitude=-100.1, speed=0.0, ignition=None, battery=92,
        temperature=20.69, serial_number="869267076302806", shipment="4600410383",
    )
    assert _formato_rc(fila) == construir_evento_rc(fila)

    panel = _formato_rc(fila)
    for inventado in ("ignition", "odometer", "altitude", "humidity",
                      "vehicleType", "vehicleBrand", "vehicleModel"):
        assert inventado not in panel, f"El panel muestra {inventado}, que no se envía"
    assert not panel["date"].endswith("Z")


def test_el_endpoint_del_panel_usa_la_funcion_del_envio(webhook_tive):
    """
    Cableado, no solo lógica. El test de arriba prueba _formato_rc, pero no
    que el panel la llame: con el panel armando su propio JSON a mano, ese
    test seguía en verde. Este pide los datos al endpoint real del panel y
    compara contra lo que se enviaría a RC.
    """
    import asyncio

    from app.api.routers.dashboard import get_stats_data
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent

    webhook_tive.post("/webhook/dynamic/studio", headers={"x-api-key": "CLAVE"},
                      json={**BASE, "AccountId": 10471, "DeviceName": "Q92951",
                            "ShipmentId": "4600410383"})

    datos = asyncio.run(get_stats_data())
    evento_panel = next(e for e in datos["recent"] if e.get("rc_format", {}).get("asset") == "Q92951")

    db = get_session("studio", "prod")
    fila = db.query(NormalizedRCEvent).filter_by(chassis_number="Q92951").first()
    db.close()

    assert evento_panel["rc_format"] == construir_evento_rc(fila), (
        "El JSON RC del panel no es lo que se envía a RC"
    )
    assert "ignition" not in evento_panel["rc_format"]
    assert evento_panel["ignition"] == "N/A"


def test_el_envio_real_usa_la_funcion_unica(monkeypatch):
    """
    Cableado: lo que _send_batch_sync le entrega a zeep es exactamente
    construir_evento_rc de cada evento. v1.9.9 (B-20): se EJECUTA con un cliente
    zeep de captura (antes se buscaba el texto en el fuente).
    """
    from datetime import datetime
    from app.schemas.canonical import RCCanonicalModel
    from app.services import rc_soap
    capturado = []

    class _Servicio:
        def GPSAssetTracking(self, token, datos):
            capturado.append(datos)
            return []

    class _Zeep:
        service = _Servicio()

    monkeypatch.setattr(rc_soap.RCSOAPClient, "_get_token_sync", lambda self: "TOKEN")
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_get_zeep_client", classmethod(lambda cls, endpoint: _Zeep()))
    eventos = [RCCanonicalModel(chassis_number="P1", latitude=1.0, longitude=2.0, code="1", ignition=None,
                                date=datetime(2026, 10, 10, 12, 0, 0)),
               RCCanonicalModel(chassis_number="P2", latitude=3.0, longitude=4.0, code="11", ignition=True,
                                date=datetime(2026, 10, 10, 12, 0, 1))]
    rc_soap.RCSOAPClient(username="u", password="p", use_mock=False)._send_batch_sync(eventos)
    assert capturado == [{"Event": [rc_soap.construir_evento_rc(e) for e in eventos]}]
    assert "ignition" not in capturado[0]["Event"][0], "un dato desconocido no se manda como false"
