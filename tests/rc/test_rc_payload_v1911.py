"""
v1.9.11 · Bloque 3 — Ver fielmente lo enviado a RC.

rc_payload guarda EXACTAMENTE el diccionario que el cliente SOAP le entregó
a zeep, tomado en rc_soap al enviar (no reconstruido). En simulado, el que se
habría enviado. El panel lo muestra en "Enviado a RC", solo eso; el cálculo
de velocidad y la trazabilidad van en sus propios botones.

Se ejecuta: ingreso real de Schmitz, Protrack y Tive, worker real, cliente
SOAP del hub y zeep contra el WSDL real de RC (tests/rc/rc_falso.py); el
panel con su JS real.
"""
import collections
import copy
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.core import contrato
from tests.proveedores.protrack.test_protrack_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
from tests.proveedores.schmitz.test_schmitz_v192 import SCHMITZ_REAL
from tests.rc import rc_falso

RAIZ = Path(__file__).resolve().parents[2]
CAMPOS_DEL_CONTRATO = {"asset", "code", "customer", "date", "direction", "latitude", "longitude", "speed",
                       "altitude", "battery", "course", "humidity", "ignition", "odometer", "temperature",
                       "serialNumber", "shipment", "vehicleType", "vehicleBrand", "vehicleModel"}


@pytest.fixture(autouse=True)
def cache_limpia():
    contrato.invalidar_coordenadas_cero()
    yield
    contrato.invalidar_coordenadas_cero()


def _configurar(proveedor, env, use_mock):
    from app import database
    from app.models.config_models import ProviderConfig
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(provider_name=proveedor, env=env, is_active=True, use_mock=use_mock,
                          enable_state_dedup=False, rc_user="u", rc_password="p"))
    db.commit()
    db.close()


def _filas(proveedor, env):
    from app import database
    from app.models.db_models import NormalizedRCEvent
    database.check_and_migrate_provider_db(proveedor, env)
    db = database.get_session(proveedor, env)
    try:
        return list(db.query(NormalizedRCEvent).order_by(NormalizedRCEvent.id))
    finally:
        db.close()


def _identico_a_zeep(filas, falso):
    """Cada rc_payload es, byte a byte, el JSON de un diccionario que recibió zeep."""
    recibidos = [json.dumps(d, ensure_ascii=False) for d in falso.eventos()]
    guardados = [f.rc_payload for f in filas]
    assert recibidos and None not in guardados
    assert collections.Counter(guardados) == collections.Counter(recibidos)
    for texto in guardados:
        assert set(json.loads(texto)) <= CAMPOS_DEL_CONTRATO, "rc_payload tiene campos internos"


def _schmitz_por_su_endpoint(env, iid="p1"):
    from app.api.routers import schmitz
    schmitz._persist_batch([(copy.deepcopy(SCHMITZ_REAL), env, iid)])


# ─── Byte a byte con lo que recibe zeep ──────────────────────────────────────

def test_schmitz_real_rc_payload_identico_a_zeep(config_aislada, monkeypatch):
    _configurar("schmitz", "prod", use_mock=False)
    _schmitz_por_su_endpoint("prod")
    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "schmitz", "prod")
    filas = _filas("schmitz", "prod")
    assert {f.status for f in filas} == {"sent"}
    _identico_a_zeep(filas, falso)
    # El formato con que salió: textos, como los serializa el sobre SOAP.
    nodos = falso.nodos()
    assert sorted(json.loads(f.rc_payload)["speed"] for f in filas) == sorted(n["speed"] for n in nodos)


def test_protrack_real_rc_payload_identico_a_zeep(config_aislada, monkeypatch):
    from app.worker import pull_engine
    import asyncio
    _configurar("protrack", "test", use_mock=False)
    asyncio.run(pull_engine.process_and_enqueue("protrack", "test", [copy.deepcopy(PROTRACK_ITEM)],
                                                PROTRACK_MAPEO, enable_state_dedup=False))
    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "protrack", "test")
    _identico_a_zeep(_filas("protrack", "test"), falso)


def test_tive_real_rc_payload_identico_a_zeep_con_velocidad(app_tive, tmp_path, monkeypatch):
    from app.providers.tive import estado
    from tests.proveedores.tive.test_velocidad_v1910 import ENCENDIDO, _opciones_tive, _posicion
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado_tive"))
    _opciones_tive(ENCENDIDO, use_mock=False)
    for p in (_posicion(), _posicion(segundos=60, norte_m=1000)):
        assert _post(app_tive, p).status_code == 200
    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "tive", "prod")
    filas = _filas("tive", "prod")
    _identico_a_zeep(filas, falso)
    assert [json.loads(f.rc_payload)["speed"] for f in filas] == ["1.0", "60.0"], "formato con que salió"
    assert all("_velocidad" not in f.rc_payload for f in filas)
    estado.cerrar_todo()


def test_simulado_guarda_lo_que_se_habria_enviado(config_aislada, monkeypatch):
    """El mismo crudo, una integración real y otra simulada: el mismo rc_payload."""
    _configurar("schmitz", "prod", use_mock=False)
    _configurar("schmitz", "test", use_mock=True)
    _schmitz_por_su_endpoint("prod", "a")
    _schmitz_por_su_endpoint("test", "b")
    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "schmitz", "test")
    assert falso.eventos() == [], "en simulado no se llama a zeep"
    simulados = _filas("schmitz", "test")
    assert {f.status for f in simulados} == {"simulado"}
    rc_falso.procesar_cola(monkeypatch, "schmitz", "prod")
    reales = _filas("schmitz", "prod")
    _identico_a_zeep(reales, falso)
    assert collections.Counter(f.rc_payload for f in simulados) == collections.Counter(f.rc_payload for f in reales)


def test_tive_simulado_rc_payload(app_tive, tmp_path, monkeypatch):
    from app.providers.tive import estado
    from tests.proveedores.tive.test_v192_tive import CONNECTIVITY
    from tests.proveedores.tive.test_velocidad_v1910 import _opciones_tive
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado_tive"))
    _opciones_tive({"alertas_trackers": True}, use_mock=True)
    _post(app_tive, _por_origen(CONNECTIVITY))
    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "tive", "prod")
    (fila,) = _filas("tive", "prod")
    assert fila.status == "simulado" and falso.eventos() == []
    enviado = json.loads(fila.rc_payload)
    assert (enviado["latitude"], enviado["longitude"], enviado["asset"]) == ("0", "0", "K1153407")
    estado.cerrar_todo()


def test_un_error_de_transporte_guarda_el_intento(config_aislada, monkeypatch):
    """RC no respondió: el evento queda para reintento con lo que se le entregó a zeep."""
    _configurar("schmitz", "prod", use_mock=False)
    _schmitz_por_su_endpoint("prod")
    falso = rc_falso.instalar(monkeypatch)

    def caido(token, argumento):
        falso.lotes.append(copy.deepcopy(argumento))
        raise ConnectionError("RC no responde")

    monkeypatch.setattr(falso, "GPSAssetTracking", caido)
    rc_falso.procesar_cola(monkeypatch, "schmitz", "prod")
    filas = _filas("schmitz", "prod")
    assert {(f.status, f.retry_count) for f in filas} == {("pending", 1)}
    _identico_a_zeep(filas, falso)


def test_sin_intento_no_pisa_lo_guardado(config_aislada):
    """La cola no escribe rc_payload cuando no vino (p. ej. el respaldo falso del interruptor)."""
    import asyncio
    from app.core.sqlite_queue import SQLiteQueue
    _configurar("schmitz", "prod", use_mock=False)
    _schmitz_por_su_endpoint("prod")
    fila = _filas("schmitz", "prod")[0]
    cola = SQLiteQueue()
    asyncio.run(cola.mark_batch_as_sent("schmitz", "prod", [
        {"event_id": fila.id, "elapsed_sec": 0.1, "rc_response": "OK", "job_id": "1", "rc_payload": '{"a": 1}'}]))
    asyncio.run(cola.mark_batch_as_sent("schmitz", "prod", [
        {"event_id": fila.id, "elapsed_sec": 0.1, "rc_response": "OK", "job_id": "2"}]))
    asyncio.run(cola.mark_batch_as_failed("schmitz", "prod", [
        {"event_id": fila.id, "elapsed_sec": 0.1, "rc_response": "x", "job_id": "3", "rc_payload": None}]))
    assert next(f for f in _filas("schmitz", "prod") if f.id == fila.id).rc_payload == '{"a": 1}'


# ─── Migración y respaldo ────────────────────────────────────────────────────

def test_la_migracion_agrega_la_columna_y_las_filas_viejas_quedan_en_null(config_aislada):
    from app import database
    import os
    os.makedirs("db", exist_ok=True)
    ruta = database.get_db_url("schmitz", "prod").replace("sqlite:///", "")
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE normalized_rc_events (id INTEGER PRIMARY KEY, provider TEXT, status TEXT, "
                "chassis_number TEXT, latitude FLOAT, longitude FLOAT, code TEXT, date DATETIME)")
    con.execute("INSERT INTO normalized_rc_events (provider, status, chassis_number) VALUES ('schmitz','sent','V1')")
    con.commit()
    con.close()
    database.check_and_migrate_provider_db("schmitz", "prod")
    database.check_and_migrate_provider_db("schmitz", "prod")  # idempotente
    con = sqlite3.connect(ruta)
    columnas = [c[1] for c in con.execute("PRAGMA table_info(normalized_rc_events)")]
    assert columnas.count("rc_payload") == 1
    assert con.execute("SELECT rc_payload FROM normalized_rc_events").fetchall() == [(None,)]
    con.close()


def test_el_respaldo_de_procesados_lleva_el_rc_payload(config_aislada, monkeypatch):
    from app.worker.processor import evento_a_registro_respaldo
    _configurar("schmitz", "prod", use_mock=False)
    _schmitz_por_su_endpoint("prod")
    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "schmitz", "prod")
    fila = _filas("schmitz", "prod")[0]
    registro = evento_a_registro_respaldo(fila, "prod")
    assert registro["rc_payload"] == json.loads(fila.rc_payload)
    assert registro["rc_payload"] in falso.eventos()
    fila.rc_payload = None
    assert "rc_payload" not in evento_a_registro_respaldo(fila, "prod"), "evento anterior: igual que v1.9.10"


# ─── Panel: API y JS real ────────────────────────────────────────────────────

from tests.proveedores.tive.test_v192_tive import _por_origen, _post, app_tive  # noqa: E402,F401


def _stats():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard
    from app.core.auth import verify_dashboard_auth
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[verify_dashboard_auth] = lambda: None
    return TestClient(app).get("/api/stats").json()["recent"]


def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


# El arnés carga lo que falte y REPITE la llamada: renderRecentTable dibujaría
# las filas dos veces. Se cargan de antemano las funciones que usa.
PRECARGA = ["_contenidoEnviadoRC", "_escAttr", "_escapeHtml", "formatLatency", "_textoVelocidadEstimada",
            "_tituloVelocidadEstimada"]
GLOBALES = {"currentStatusFilter": "all", "currentProviderFilter": "all", "currentLatencyFilter": "all",
            "currentSoloEventos": False, "expandedRows": []}


def test_api_y_etiquetas_por_estado(config_aislada, monkeypatch):
    _configurar("schmitz", "test", use_mock=True)
    _configurar("schmitz", "prod", use_mock=False)
    _schmitz_por_su_endpoint("test", "s")
    _schmitz_por_su_endpoint("prod", "r")
    rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "schmitz", "test")
    filas = _stats()
    simulados = [f for f in filas if f["status"] == "simulado"]
    pendientes = [f for f in filas if f["status"] == "pending"]
    assert simulados and pendientes
    for f in simulados:
        assert f["time_sent"] == f["time_received_rc"] == "Simulado (no enviado)", "decía Fallido"
        assert set(f["rc_payload"]) <= CAMPOS_DEL_CONTRATO
    for f in pendientes:
        assert f["time_sent"] == f["time_received_rc"] == "Pendiente" and f["rc_payload"] is None
    rc_falso.procesar_cola(monkeypatch, "schmitz", "prod")
    enviados = [f for f in _stats() if f["status"] == "sent"]
    assert enviados and all(f["time_sent"].endswith("(UTC)") for f in enviados)


def test_panel_js_real_muestra_solo_lo_enviado(config_aislada, monkeypatch, tmp_path):
    _configurar("schmitz", "test", use_mock=True)
    _schmitz_por_su_endpoint("test")
    rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "schmitz", "test")
    filas = _stats()
    fila = filas[0]
    r = _node({"globales": {**GLOBALES, "allRecentEvents": filas}, "cargar": PRECARGA,
               "llamadas": [{"fn": "renderRecentTable"}, {"fn": "verEnviadoRC", "args": [fila["id"]]},
                            {"fn": "_contenidoEnviadoRC", "args": [fila]}]}, tmp_path)
    html = " ".join(r["elementos"]["recent-table-body"]["hijos"])
    # Simulado: el botón no dice "Enviado" (garantía de v1.9.7).
    assert html.count("🚀 Se habría enviado a RC") == len(filas) and "🚀 Enviado a RC" not in html
    assert 'title="Simulado: esto se habría enviado"' in html
    assert "Trazabilidad</button>" in html and "Cálculo de velocidad" not in html
    contenido = r["resultados"][2]
    assert contenido["titulo"] == "Simulado: esto se habría enviado"
    assert contenido["payload"] == fila["rc_payload"]
    modal = r["elementos"]["body"]["anidado"]
    assert "Simulado: esto se habría enviado" in modal
    for interno in ("_rc_assigned_job_id", "_velocidad_estimada", "job_id", "rc_response"):
        assert interno not in modal, f"{interno} mezclado con lo enviado"
    assert f'&quot;asset&quot;: &quot;{fila["rc_payload"]["asset"]}&quot;' in modal or \
        f'"asset": "{fila["rc_payload"]["asset"]}"' in modal


def test_panel_js_real_evento_anterior_y_trazabilidad(tmp_path):
    viejo = {"id": 7, "status": "sent", "rc_payload": None, "job_id": "123", "rc_response": "OK",
             "time_received": "t1", "time_sent": "t2", "time_received_rc": "t3", "latency_sec": 0.2,
             "rc_latency_sec": 0.5, "transmission_latency_sec": 1.0, "retry_count": 0}
    pendiente = dict(viejo, id=8, status="pending")
    r = _node({"globales": {"allRecentEvents": [viejo]},
               "llamadas": [{"fn": "_contenidoEnviadoRC", "args": [viejo]},
                            {"fn": "_contenidoEnviadoRC", "args": [pendiente]},
                            {"fn": "_contenidoTrazabilidad", "args": [viejo]},
                            {"fn": "verEnviadoRC", "args": [7]}]}, tmp_path)
    assert r["resultados"][0] == {"titulo": "No disponible (evento anterior a v1.9.11)", "payload": None, "nota": ""}
    assert r["resultados"][1]["titulo"] == "Todavía no se envió a RC"
    assert r["resultados"][2]["job_id"] == "123" and r["resultados"][2]["latencia_rc_seg"] == 0.5
    assert "No disponible (evento anterior a v1.9.11)" in r["elementos"]["body"]["anidado"]


def test_panel_js_real_escapa_el_contenido_del_modal(tmp_path):
    malicioso = {"id": 9, "status": "sent", "rc_payload": {"asset": "</textarea><img src=x onerror=alert(1)>"}}
    r = _node({"globales": {"allRecentEvents": [malicioso]},
               "llamadas": [{"fn": "verEnviadoRC", "args": [9]}]}, tmp_path)
    modal = r["elementos"]["body"]["anidado"]
    assert "<img" not in modal and "&lt;img" in modal
