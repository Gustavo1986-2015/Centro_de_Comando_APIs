# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque1.py (reorganización por tema: proveedores/schmitz).
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
# 1.4 B-19 y 1.5 B-28 — Schmitz: ignición y velocidad
# ═══════════════════════════════════════════════════════════════════════════

def _schmitz(**cambios):
    """El crudo real de los tests (SCHMITZ_REAL), con lo que el caso cambie."""
    from tests.proveedores.schmitz.test_schmitz_v192 import SCHMITZ_REAL
    p = copy.deepcopy(SCHMITZ_REAL)
    sd = p["StatusData"][0]
    sd.setdefault("Position", {})
    sd.setdefault("SensorStatus", {})
    sd.setdefault("EBS", {})
    for clave, valor in cambios.items():
        bloque, campo = clave.split("__")
        if valor is None:
            sd[bloque].pop(campo, None)
        else:
            sd[bloque][campo] = valor
    return p


def _mapear(p):
    from app.providers.schmitz.mapper import map_schmitz_payload
    return map_schmitz_payload(p)[0]


def test_b19_sin_is_ignition_on_la_ignicion_queda_sin_dato():
    from app.services.rc_soap import construir_evento_rc
    ev = _mapear(_schmitz(SensorStatus__IsIgnitionOn=None, SensorStatus__IsInMotion={"exists": True, "Value": True}))
    assert ev.ignition is None, "se dedujo la ignición del movimiento"
    assert "ignition" not in construir_evento_rc(ev)
    assert _mapear(_schmitz(SensorStatus__IsIgnitionOn=False)).ignition is False


def test_b19_gps_speed_cero_medido_es_cero():
    ev = _mapear(_schmitz(Position__GPSSpeed={"exists": True, "Value": 0}, EBS__Velocity="50"))
    assert ev.speed == 0.0, "un 0 medido se reemplazó por EBS.Velocity"


def test_b19_ebs_solo_si_gps_speed_no_vino():
    assert _mapear(_schmitz(Position__GPSSpeed=None, EBS__Velocity="50")).speed == 50.0
    assert _mapear(_schmitz(Position__GPSSpeed={"exists": False, "Value": 0}, EBS__Velocity="50")).speed == 50.0
    assert _mapear(_schmitz(Position__GPSSpeed={"exists": True, "Value": 84}, EBS__Velocity="50")).speed == 84.0


def test_b28_velocidad_sin_medir_vacia_en_la_base_y_cero_a_rc():
    from app.services.rc_soap import construir_evento_rc
    ev = _mapear(_schmitz(Position__GPSSpeed={"exists": False, "Value": 0}, EBS__Velocity=None))
    assert ev.speed is None, "la base tiene que quedar vacía (el panel muestra N/A)"
    assert construir_evento_rc(ev)["speed"] == "0", "a RC tiene que llegarle 0: es obligatoria"


def test_cableado_b28_schmitz_por_su_endpoint(config_aislada):
    """Camino real de Schmitz: la fila queda con velocidad NULL."""
    from app.api.routers import schmitz
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    schmitz._persist_batch([(_schmitz(Position__GPSSpeed={"exists": False, "Value": 0}, EBS__Velocity=None),
                             "prod", "v0")])
    db = get_session("schmitz", "prod")
    velocidades = {e.speed for e in db.query(NormalizedRCEvent).all()}
    db.close()
    assert velocidades == {None}
