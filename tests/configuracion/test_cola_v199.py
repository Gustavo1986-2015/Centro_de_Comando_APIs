# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque3.py (reorganización por tema: configuracion).
"""
v1.9.9 · Bloque 3 — Visor y panel.

3.1 Edición de celdas del visor: el valor nuevo se escribe, con confirmación,
    validación de tipo, registro de quién cambió qué y el aviso de modo
    simulado / activación (B-13).
3.2 Descarga "Enviados": solo bases de cola.
3.3 B-17 Aceptación PUSH filtrada por integración.
3.4 Aceptación PUSH de Schmitz: solo lo aceptado (test en test_v196_salud_push).
3.5 B-18 Motor de cola: solo SQLite; otro valor existente, con aviso.
3.6 _escAttr en todo atributo del panel.
3.7 db/descartes.db no es "Residual".

Todos EJECUTAN el código (endpoints reales y JS real del panel).
"""
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]


def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════════
# 3.5 B-18, 3.6 escape de atributos, 3.7 descartes.db
# ═══════════════════════════════════════════════════════════════════════════

def test_35_motor_de_cola_solo_sqlite_y_otro_valor_con_aviso(tmp_path):
    r = _node({"cargar": ["_escapeHtml", "_escAttr"], "llamadas": [
        {"fn": "_selectorMotorCola", "args": [{"_originalIdx": 1, "queue_backend": "sqlite"}]},
        {"fn": "_selectorMotorCola", "args": [{"_originalIdx": 2, "queue_backend": "redis"}]},
    ]}, tmp_path)
    sqlite, redis = r["resultados"]
    assert sqlite.count("<option") == 1 and 'value="sqlite"' in sqlite and "postgres" not in sqlite.lower()
    assert 'value="redis" selected' in redis and "no implementado" in redis and "aviso-motor-cola" in redis
