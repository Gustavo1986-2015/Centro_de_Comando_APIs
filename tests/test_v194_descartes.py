"""
v1.9.4 · Punto 2 — Descartes visibles en el panel (transversal).

Antes cada descarte quedaba solo en la consola y un reinicio se lo llevaba.
Ahora va también a db/descartes.db y se ve en Diagnóstico y Salud: conteo
por integración y motivo, y los últimos con hora, equipo, envío, motivo y
AlertId. Cubre admisión, contrato y módulo de Tive.
"""
import asyncio
import copy
import json
import logging
import os
import queue
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import admision, contrato, descartes

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tive_crudos_2026-10.jsonl")
CRUDOS = {json.loads(l)["origen"]: json.loads(l)["payload"] for l in open(FIXTURE, encoding="utf-8")}


@pytest.fixture(autouse=True)
def registro_aislado(tmp_path, monkeypatch):
    descartes.esperar_escritura(2.0)
    monkeypatch.setattr(descartes, "DIRECTORIO", str(tmp_path / "db"))
    descartes.reset()
    admision.reset()
    yield
    descartes.esperar_escritura(2.0)
    admision.reset()


def _consultar():
    descartes.esperar_escritura(3.0)
    return descartes.consultar(100)


def test_registrar_y_consultar():
    descartes.registrar("tive", "prod", "tive", "posición de tracker, va por RC directo",
                        equipo="K676092", envio=None)
    descartes.registrar("tive", "prod", "tive", "posición de tracker, va por RC directo", equipo="J1")
    descartes.registrar("tive", "prod", "tive", "duplicado de ShockEvents (puntual) ya recibido",
                        equipo="K393478", envio="VIAJE", alert_id="f2769c5b")
    d = _consultar()
    totales = {(r["origen"], r["motivo"]): r["total"] for r in d["resumen"]}
    assert totales[("tive", "posición de tracker, va por RC directo")] == 2
    ultimo = d["ultimos"][0]
    assert (ultimo["equipo"], ultimo["envio"], ultimo["alert_id"]) == ("K393478", "VIAJE", "f2769c5b")
    assert ultimo["ts"] > 0


def test_sobrevive_un_reinicio():
    """Lo guardado sigue ahí con una conexión nueva, como tras reiniciar el hub."""
    descartes.registrar("schmitz", "prod", "contrato", "falta fecha", equipo="R5868BDP")
    descartes.esperar_escritura(3.0)
    con = sqlite3.connect(os.path.join(descartes.DIRECTORIO, descartes.ARCHIVO))
    filas = con.execute("SELECT proveedor, motivo, equipo FROM descartes").fetchall()
    con.close()
    assert filas == [("schmitz", "falta fecha", "R5868BDP")]


def test_tope_de_filas_y_retencion(monkeypatch):
    monkeypatch.setattr(descartes, "MAX_FILAS", 5)
    for i in range(10):
        descartes.registrar("studio", "prod", "admision", "m", equipo=f"E{i}")
    descartes.esperar_escritura(3.0)
    con = sqlite3.connect(os.path.join(descartes.DIRECTORIO, descartes.ARCHIVO))
    con.execute("INSERT INTO descartes (ts, proveedor, env, origen, motivo) VALUES (?, 'v', 'p', 'admision', 'viejo')",
                (time.time() - (descartes.DIAS_RETENCION + 1) * 86400,))
    con.commit()
    descartes._purgar_si_corresponde(con, forzar=True)
    equipos = [r[0] for r in con.execute("SELECT equipo FROM descartes ORDER BY id")]
    viejos = con.execute("SELECT COUNT(*) FROM descartes WHERE motivo = 'viejo'").fetchone()[0]
    con.close()
    assert viejos == 0, "lo vencido no se borró"
    assert equipos == ["E5", "E6", "E7", "E8", "E9"], "el tope no conservó lo más nuevo"


def test_si_la_cola_se_llena_se_cuenta_y_se_avisa(monkeypatch, caplog):
    llena = queue.Queue(maxsize=1)
    llena.put(("x",))
    monkeypatch.setattr(descartes, "_cola", llena)
    monkeypatch.setattr(descartes, "_asegurar_hilo", lambda: None)
    monkeypatch.setattr(descartes, "_ultimo_aviso_perdidos", 0.0)
    with caplog.at_level(logging.WARNING, logger="app.core.descartes"):
        descartes.registrar("tive", "prod", "tive", "m")
    assert descartes._perdidos == 1
    assert "saturado" in caplog.text


# ─── Cableado: los tres orígenes ───────────────────────────────────────────

def test_cableado_contrato():
    ev = SimpleNamespace(chassis_number="R5868BDP", date=None, latitude=1.0, longitude=2.0,
                         serial_number="123052056", code="Standard", shipment="ENV-1")
    assert contrato.filtrar_validos([ev], "schmitz", "prod") == []
    (u,) = _consultar()["ultimos"]
    assert (u["proveedor"], u["origen"], u["motivo"], u["equipo"], u["envio"]) == (
        "schmitz", "contrato", "falta fecha", "R5868BDP", "ENV-1")


def test_cableado_admision():
    admision.registrar_descarte("studio", "prod", "solo eventos con envío", "patente=Q48548")
    admision.registrar_descarte("studio", "prod", "solo eventos con envío", "patente=Q48549")
    d = _consultar()
    assert [r["total"] for r in d["resumen"] if r["origen"] == "admision"] == [2], (
        "la consola resume por minuto, pero la base guarda cada descarte")
    assert {u["equipo"] for u in d["ultimos"]} == {"patente=Q48548", "patente=Q48549"}


def test_cableado_tive(tmp_path, monkeypatch):
    from app.providers.tive import estado, modulo
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    try:
        assert modulo.procesar(copy.deepcopy(CRUDOS["2026-10-01:1"]), "prod", {}, "a") == []
        sin_par = copy.deepcopy(CRUDOS["2026-10-02:167"])
        assert modulo.procesar(sin_par, "prod", {}, "b") == []
        shock = CRUDOS["2026-10-02:123"]
        modulo.procesar(copy.deepcopy(shock), "prod", {"alertas_trackers": True}, "c")
        modulo.procesar(copy.deepcopy(CRUDOS["2026-10-02:124"]), "prod", {"alertas_trackers": True}, "d")
    finally:
        estado.cerrar_todo()
    d = _consultar()
    por_motivo = {u["motivo"]: u for u in d["ultimos"]}
    assert por_motivo["posición de tracker, va por RC directo"]["equipo"] == CRUDOS["2026-10-01:1"]["DeviceName"]
    tramo = por_motivo["tramo de tercero sin patente resoluble: ningún equipo del envío tiene par aprendido"]
    assert tramo["envio"] == "ID 612780-CONTEN TLLU5171893- MSC- DESTINO BOLIVIA"
    assert tramo["equipo"] == "867860087520523"
    assert "TLLU5171893" in tramo["detalle"]
    dup = por_motivo["duplicado de ShockEvents (puntual) ya recibido"]
    assert dup["alert_id"] == CRUDOS["2026-10-02:124"]["Alert"]["AlertId"]
    assert dup["equipo"] == "K393478"


def test_el_endpoint_del_panel_los_devuelve():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard
    from app.core.auth import verify_dashboard_auth

    descartes.registrar("tive", "prod", "tive", "motivo X", equipo="K1", envio="E1", alert_id="A1")
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[verify_dashboard_auth] = lambda: None
    r = TestClient(app).get("/api/diagnostico/descartes?limite=10")
    assert r.status_code == 200
    d = r.json()
    assert d["ultimos"][0]["alert_id"] == "A1" and d["resumen"][0]["motivo"] == "motivo X"
    assert d["retencion_dias"] == descartes.DIAS_RETENCION


def test_el_js_del_panel_v194():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(["node", os.path.join("tools", "verificar_panel_v194.js")], cwd=raiz,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
