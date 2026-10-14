# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v191.py (reorganización por tema: ingesta).
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


# ═══════════════════════════════════════════════════════════════════════════
# Rastro de los 404
# ═══════════════════════════════════════════════════════════════════════════

def test_un_proveedor_desconocido_deja_rastro(webhook_tive, monkeypatch):
    """
    En la prueba de Tive, los 404 solo se veían como líneas de acceso y hubo
    que adivinar la URL. Ahora queda registrado el nombre exacto que llegó.
    """
    from app.api.routers import dynamic_webhook

    vistos = []
    monkeypatch.setattr(dynamic_webhook, "registrar_rechazo",
                        lambda p, e, motivo, detalle="": vistos.append(motivo))
    r = webhook_tive.post("/webhook/dynamic/inexistente", json={"a": 1})
    assert r.status_code == 404
    assert vistos and "inexistente" in vistos[0]
