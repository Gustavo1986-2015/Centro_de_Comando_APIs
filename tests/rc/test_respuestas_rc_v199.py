# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque1.py (reorganización por tema: rc).
"""
v1.9.9 · Bloque 1 — Integridad de datos hacia RC.

1.1 B-8   Studio sin patente: se descarta con aviso para el prestador.
1.2 B-15  Respuesta parcial de RC: un evento sin su respuesta se reintenta.
1.3 B-24  Coordenadas 0,0: se envían igual, con aviso en consola.
1.4 B-19  Schmitz: ignición sin IsIgnitionOn = sin dato; GPSSpeed=0 medido es 0.
1.5 B-28  Schmitz: velocidad sin medir vacía en la base, "0" a RC.

Todos EJECUTAN el código (mapeadores, webhooks, worker, cliente de RC, JS
real del panel): ninguno busca texto en el fuente.
"""
import asyncio
import copy
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import descartes


# ═══════════════════════════════════════════════════════════════════════════
# 1.2 B-15 — Respuesta parcial de RC
# ═══════════════════════════════════════════════════════════════════════════

def _eventos(n):
    from datetime import datetime
    from app.schemas.canonical import RCCanonicalModel
    return [RCCanonicalModel(chassis_number=f"P{i}", latitude=1.0, longitude=2.0, code="1",
                             date=datetime(2026, 10, 10, 12, 0, i)) for i in range(n)]


def _respuestas_rc(monkeypatch, respuestas):
    from app.services import rc_soap
    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", lambda self, ev: respuestas)
    return rc_soap.RCSOAPClient(username="u", password="p", use_mock=False)


def test_b15_un_evento_sin_su_respuesta_no_hereda_la_de_otro(monkeypatch):
    from app.services.rc_soap import RCResponseCategory
    cliente = _respuestas_rc(monkeypatch, [{"exception": None, "idJob": 11}, {"exception": None, "idJob": 12}])
    r = asyncio.run(cliente.send_events_batch(_eventos(3)))
    assert [(x[0], x[1], x[3]) for x in r[:2]] == [(True, "11", RCResponseCategory.SUCCESS),
                                                     (True, "12", RCResponseCategory.SUCCESS)]
    assert r[2][0] is False and r[2][3] == RCResponseCategory.TRANSPORT, "heredó la respuesta del primero"
    assert "no tuvo la suya" in r[2][2]


def test_b15_la_respuesta_global_de_rc_sigue_valiendo_para_todo_el_lote(monkeypatch):
    """Lo que RC hace hoy (una respuesta para el lote entero) no cambia."""
    from app.services.rc_soap import RCResponseCategory
    cliente = _respuestas_rc(monkeypatch, [{"exception": None, "idJob": 7033068595}])
    r = asyncio.run(cliente.send_events_batch(_eventos(3)))
    assert [(x[0], x[1], x[3]) for x in r] == [(True, "7033068595", RCResponseCategory.SUCCESS)] * 3


def test_cableado_b15_el_worker_reintenta_el_que_no_tuvo_respuesta(config_aislada, monkeypatch):
    from app.api.routers import schmitz
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent
    from app.services import rc_soap
    from app.worker import processor
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          use_mock=False, rc_user="AC_avl_SchmitzCargoBull", rc_password_enc=encrypt("x")))
    db.commit()
    db.close()
    schmitz._persist_batch([({"ChassisNumber": f"R{i}", "DeviceTime": "2026-10-04T22:23:36Z",
                              "StatusData": [{"Position": {"Latitude": 42.44, "Longitude": -3.49}}]}, "prod", f"b{i}")
                            for i in range(3)])
    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync",
                        lambda self, ev: [{"exception": None, "idJob": 21}, {"exception": None, "idJob": 22}])
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    asyncio.run(processor.process_provider_events("schmitz", "prod"))
    db = get_session("schmitz", "prod")
    filas = sorted((e.status, e.job_id or "", e.retry_count or 0) for e in db.query(NormalizedRCEvent).all())
    db.close()
    assert [f[0] for f in filas].count("sent") == 2
    (pendiente,) = [f for f in filas if f[0] != "sent"]
    assert pendiente[0] == "pending" and pendiente[2] == 1, f"no quedó para reintento: {pendiente}"
