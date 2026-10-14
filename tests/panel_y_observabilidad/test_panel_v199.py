# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque3.py (reorganización por tema: panel_y_observabilidad).
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
# 3.3 B-17 — Aceptación PUSH filtrada
# ═══════════════════════════════════════════════════════════════════════════

async def test_33_push_por_integracion_en_el_servidor(config_aislada):
    from app.api.routers import dashboard
    dashboard.reset_push_stats()
    for _ in range(3):
        dashboard.record_push_latency("tive:prod", 0.004)
    dashboard.record_push_latency("schmitz:prod", 0.002)
    dashboard.record_push_latency("schmitz:test", 0.003)
    d = await dashboard.get_stats_data()
    assert d["push_per_provider"]["tive"]["count"] == 3
    assert d["push_per_provider"]["schmitz"]["count"] == 2, "la integración suma sus entornos"
    assert d["push_stats"]["count"] == 5
    dashboard.reset_push_stats()


def test_cableado_33_el_js_real_muestra_la_de_la_integracion(tmp_path):
    datos = {"pending": 0, "sent": 0, "failed": 0, "retries": 0, "recent": [],
             "push_stats": {"avg_ms": 3.0, "compliance_pct": 100, "count": 1035},
             "push_per_provider": {"tive:prod": {"avg_ms": 4.0, "compliance_pct": 100, "count": 734},
                                   "tive": {"avg_ms": 4.0, "compliance_pct": 100, "count": 734}}}
    comun = {"globales": {"currentProviderFilter": "all", "currentStatusFilter": "all", "currentSoloEventos": False,
                          "currentLatencyFilter": "all", "_ultimaRecargaSoloEventos": 0, "allRecentEvents": []},
             "stubs": ["renderRecentTable", "fetchFilteredEvents", "updateSparkline", "applyCardHierarchy",
                       "renderHealthPills", "renderThroughput", "renderMockBanner", "renderProviderPills"]}
    def contar(filtro):
        r = _node(dict(comun, llamadas=[{"set": {"id": "filter-provider", "value": filtro}},
                                        {"fn": "renderStats", "args": [datos]}]), tmp_path)
        return r["elementos"]["push-count"]["textContent"]
    assert contar("all") == "1,035" or contar("all") == "1.035"
    assert contar("tive") in ("734",), "con el filtro de Tive mostró otra cosa"
    assert contar("schmitz") == "0", "sin datos de esa integración tiene que mostrar 0, no el total global"


def test_36_los_atributos_del_panel_resisten_comillas(tmp_path):
    malo = 'a "comillas" <b>'
    r = _node({"cargar": ["_escapeHtml", "_escAttr"], "llamadas": [
        {"fn": "_renderDescartes", "args": [{"resumen": [{"proveedor": "tive", "env": "prod", "origen": "tive",
                                                          "motivo": "m", "total": 1, "ultimo": 1}],
                                             "ultimos": [{"ts": 1, "proveedor": "tive", "motivo": "m", "detalle": malo}],
                                             "retencion_dias": 7, "max_filas": 1}]},
        {"fn": "_credencialesDeModulo", "args": [{"_originalIdx": 1, "module_credentials": {"client_id": malo},
                                                  "modulo_dedicado": True, "usa_credenciales_api": True}]},
        {"fn": "_inputValorRegla", "args": [{"id": "r1", "operator": "eq", "value": malo}]},
    ]}, tmp_path)
    for html in r["resultados"]:
        assert malo not in html, "un atributo quedó con la comilla sin escapar"
        assert "&quot;comillas&quot;" in html
