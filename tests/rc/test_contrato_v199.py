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
# 1.3 B-24 — Coordenadas 0,0
# ═══════════════════════════════════════════════════════════════════════════

def test_b24_cero_cero_se_envia_igual_con_aviso(caplog):
    from datetime import datetime
    from app.core.contrato import filtrar_validos
    from app.schemas.canonical import RCCanonicalModel
    cero = RCCanonicalModel(chassis_number="K393478", latitude=0.0, longitude=0.0, code="ShockEvents",
                            date=datetime(2026, 10, 10, 12, 0, 0))
    una = RCCanonicalModel(chassis_number="R1", latitude=0.0, longitude=-58.4, code="1",
                           date=datetime(2026, 10, 10, 12, 0, 0))
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        assert filtrar_validos([cero, una], "tive", "prod") == [cero, una], "0,0 se envía igual"
    avisos = [r.getMessage() for r in caplog.records if "Coordenadas 0,0" in r.getMessage()]
    assert len(avisos) == 1, "una sola coordenada en 0 no avisa"
    for dato in ("probable GPS sin señal", "tive/prod", "K393478", "ShockEvents", "2026-10-10T12:00:00"):
        assert dato in avisos[0], dato


def test_cableado_b24_schmitz_cero_cero_se_encola_y_avisa(config_aislada, caplog):
    from app.api.routers import schmitz
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        schmitz._persist_batch([({"ChassisNumber": "R0", "DeviceTime": "2026-10-04T22:23:36Z",
                                  "StatusData": [{"Position": {"Latitude": 0, "Longitude": 0}}]}, "prod", "z0")])
    db = get_session("schmitz", "prod")
    assert db.query(NormalizedRCEvent).count() == 1
    db.close()
    assert "Coordenadas 0,0 — probable GPS sin señal" in caplog.text and "schmitz/prod" in caplog.text
