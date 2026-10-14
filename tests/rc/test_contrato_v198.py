# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v198.py (reorganización por tema: rc).
"""
v1.9.8 — Protección de datos hacia RC, panel y visor de base de datos.

Todos los tests EJECUTAN el código (endpoints, worker, módulo de Tive, JS real
del panel): ninguno busca texto en el fuente.
"""
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 2. B-3 — Batería fuera de 0-100 y odómetro negativo se omiten
# ═══════════════════════════════════════════════════════════════════════════

def test_sanear_mediciones_omite_solo_lo_imposible():
    from types import SimpleNamespace
    from app.core.contrato import sanear_mediciones
    casos = [((-1, -1), (None, None)), ((101, 5), (None, 5)), ((0, 0), (0, 0)), ((100, 12.5), (100, 12.5)),
             ((24.6, 1000), (24.6, 1000)),   # Schmitz: tensión de 12-28 V, no cambia
             ((None, None), (None, None))]
    for (bateria, odometro), esperado in casos:
        ev = SimpleNamespace(battery=bateria, odometer=odometro)
        sanear_mediciones(ev)
        assert (ev.battery, ev.odometer) == esperado, (bateria, odometro)


def test_construir_evento_rc_no_manda_mediciones_imposibles_ya_encoladas():
    """Defensa para filas que ya estaban en la cola antes de la v1.9.8."""
    from app.models.db_models import NormalizedRCEvent
    from app.services.rc_soap import construir_evento_rc
    base = dict(provider="protrack", chassis_number="P1", latitude=1.0, longitude=2.0, speed=0,
                code="1", date=datetime(2026, 10, 8, 12, 0, 0))
    malo = construir_evento_rc(NormalizedRCEvent(**base, battery=-1, odometer=-5))
    assert "battery" not in malo and "odometer" not in malo
    assert construir_evento_rc(NormalizedRCEvent(**base, battery=150, odometer=1)).get("battery") is None
    bueno = construir_evento_rc(NormalizedRCEvent(**base, battery=0, odometer=0))
    assert (bueno["battery"], bueno["odometer"]) == (0, 0)


async def test_cableado_protrack_con_bateria_y_odometro_negativos(config_aislada, monkeypatch):
    """Camino PULL real: el evento se encola igual, sin batería ni odómetro."""
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    from app.services.rc_soap import construir_evento_rc
    from app.worker import processor, pull_engine
    from tests.proveedores.protrack.test_protrack_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    assert (PROTRACK_ITEM["battery"], PROTRACK_ITEM["odometer"]) == (-1, -1)
    await pull_engine.process_and_enqueue("protrack", "prod", {"code": 0, "record": [dict(PROTRACK_ITEM)]},
                                          PROTRACK_MAPEO, enable_state_dedup=False)
    db = get_session("protrack", "prod")
    filas = db.query(NormalizedRCEvent).all()
    db.close()
    assert filas, "el evento tiene que salir igual"
    for fila in filas:   # la posición y los eventos de las reglas de disparo
        assert fila.chassis_number == "864035052734572"
        assert (fila.battery, fila.odometer) == (None, None)
        rc = construir_evento_rc(fila)
        assert "battery" not in rc and "odometer" not in rc
