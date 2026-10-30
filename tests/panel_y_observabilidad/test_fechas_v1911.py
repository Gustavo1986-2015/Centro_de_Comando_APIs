"""
v1.9.11 · 6.1 — Una sola forma de mostrar fechas en el panel.

Con el idioma del navegador es-419, toLocaleString() da 12 h sin a. m./p. m.
(19:31 se lee 07:31). v1.9.10 lo corrigió en Descartes; ahora todo el panel
usa _fechaHora: AAAA-MM-DD HH:MM:SS en la hora local del navegador.

Se ejecuta el JS REAL con Node en dos husos horarios (TZ), para comprobar que
es hora local y no UTC ni la del servidor.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
JS = RAIZ / "frontend" / "static" / "dashboard.js"


def _node(entrada, tmp_path, tz):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / f"entrada_{tz.replace('/', '_')}.json", tmp_path / f"salida_{tz.replace('/', '_')}.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    entorno = {**os.environ, "TZ": tz, "LANG": "es_419.UTF-8", "LC_ALL": "es_419.UTF-8"}
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60, env=entorno)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))["resultados"]


# 2026-10-23 19:31:43 en Costa Rica (UTC-6) = 2026-10-24 01:31:43 UTC.
TS = 1792805503
ISO = "2026-10-23T19:31:43-06:00"


@pytest.mark.parametrize("tz,esperado", [
    ("America/Costa_Rica", "2026-10-23 19:31:43"),
    ("UTC", "2026-10-24 01:31:43"),
])
def test_fecha_hora_local_24_h_js_real(tmp_path, tz, esperado):
    llamadas = [{"fn": "_fechaHora", "args": [TS]},
                {"fn": "_fechaHora", "args": [ISO]},
                {"fn": "_textoFechaDescarte", "args": [TS]},
                {"fn": "_horaDescarte", "args": [TS]},
                {"fn": "_fechaHora", "args": [None]},
                {"fn": "_fechaHora", "args": [""]},
                {"fn": "_fechaHora", "args": ["no es una fecha"]},
                {"fn": "_horaDescarte", "args": [None]}]
    r = _node({"llamadas": llamadas}, tmp_path, tz)
    assert r[:4] == [esperado] * 4, "epoch, ISO con zona, Descartes y Diagnóstico: el mismo formato"
    assert r[4:] == ["—"] * 4


def test_ninguna_fecha_del_panel_pasa_por_tolocalestring():
    """
    toLocaleString() sigue en uso para NÚMEROS (separador de miles). Ninguna
    fecha: ni new Date(...).toLocaleString() ni toLocaleTimeString/DateString,
    ni un formateo propio fuera de _fechaHora.
    """
    src = JS.read_text(encoding="utf-8")
    assert not re.search(r"new Date\([^)]*\)\.toLocale", src)
    assert "toLocaleTimeString" not in src and "toLocaleDateString" not in src
    # getHours() solo dentro de _fechaHora y del valor de un <input type=datetime-local>.
    usos = [m.start() for m in re.finditer(r"getHours\(\)", src)]
    funciones = [src.rfind("function ", 0, i) for i in usos]
    nombres = {re.match(r"function (\w+)", src[f:]).group(1) for f in funciones}
    assert nombres == {"_fechaHora", "_fechaLocalInput"}, nombres
