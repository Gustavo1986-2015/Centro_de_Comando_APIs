"""
v1.9.9 — 3.8: reintento manual de eventos fallidos desde el panel.

Caso real (13/10): una caída de RC dejó 35 eventos en 'failed' y la única forma
de reenviarlos fue SQL a mano en el servidor. Estos tests EJECUTAN el endpoint,
el worker y el JS real del panel.
"""
import asyncio
import glob
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
RESPUESTA_RETRY = ("RC pidió reintentar: key: CGI:SESSION_STORE_UNAVAILABLE, code: 4023, severity: Retry, "
                   "layer: CGI, message: El servicio de autenticación no está disponible temporalmente.")
RESPUESTA_NEGOCIO = "Rechazo de negocio RC: SQL: datos invalidos"


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _cliente():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard, reintentos
    app = FastAPI()
    app.include_router(reintentos.router)
    app.include_router(dashboard.router)
    return TestClient(app)


def _integracion(use_mock=False):
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          use_mock=use_mock, rc_user="AC_avl_SchmitzCargoBull", rc_password_enc=encrypt("x")))
    db.commit()
    db.close()


def _eventos(estados):
    """Un evento de Schmitz por cada (nombre, estado, job_id, respuesta); devuelve {nombre: id}."""
    from app.api.routers import schmitz
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    schmitz._persist_batch([({"ChassisNumber": nombre, "DeviceTime": "2026-10-13T12:00:00Z",
                              "StatusData": [{"Position": {"Latitude": 42.44, "Longitude": -3.49}}]},
                             "prod", f"m-{nombre}") for nombre, *_ in estados])
    db = get_session("schmitz", "prod")
    ids = {}
    for nombre, estado, job_id, respuesta in estados:
        ev = db.query(NormalizedRCEvent).filter_by(chassis_number=nombre).one()
        ev.status, ev.job_id, ev.rc_response = estado, job_id, respuesta
        ev.retry_count = 3 if estado == "failed" else 0
        # Un fallido conserva el próximo intento del último reintento: si no se
        # limpiara, el worker no lo tomaría hasta esa hora.
        ev.next_retry_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=6)
                            if estado == "failed" else None)
        ids[nombre] = ev.id
    db.commit()
    db.close()
    return ids


def _estado(nombre):
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("schmitz", "prod")
    try:
        ev = db.query(NormalizedRCEvent).filter_by(chassis_number=nombre).one()
        return ev.status, ev.retry_count, ev.next_retry_at, ev.job_id
    finally:
        db.close()


CASO = [("FALLA1", "failed", "rc_business_1", RESPUESTA_RETRY.replace("RC pidió reintentar", "Rechazo de negocio RC")),
        ("FALLA2", "failed", "rc_business_2", RESPUESTA_RETRY.replace("RC pidió reintentar", "Rechazo de negocio RC")),
        ("NEGOCIO", "failed", "rc_business_3", RESPUESTA_NEGOCIO),
        ("ENVIADO", "sent", "12345", "OK"),
        ("SIMULADO", "simulado", "mock_1", "mock"),
        ("ENCOLA", "pending", None, None)]


@pytest.fixture
def escenario(config_aislada, monkeypatch):
    """El caso del 13/10: dos fallidos por la caída de RC (key/code), uno de negocio y de los demás estados."""
    from app.worker import processor
    _integracion()
    despertados = []
    monkeypatch.setattr(processor, "trigger_worker", lambda p, e: despertados.append((p, e)))
    return {"ids": _eventos(CASO), "despertados": despertados, "c": _cliente()}


def _ciclo_rc_ok(monkeypatch):
    from app.services import rc_soap
    from app.worker import processor
    llamadas = []

    def rc(self, ev):
        llamadas.append(len(ev))
        return [{"exception": None, "idJob": 900 + i} for i in range(len(ev))]

    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", rc)
    asyncio.run(processor.process_provider_events("schmitz", "prod"))
    return llamadas


# ─── Servidor ────────────────────────────────────────────────────────────────

def test_el_resumen_agrupa_por_integracion_y_motivo(escenario):
    c = escenario["c"]
    assert c.get("/api/reintentos/fallidos").status_code == 401
    r = c.get("/api/reintentos/fallidos", auth=_auth())
    assert r.status_code == 200
    datos = r.json()
    grupos = {g["motivo"]: g for g in datos["grupos"]}
    assert datos["total"] == 3
    caida = grupos["CGI:SESSION_STORE_UNAVAILABLE (4023)"]
    # Con job_id de negocio (como quedaron el 13/10 en v1.9.8), pero RC había
    # pedido reintentar: transitorio, no se marca como permanente.
    assert (caida["integracion"], caida["cantidad"], caida["categoria"]) == ("schmitz/prod", 2, "TRANSPORT")
    assert caida["permanente"] is False and caida["aviso"] == ""
    negocio = grupos["BUSINESS"]
    assert negocio["cantidad"] == 1 and negocio["permanente"] is True
    assert negocio["aviso"] == "Va a volver a fallar salvo que se corrija el dato."
    assert "ids" not in caida, "el resumen no expone los ids"


def test_un_failed_vuelve_a_pending_y_el_worker_lo_envia(escenario, monkeypatch, caplog):
    c, ids = escenario["c"], escenario["ids"]
    with caplog.at_level(logging.INFO):
        r = c.post("/api/reintentos/reintentar", auth=_auth(),
                   json={"provider": "schmitz", "env": "prod", "ids": [ids["FALLA1"]], "cantidad_confirmada": 1})
    assert r.status_code == 200 and r.json()["reintentados"] == 1
    assert _estado("FALLA1")[:3] == ("pending", 0, None)
    assert escenario["despertados"] == [("schmitz", "prod")], "no despertó al worker"

    # El resultado anterior de RC queda registrado en el respaldo de procesados.
    (archivo,) = glob.glob(os.path.join("db", "backups_diarios", "schmitz_prod", "*", "procesados_*.jsonl"))
    (registro,) = [json.loads(l) for l in open(archivo, encoding="utf-8")]
    assert (registro["id"], registro["status"], registro["job_id"]) == (ids["FALLA1"], "failed", "rc_business_1")
    assert "SESSION_STORE_UNAVAILABLE" in registro["response"]
    assert registro["reintento_manual"]["usuario"] == os.environ["DASHBOARD_USER"]

    # Registro de acciones administrativas.
    linea = next(r.getMessage() for r in caplog.records if "action=reintento_manual" in r.getMessage())
    assert f"user={os.environ['DASHBOARD_USER']}" in linea
    assert '"cantidad": 1' in linea and '"schmitz/prod": 1' in linea and '"fecha":' in linea

    # Y el worker lo envía.
    assert _ciclo_rc_ok(monkeypatch) == [2], "tenía que mandar el reintentado y el que estaba en cola"
    assert _estado("FALLA1")[0] == "sent"


def test_sent_simulado_y_pendientes_nunca_se_tocan(escenario):
    c, ids = escenario["c"], escenario["ids"]
    antes = {n: _estado(n) for n in ("ENVIADO", "SIMULADO", "ENCOLA", "FALLA1")}
    pedir = lambda lista, n: c.post("/api/reintentos/reintentar", auth=_auth(), json={
        "provider": "schmitz", "env": "prod", "ids": [ids[x] for x in lista], "cantidad_confirmada": n})
    assert pedir(["ENVIADO", "SIMULADO", "ENCOLA"], 3).status_code == 409
    assert pedir(["ENVIADO"], 1).status_code == 409
    assert pedir(["ENVIADO", "FALLA1"], 2).status_code == 409, "aceptó una cantidad que incluía un 'sent'"
    assert {n: _estado(n) for n in antes} == antes, "un pedido rechazado cambió algo"
    assert escenario["despertados"] == []
    # Con la cantidad real (1), solo el fallido.
    assert pedir(["ENVIADO", "SIMULADO", "ENCOLA", "FALLA1"], 1).status_code == 200
    assert {n: _estado(n) for n in ("ENVIADO", "SIMULADO", "ENCOLA")} == {
        n: antes[n] for n in ("ENVIADO", "SIMULADO", "ENCOLA")}
    assert _estado("FALLA1")[0] == "pending"


def test_por_grupos_y_con_la_cantidad_exacta(escenario):
    c = escenario["c"]
    grupo = "schmitz/prod|CGI:SESSION_STORE_UNAVAILABLE (4023)"
    r = c.post("/api/reintentos/reintentar", auth=_auth(), json={"grupos": [grupo], "cantidad_confirmada": 3})
    assert r.status_code == 409 and "cantidad cambió" in r.json()["detail"]
    assert _estado("FALLA1")[0] == _estado("FALLA2")[0] == "failed"
    r = c.post("/api/reintentos/reintentar", auth=_auth(), json={"grupos": [grupo], "cantidad_confirmada": 2})
    assert r.status_code == 200 and r.json()["reintentados"] == 2
    assert (_estado("FALLA1")[0], _estado("FALLA2")[0], _estado("NEGOCIO")[0]) == ("pending", "pending", "failed")
    # El grupo ya no existe: se pide revisar.
    r = c.post("/api/reintentos/reintentar", auth=_auth(), json={"grupos": [grupo], "cantidad_confirmada": 2})
    assert r.status_code == 409


def test_parametros_validados(escenario):
    c, ids = escenario["c"], escenario["ids"]
    pedir = lambda cuerpo: c.post("/api/reintentos/reintentar", auth=_auth(), json=cuerpo)
    assert c.post("/api/reintentos/reintentar", json={"grupos": ["x"], "cantidad_confirmada": 1}).status_code == 401
    assert pedir({"provider": "../db", "env": "prod", "ids": [1], "cantidad_confirmada": 1}).status_code == 400
    assert pedir({"provider": "schmitz", "env": "prod; DROP", "ids": [1], "cantidad_confirmada": 1}).status_code == 400
    assert pedir({"provider": "noexiste", "env": "prod", "ids": [1], "cantidad_confirmada": 1}).status_code == 404
    assert pedir({"provider": "schmitz", "env": "prod", "ids": [ids["FALLA1"]], "grupos": ["x"],
                  "cantidad_confirmada": 1}).status_code == 400
    assert pedir({"provider": "schmitz", "env": "prod", "ids": ["uno"], "cantidad_confirmada": 1}).status_code == 422
    assert c.get("/api/reintentos/fallidos", params={"provider": "x/../y"}, auth=_auth()).status_code == 400
    assert _estado("FALLA1")[0] == "failed"


def test_respeta_el_modo_simulado(config_aislada, monkeypatch):
    """Integración en simulado: el reintento no llama a RC, como siempre."""
    from app.worker import processor
    _integracion(use_mock=True)
    monkeypatch.setattr(processor, "trigger_worker", lambda *a: None)
    ids = _eventos([("FALLA1", "failed", "rc_business_1", RESPUESTA_NEGOCIO)])
    r = _cliente().post("/api/reintentos/reintentar", auth=_auth(),
                        json={"provider": "schmitz", "env": "prod", "ids": [ids["FALLA1"]], "cantidad_confirmada": 1})
    assert r.status_code == 200
    assert _ciclo_rc_ok(monkeypatch) == [], "en modo simulado llamó a RC"
    assert _estado("FALLA1")[0] == "simulado"


def test_una_base_de_estado_no_es_una_integracion_a_reintentar(escenario):
    """La base de estado de Tive (y cualquier base que no sea de cola) no se ofrece ni se toca."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="tive", env="prod", provider_type="push"))
    db.commit()
    db.close()
    os.makedirs(os.path.join("db", "tive"), exist_ok=True)
    import sqlite3
    con = sqlite3.connect(os.path.join("db", "tive", "prod.db"))
    con.execute("CREATE TABLE pares (imei TEXT)")
    con.commit()
    con.close()
    c = escenario["c"]
    assert {g["integracion"] for g in c.get("/api/reintentos/fallidos", auth=_auth()).json()["grupos"]} == {"schmitz/prod"}
    assert c.post("/api/reintentos/reintentar", auth=_auth(), json={
        "provider": "tive", "env": "prod", "ids": [1], "cantidad_confirmada": 1}).status_code == 404


# ─── JS real del panel ───────────────────────────────────────────────────────

def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


def test_cableado_el_js_real_arma_el_resumen_y_la_confirmacion(escenario, tmp_path):
    """
    Resumen REAL del servidor → abrirReintentoFallidos y confirmarReintento REALES:
    los grupos con su cantidad y el aviso de permanente; el primer clic muestra
    la cantidad exacta y no manda nada; el segundo manda grupos y cantidad, y
    ese pedido, mandado al servidor real, reintenta exactamente esos.
    """
    c = escenario["c"]
    resumen = c.get("/api/reintentos/fallidos", auth=_auth()).json()
    r = _node({
        "globales": {"currentProviderFilter": "all", "currentStatusFilter": "failed"},
        "cargar": ["_escapeHtml", "_escAttr", "_mostrarModalReintento", "_mensajeReintento",
                   "_renderGruposReintento", "_gruposElegidosReintento", "_cambioEnReintento"],
        "stubs": ["fetchFilteredEvents"],
        "respuestas": {"/api/reintentos/fallidos": {"status": 200, "cuerpo": resumen},
                       "/api/reintentos/reintentar": {"status": 200, "cuerpo": {"reintentados": 2}}},
        "llamadas": [
            {"fn": "abrirReintentoFallidos"},
            {"fn": "confirmarReintento"},                    # 1.er clic: revisar
            {"fn": "confirmarReintento"},                    # 2.º clic: ejecutar
        ],
    }, tmp_path)
    el = r["elementos"]
    contenido = el["reintento-contenido"]["innerHTML"]
    assert "CGI:SESSION_STORE_UNAVAILABLE (4023)" in contenido and "schmitz/prod" in contenido
    assert "Va a volver a fallar salvo que se corrija el dato." in contenido
    assert el["reintento-total"]["textContent"] == "Elegidos: 2 evento(s) en 1 grupo(s).", "el permanente no arranca marcado"
    assert "<strong>2</strong>" in el["reintento-resumen"]["innerHTML"]
    pedidos = [p for p in r["fetch"] if p["url"].startswith("/api/reintentos/reintentar")]
    assert len(pedidos) == 1, "el primer clic ya mandó el pedido"
    cuerpo = json.loads(pedidos[0]["opciones"]["body"])
    assert cuerpo == {"grupos": ["schmitz/prod|CGI:SESSION_STORE_UNAVAILABLE (4023)"], "cantidad_confirmada": 2}
    assert "Listo: 2 evento(s) volvieron a la cola." == el["reintento-mensaje"]["textContent"]
    eco = c.post("/api/reintentos/reintentar", json=cuerpo, auth=_auth())
    assert eco.status_code == 200
    assert (_estado("FALLA1")[0], _estado("FALLA2")[0], _estado("NEGOCIO")[0]) == ("pending", "pending", "failed")


def test_cableado_js_marcar_un_permanente_lo_advierte(escenario, tmp_path):
    resumen = escenario["c"].get("/api/reintentos/fallidos", auth=_auth()).json()
    i_negocio = str(next(i for i, g in enumerate(resumen["grupos"]) if g["permanente"]))
    r = _node({
        "globales": {"currentProviderFilter": "schmitz", "currentStatusFilter": "failed"},
        "cargar": ["_escapeHtml", "_escAttr", "_mostrarModalReintento", "_mensajeReintento",
                   "_renderGruposReintento", "_gruposElegidosReintento", "_cambioEnReintento"],
        "stubs": ["fetchFilteredEvents"],
        "respuestas": {"/api/reintentos/fallidos": {"status": 200, "cuerpo": resumen}},
        "llamadas": [
            {"fn": "abrirReintentoFallidos"},
            {"marcar": {"selector": ".reintento-grupo", "i": i_negocio, "checked": True}},
            {"fn": "_cambioEnReintento"},
            {"fn": "confirmarReintento"},
        ],
    }, tmp_path)
    el = r["elementos"]
    assert r["fetch"][0]["url"] == "/api/reintentos/fallidos?provider=schmitz"
    assert el["reintento-total"]["textContent"] == "Elegidos: 3 evento(s) en 2 grupo(s)."
    assert "<strong>3</strong>" in el["reintento-resumen"]["innerHTML"]
    assert "1 son rechazos permanentes" in el["reintento-resumen"]["innerHTML"]
    assert not [p for p in r["fetch"] if "reintentar" in p["url"]], "mandó sin la segunda confirmación"


def test_cableado_js_reintento_de_una_fila(escenario, tmp_path):
    """Fila REAL de /api/stats con el filtro Fallidos → abrirReintentoFila → dos clics → pedido de 1."""
    c, ids = escenario["c"], escenario["ids"]
    filas = c.get("/api/stats", params={"status": "failed"}, auth=_auth()).json()["recent"]
    fila = next(f for f in filas if f["id"] == ids["NEGOCIO"])
    r = _node({
        "globales": {"allRecentEvents": filas, "currentStatusFilter": "failed", "currentProviderFilter": "all"},
        "cargar": ["_escapeHtml", "_escAttr", "_mostrarModalReintento", "_mensajeReintento", "_cambioEnReintento"],
        "stubs": ["fetchFilteredEvents"],
        "respuestas": {"/api/reintentos/reintentar": {"status": 200, "cuerpo": {"reintentados": 1}}},
        "llamadas": [
            {"fn": "_botonReintentoFallidos"},
            {"fn": "abrirReintentoFila", "args": [fila["id"]]},
            {"fn": "confirmarReintento"},
            {"fn": "confirmarReintento"},
        ],
    }, tmp_path)
    el = r["elementos"]
    assert el["btn-reintentar-fallidos"]["style"]["display"] == ""
    assert "datos invalidos" in el["reintento-contenido"]["innerHTML"]
    assert "<strong>1</strong>" in el["reintento-resumen"]["innerHTML"] or el["reintento-mensaje"]["textContent"]
    (pedido,) = [p for p in r["fetch"] if "reintentar" in p["url"]]
    cuerpo = json.loads(pedido["opciones"]["body"])
    assert cuerpo == {"provider": "schmitz", "env": "prod", "ids": [ids["NEGOCIO"]], "cantidad_confirmada": 1}
    assert c.post("/api/reintentos/reintentar", json=cuerpo, auth=_auth()).status_code == 200
    assert _estado("NEGOCIO")[0] == "pending"


def test_cableado_js_la_tabla_real_ofrece_reintentar_solo_con_el_filtro_fallidos(escenario, tmp_path):
    """renderRecentTable REAL con las filas REALES de /api/stats: el botón por fila sale solo en 'Fallidos'."""
    c, ids = escenario["c"], escenario["ids"]
    filas = c.get("/api/stats", params={"status": "failed"}, auth=_auth()).json()["recent"]
    todas = c.get("/api/stats", auth=_auth()).json()["recent"]

    def tabla(filtro, eventos):
        r = _node({"globales": {"allRecentEvents": eventos, "currentStatusFilter": filtro,
                                "currentProviderFilter": "all", "currentLatencyFilter": "all",
                                "currentSoloEventos": False, "expandedRows": []},
                   "llamadas": [{"fn": "renderRecentTable"}]}, tmp_path)
        return " ".join(r["elementos"]["recent-table-body"]["hijos"])

    con_filtro = tabla("failed", filas)
    for nombre in ("FALLA1", "FALLA2", "NEGOCIO"):
        assert f"abrirReintentoFila({ids[nombre]})" in con_filtro
    assert "abrirReintentoFila" not in tabla("all", todas), "sin el filtro Fallidos no se ofrece"


def test_cableado_js_el_boton_solo_con_el_filtro_fallidos(tmp_path):
    r = _node({"globales": {"currentStatusFilter": "sent"}, "llamadas": [{"fn": "_botonReintentoFallidos"}]},
              tmp_path)
    assert r["elementos"]["btn-reintentar-fallidos"]["style"]["display"] == "none"
