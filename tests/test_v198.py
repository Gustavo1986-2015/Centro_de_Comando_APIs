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

RAIZ = Path(__file__).resolve().parent.parent


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _app(*routers):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


def _insertar(proveedor, env, filas):
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session(proveedor, env)
    for f in filas:
        db.add(NormalizedRCEvent(provider=proveedor, **{"raw_data": "{}", **f}))
    db.commit()
    db.close()


def _node(script, entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / script), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════════
# 7. La grilla conserva el filtro con la actualización en vivo
# ═══════════════════════════════════════════════════════════════════════════

def test_cableado_la_grilla_filtrada_sobrevive_al_sse(config_aislada, tmp_path):
    """
    Backend real: 3 eventos de Tive y, DESPUÉS, 250 de Schmitz. El SSE manda los
    últimos 200 de todo el hub (todos de Schmitz). Con el filtro "tive" activo,
    la grilla tiene que seguir mostrando los 3 de Tive.
    """
    from app.api.routers import dashboard
    from app.models.config_models import ProviderConfig
    from app.database import get_session
    db = get_session("system_config", "global")
    db.add_all([ProviderConfig(provider_name="tive", env="prod", provider_type="push", is_active=True),
                ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True)])
    db.commit()
    db.close()
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    _insertar("tive", "prod", [dict(status="sent", chassis_number=f"K39347{i}", code="1",
                                    created_at=ahora - timedelta(minutes=10), date=ahora) for i in range(3)])
    _insertar("schmitz", "prod", [dict(status="sent", chassis_number=f"R{i:04d}", code="Standard",
                                       created_at=ahora, date=ahora) for i in range(250)])
    c = _app(dashboard.router)
    sse = c.get("/api/stats", auth=_auth()).json()
    filtrada = c.get("/api/stats?provider=tive", auth=_auth()).json()
    assert {e["provider"].lower() for e in sse["recent"]} == {"schmitz"}, "el escenario necesita que Tive no entre en los 200"
    assert len(filtrada["recent"]) == 3
    r = _node("verificar_panel_v198.js", {"filtro": {"proveedor": "tive"}, "filtrada": filtrada, "sse": sse}, tmp_path)
    assert [p.lower() for p in r["proveedores_antes_del_sse"]] == ["tive"] * 3
    assert [p.lower() for p in r["proveedores_despues_del_sse"]] == ["tive"] * 3, "el SSE pisó la lista filtrada"
    assert r["filas_en_la_grilla"] == 3
    assert any("provider=tive" in p for p in r["pedidos"][1:]), "el SSE no volvió a pedir la lista filtrada"


def test_cableado_sin_filtro_la_grilla_sigue_al_sse(config_aislada, tmp_path):
    from app.api.routers import dashboard
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    _insertar("schmitz", "prod", [dict(status="sent", chassis_number=f"R{i:04d}", code="Standard",
                                       created_at=ahora, date=ahora) for i in range(5)])
    c = _app(dashboard.router)
    sse = c.get("/api/stats", auth=_auth()).json()
    r = _node("verificar_panel_v198.js", {"filtro": {"proveedor": "all"}, "filtrada": sse, "sse": sse}, tmp_path)
    assert [p.lower() for p in r["proveedores_despues_del_sse"]] == ["schmitz"] * 5
    assert r["pedidos"] == [], "sin filtro no tiene que pedir nada al servidor"


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
    from tests.test_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
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


# ═══════════════════════════════════════════════════════════════════════════
# 3. B-4 — El sondeo PULL no bloquea el bucle de eventos
# ═══════════════════════════════════════════════════════════════════════════

async def test_el_bucle_de_eventos_responde_durante_un_sondeo_con_la_base_bloqueada(config_aislada, monkeypatch):
    """
    Otra conexión tiene la base tomada 1,5 s mientras el sondeo inserta. Antes,
    el bucle quedaba sin responder todo ese tiempo (medido: 3.010 ms con 3 s
    de bloqueo) y el hub no atendía webhooks. Ahora la espera ocurre en un hilo.
    """
    import asyncio
    import threading
    import time
    from app.database import get_engine, get_session
    from app.models.db_models import NormalizedRCEvent
    from app.worker import processor, pull_engine
    from tests.test_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    get_engine("protrack", "prod")

    tomada = threading.Event()

    def candado():
        con = sqlite3.connect("db/protrack/prod.db")
        con.execute("BEGIN IMMEDIATE")
        tomada.set()
        time.sleep(1.5)
        con.rollback()
        con.close()

    peor, corriendo = 0.0, True

    async def latido():
        nonlocal peor
        while corriendo:
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            peor = max(peor, time.perf_counter() - t - 0.01)

    tarea = asyncio.create_task(latido())
    await asyncio.sleep(0.05)   # el latido ya corre antes del sondeo
    hilo = threading.Thread(target=candado)
    hilo.start()
    await asyncio.to_thread(tomada.wait, 5)   # esperar el candado sin bloquear el bucle
    await pull_engine.process_and_enqueue("protrack", "prod", {"code": 0, "record": [dict(PROTRACK_ITEM)]},
                                          PROTRACK_MAPEO, enable_state_dedup=False)
    corriendo = False
    await tarea
    hilo.join()
    db = get_session("protrack", "prod")
    assert db.query(NormalizedRCEvent).count() >= 1, "el sondeo tenía que esperar el candado e insertar"
    db.close()
    assert peor < 0.5, f"el bucle de eventos quedó {peor * 1000:.0f} ms sin responder"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Tive — ShipmentArriveDepart (estructura DERIVADA: faltan los crudos reales)
# ═══════════════════════════════════════════════════════════════════════════

def _llegada_salida(alert_on, reasons=None):
    """
    Alerta de sello derivada de un cierre real del fixture (HumidityMax,
    05/10): AlertType=ShipmentArriveDepart y Alert.Trigger.AlertOn. Los dos
    crudos reales del 08/10 (037ab87a… Departure, b3ad9d3f… Arrival) no están
    en esta máquina; cuando lleguen, reemplazan a esta derivación.
    """
    import copy
    from tests.test_v195_tive import CRUDOS, CIERRE_2284
    alerta = copy.deepcopy(CRUDOS[CIERRE_2284])
    for nodo in (alerta, alerta["Alert"]):
        nodo["DeviceId"] = nodo["DeviceName"] = nodo["EntityName"] = "A2A2A2017BF2"
        nodo["AlertType"] = "ShipmentArriveDepart"
    alerta["Alert"]["AlertId"] = f"derivada-{alert_on}-{reasons}"
    if alert_on is None:
        alerta["Alert"]["Trigger"].pop("AlertOn", None)
    else:
        alerta["Alert"]["Trigger"]["AlertOn"] = alert_on
    if reasons is not None:
        alerta["RecoveredAlertDate"] = "0001-01-01T00:00:00"
        for d in alerta["Alert"]["Details"]:
            d["Reasons"] = reasons
    return alerta


def _procesar_tive(tmp_path, monkeypatch, payload):
    from app.providers.tive import estado, modulo
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    try:
        return modulo.procesar(payload, "prod", {}, "llegada-salida")
    finally:
        estado.cerrar_todo()


@pytest.mark.parametrize("alert_on,codigo", [("Departure", "ShipmentArriveDepart-Departure"),
                                              ("Arrival", "ShipmentArriveDepart-Arrival"),
                                              ("arrival", "ShipmentArriveDepart-Arrival")])
@pytest.mark.parametrize("reasons", [None, ["Created", "Closed"], ["Closed"]])
def test_llegada_y_salida_son_puntuales_con_su_sufijo(tmp_path, monkeypatch, caplog, alert_on, codigo, reasons):
    """
    reasons=None: el cierre real (con fecha de recuperación) que antes salía
    con -FIN. ["Created","Closed"] sin recuperación: antes avisaba "Verificar
    el tipo". ["Closed"]: antes era un cierre.
    """
    import logging
    with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
        (ev,) = _procesar_tive(tmp_path, monkeypatch, _llegada_salida(alert_on, reasons))
    assert ev.code == codigo
    assert ev.chassis_number == "A2A2A2017BF2", "el sello sale con su número"
    assert "-FIN" not in ev.code and "Verificar el tipo" not in caplog.text


@pytest.mark.parametrize("alert_on", [None, "Loitering"])
def test_llegada_salida_sin_alert_on_reconocible_sale_literal_con_aviso(tmp_path, monkeypatch, caplog, alert_on):
    import logging
    with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
        (ev,) = _procesar_tive(tmp_path, monkeypatch, _llegada_salida(alert_on))
    assert ev.code == "ShipmentArriveDepart"
    assert "sin Alert.Trigger.AlertOn reconocible" in caplog.text and repr(alert_on) in caplog.text
    assert "Verificar el tipo" not in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# 5. Errores PULL: siempre el tipo, y el mensaje si lo hay
# ═══════════════════════════════════════════════════════════════════════════

def test_describir_error():
    import httpx
    from app.worker.pull_engine import describir_error
    assert str(httpx.ConnectTimeout("")) == "", "medido: el timeout real de httpx no tiene texto"
    assert describir_error(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert describir_error(ValueError("respuesta rara")) == "ValueError: respuesta rara"


async def test_cableado_un_timeout_del_sondeo_se_ve_en_consola_y_en_salud(monkeypatch, caplog):
    import asyncio
    import logging
    import httpx
    from app.core import provider_health
    from app.worker import pull_engine

    async def falla(_cfg):
        raise httpx.ConnectTimeout("")

    async def cortar(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(pull_engine, "_leer_config_sondeo_sync",
                        lambda p, e: (True, {"url": "http://proveedor.invalid/api"}, {}, 30, False, False))
    monkeypatch.setattr(pull_engine, "execute_fetch", falla)
    monkeypatch.setattr(pull_engine.asyncio, "sleep", cortar)
    provider_health.forget("sondeo", "prod")
    try:
        with caplog.at_level(logging.ERROR, logger=pull_engine.logger.name):
            with pytest.raises(asyncio.CancelledError):
                await pull_engine.telemetry_poll_loop("sondeo", "prod")
        assert provider_health._entry("sondeo", "prod")["last_fetch_error"] == "ConnectTimeout"
        assert "Error en Sondeo PULL: ConnectTimeout" in caplog.text
    finally:
        provider_health.forget("sondeo", "prod")


# ═══════════════════════════════════════════════════════════════════════════
# 6. Versión visible en el pie del panel y en la consola al arrancar
# ═══════════════════════════════════════════════════════════════════════════

def test_la_version_es_1_9_8_y_se_ve_en_el_pie_del_panel(config_aislada):
    from app.api.routers import dashboard
    from app.version import __version__
    assert __version__ == "1.9.8"
    r = _app(dashboard.router).get("/dashboard", auth=_auth())
    assert r.status_code == 200
    assert f'id="version-hub" title="Versión del hub en ejecución">v1.9.8</span>' in r.text


async def test_al_arrancar_la_consola_dice_la_version(monkeypatch, caplog):
    """
    Corre el arranque real (lifespan) y lo corta justo después de la línea de
    versión con un THREAD_POOL_SIZE inválido: no llega a lanzar ninguna tarea.
    """
    import logging
    import main
    monkeypatch.setenv("THREAD_POOL_SIZE", "no-es-un-numero")
    # import main reconfigura el logging (dictConfig) y saca el handler de
    # caplog de la raíz: se engancha directo al logger "main".
    registro = logging.getLogger("main")
    registro.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="main"):
            with pytest.raises(ValueError):
                async with main.lifespan(main.app):
                    pass
    finally:
        registro.removeHandler(caplog.handler)
    assert "Hub Telemático Assistcargo v1.9.8: arrancando." in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# 9. Almacenamiento: solo bases de cola, ni listadas ni purgadas las de estado
# ═══════════════════════════════════════════════════════════════════════════

def _tablas(ruta):
    con = sqlite3.connect(ruta)
    try:
        return sorted(t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        con.close()


@pytest.fixture
def base_de_estado_tive(config_aislada, monkeypatch):
    from app.providers.tive import estado
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", os.path.join(".", "db", "tive"))
    estado.aprender_par("prod", "865648069052454", "K393478", "webhook")
    estado.cerrar_todo()
    yield os.path.join("db", "tive", "prod_estado.db")
    estado.cerrar_todo()


def test_almacenamiento_lista_solo_colas_y_no_toca_la_base_de_estado(base_de_estado_tive):
    from app.api.routers import dashboard
    _insertar("tive", "prod", [dict(status="sent", chassis_number="K393478", code="1")])
    antes = _tablas(base_de_estado_tive)
    r = _app(dashboard.router).get("/api/maintenance/db-stats", auth=_auth())
    assert r.status_code == 200
    assert [(d["provider"], d["env"]) for d in r.json()["databases"]] == [("tive", "prod")]
    assert _tablas(base_de_estado_tive) == antes, "listar le creó tablas a la base de estado"
    assert "normalized_rc_events" not in antes


def test_purgar_una_base_de_estado_o_inexistente_se_rechaza(base_de_estado_tive):
    from app.api.routers import dashboard
    from app.database import es_base_de_cola
    _insertar("tive", "prod", [dict(status="sent", chassis_number="K393478", code="1")])
    c = _app(dashboard.router)
    antes = _tablas(base_de_estado_tive)
    r = c.post("/api/maintenance/purge/tive/prod_estado", auth=_auth())
    assert r.status_code == 400 and "no es una base de cola" in r.json()["detail"]
    assert _tablas(base_de_estado_tive) == antes
    r = c.post("/api/maintenance/purge/tive/inventada", auth=_auth())
    assert r.status_code == 400
    assert not os.path.exists(os.path.join("db", "tive", "inventada.db")), "la purga creó una base vacía"
    assert c.post("/api/maintenance/purge/tive/prod", auth=_auth()).status_code == 200
    assert es_base_de_cola(os.path.join("db", "tive", "prod.db"))
    # Una base de estado "contaminada" por v1.9.7 (con su tabla de cola vacía) sigue siendo de estado.
    con = sqlite3.connect(base_de_estado_tive)
    con.execute("CREATE TABLE normalized_rc_events (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    assert not es_base_de_cola(base_de_estado_tive)
    assert c.post("/api/maintenance/purge/tive/prod_estado", auth=_auth()).status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# 10. Descartes con latitud y longitud
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def descartes_propios(tmp_path, monkeypatch):
    from app.core import admision, descartes
    descartes.esperar_escritura(2.0)
    monkeypatch.setattr(descartes, "DIRECTORIO", str(tmp_path / "descartes"))
    descartes.reset()
    admision.reset()
    yield descartes
    descartes.esperar_escritura(2.0)
    admision.reset()


def _ultimos(descartes):
    descartes.esperar_escritura(3.0)
    return descartes.consultar(100)["ultimos"]


def test_descartes_migracion_idempotente_de_una_base_vieja(descartes_propios):
    d = descartes_propios
    os.makedirs(d.DIRECTORIO, exist_ok=True)
    ruta = os.path.join(d.DIRECTORIO, d.ARCHIVO)
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE descartes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, proveedor TEXT,"
                " env TEXT, origen TEXT, motivo TEXT, detalle TEXT, equipo TEXT, envio TEXT, alert_id TEXT)")
    con.execute("INSERT INTO descartes (ts, proveedor, env, origen, motivo, equipo) VALUES (1, 'tive', 'prod',"
                " 'tive', 'viejo', 'K1')")
    con.commit()
    con.close()
    for _ in range(2):
        d._conectar().close()
    con = sqlite3.connect(ruta)
    columnas = [c[1] for c in con.execute("PRAGMA table_info(descartes)")]
    filas = con.execute("SELECT motivo, equipo, latitud, longitud FROM descartes").fetchall()
    con.close()
    assert columnas[-2:] == ["latitud", "longitud"] and columnas.count("latitud") == 1
    assert filas == [("viejo", "K1", None, None)]


def test_descartes_por_contrato_guardan_y_muestran_coordenadas(descartes_propios, caplog):
    import logging
    from types import SimpleNamespace
    from app.core import contrato
    con = SimpleNamespace(chassis_number="R5868BDP", date=None, latitude=-34.6, longitude=-58.4,
                          serial_number=None, code="Standard", shipment=None)
    sin = SimpleNamespace(chassis_number="R1", date=None, latitude=None, longitude=None,
                          serial_number=None, code="Standard", shipment=None)
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        assert contrato.filtrar_validos([con, sin], "schmitz", "prod") == []
    lineas = [r.getMessage() for r in caplog.records]
    assert any("R5868BDP" in l and "lat=-34.6, lon=-58.4" in l for l in lineas)
    assert any("patente=R1 " in l and "lat=" not in l for l in lineas)
    por_equipo = {u["equipo"]: u for u in _ultimos(descartes_propios)}
    assert (por_equipo["R5868BDP"]["latitud"], por_equipo["R5868BDP"]["longitud"]) == (-34.6, -58.4)
    assert (por_equipo["R1"]["latitud"], por_equipo["R1"]["longitud"]) == (None, None)


def test_descartes_por_admision_y_por_tive_guardan_coordenadas(descartes_propios, tmp_path, monkeypatch, caplog):
    import copy
    import logging
    from app.core import admision
    from app.providers.tive import estado, modulo
    from tests.test_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
    from tests.test_v194_descartes import CRUDOS
    coords = admision.coordenadas(PROTRACK_ITEM, PROTRACK_MAPEO)
    assert coords == (9.913503, -84.679345)
    with caplog.at_level(logging.INFO, logger="app.core.admision"):
        admision.registrar_descarte("studio", "prod", "equipo excluido", "patente=864035052734572", coords)
    assert "lat=9.913503, lon=-84.679345" in caplog.text
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    try:
        assert modulo.procesar(copy.deepcopy(CRUDOS["2026-10-01:1"]), "prod", {}, "a") == []
    finally:
        estado.cerrar_todo()
    por_origen = {u["origen"]: u for u in _ultimos(descartes_propios)}
    assert (por_origen["admision"]["latitud"], por_origen["admision"]["longitud"]) == coords
    ubicacion = CRUDOS["2026-10-01:1"]["Location"]
    assert (por_origen["tive"]["latitud"], por_origen["tive"]["longitud"]) == (
        ubicacion["Latitude"], ubicacion["Longitude"])


# ═══════════════════════════════════════════════════════════════════════════
# 11-12. Visor de base de datos
# ═══════════════════════════════════════════════════════════════════════════

CIFRADO_DE_PRUEBA = "gAAAAABpruebaDeTextoCifradoQueNoTieneQueSalir=="


@pytest.fixture
def visor(config_aislada, monkeypatch):
    """
    Datos de las tres integraciones: colas de Schmitz y Protrack, la base de
    estado de Tive, la configuración con columnas cifradas y respaldos JSONL.
    Todo sintético: no hay datos reales de clientes.
    """
    from app.api.routers import db_viewer
    from app.database import get_session
    from app.models.config_models import ProviderConfig, ProviderDictionary
    from app.providers.tive import estado

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          rc_user="AC_avl_SchmitzCargoBull"))
    db.add(ProviderDictionary(provider_name="protrack", env="prod", dict_key="864035052734572", dict_value="P734572"))
    db.commit()
    db.close()
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    con.execute("UPDATE provider_config SET rc_password_enc = ?, webhook_auth_secret_enc = ?",
                (CIFRADO_DE_PRUEBA, CIFRADO_DE_PRUEBA))
    con.commit()
    con.close()

    base = datetime(2026, 10, 8, 10, 0, 0)
    estados = ["sent", "simulado", "failed", "pending"]
    _insertar("schmitz", "prod", [
        dict(status=estados[i % 4], chassis_number=f"R{i:04d}", code="Standard",
             shipment="ENVIO-A" if i % 3 == 0 else None, created_at=base + timedelta(minutes=i),
             date=base + timedelta(minutes=i),
             raw_data=json.dumps({"ChassisNumber": f"R{i:04d}", "nota": "<b>x</b>", "n": i}))
        for i in range(120)])
    _insertar("protrack", "prod", [
        dict(status="sent", chassis_number="P734572", code="1", created_at=base, date=base,
             raw_data=json.dumps({"imei": "864035052734572"}))])

    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", os.path.join(".", "db", "tive"))
    estado.aprender_par("prod", "865648069052454", "K393478", "webhook")
    estado.aprender_par("prod", "A2A2A2017BF2", "A2A2A2017BF2", "webhook")
    estado.cerrar_todo()

    def registro(i, dia, status, chassis, shipment=None):
        return {"id": i, "provider": "protrack", "env": "prod", "chassis": chassis, "status": status,
                "created_at": f"{dia}T{10 + i % 10:02d}:00:00", "response": None, "job_id": None,
                "code": "1", "date": f"{dia}T10:00:00", "latitude": 9.9, "longitude": -84.6,
                "shipment": shipment, "updated_at": f"{dia}T11:00:00"}

    carpeta = os.path.join("db", "backups_diarios", "protrack_prod", "2026-10")
    os.makedirs(carpeta)
    with open(os.path.join(carpeta, "procesados_2026-10-07.jsonl"), "w", encoding="utf-8") as f:
        for i in range(1, 4):
            f.write(json.dumps(registro(i, "2026-10-07", "sent", f"P00000{i}")) + "\n")
    with open(os.path.join(carpeta, "procesados_2026-10-08.jsonl"), "w", encoding="utf-8") as f:
        for i in range(4, 9):
            f.write(json.dumps(registro(i, "2026-10-08", "simulado" if i % 2 else "sent",
                                        f"P00000{i}", "ENVÍO-ñ" if i == 8 else None)) + "\n")
        f.write('{"id": 99, "cortada a mitad\n')
    os.makedirs(os.path.join("db", "backups_diarios", "schmitz_prod"))
    yield db_viewer
    estado.cerrar_todo()


def _visor():
    from app.api.routers import db_viewer
    return _app(db_viewer.router)


def test_visor_lo_mas_reciente_primero_y_paginado(visor):
    c = _visor()
    p1 = c.get("/api/db-viewer/query", params={"db_name": "schmitz/prod.db", "table": "normalized_rc_events",
                                               "limit": 50}, auth=_auth()).json()
    col = p1["columns"].index("chassis_number")
    assert p1["total"] == 120
    assert [f[col] for f in p1["rows"][:3]] == ["R0119", "R0118", "R0117"], "la primera página eran las más viejas"
    p3 = c.get("/api/db-viewer/query", params={"db_name": "schmitz/prod.db", "table": "normalized_rc_events",
                                               "limit": 50, "offset": 100}, auth=_auth()).json()
    assert [f[col] for f in p3["rows"]][-1] == "R0000" and len(p3["rows"]) == 20


def test_visor_filtros_de_la_base(visor):
    c = _visor()

    def consultar(**filtros):
        r = c.get("/api/db-viewer/query", params={"db_name": "schmitz/prod.db", "table": "normalized_rc_events",
                                                  "limit": 500, **filtros}, auth=_auth())
        assert r.status_code == 200, r.text
        d = r.json()
        return d, [dict(zip(d["columns"], f)) for f in d["rows"]]

    d, filas = consultar(estado="simulado")
    assert d["total"] == 30 and {f["status"] for f in filas} == {"simulado"}
    d, filas = consultar(patente="r011")
    assert {f["chassis_number"] for f in filas} == {f"R011{i}" for i in range(10)}
    d, filas = consultar(envio="envio-a", estado="sent")
    assert d["total"] == 10 and all(f["shipment"] == "ENVIO-A" and f["status"] == "sent" for f in filas)
    d, _ = consultar(desde="2026-10-08", hasta="2026-10-08")
    assert d["total"] == 120
    d, _ = consultar(desde="2026-10-09")
    assert d["total"] == 0
    assert c.get("/api/db-viewer/query", params={"db_name": "schmitz/prod.db", "table": "normalized_rc_events",
                                                 "estado": "inventado"}, auth=_auth()).status_code == 400
    # En la base de estado de Tive: patente aplica sobre device_name; estado no aplica y se avisa.
    r = c.get("/api/db-viewer/query", params={"db_name": "tive/prod_estado.db", "table": "pares",
                                              "patente": "K393", "estado": "sent"}, auth=_auth()).json()
    assert r["total"] == 1 and r["filtros_no_aplicados"] == ["estado"]


def test_visor_columnas_cifradas_nunca_salen(visor):
    c = _visor()
    r = c.get("/api/db-viewer/query", params={"db_name": "system_config_global.db", "table": "provider_config"},
              auth=_auth())
    assert CIFRADO_DE_PRUEBA not in r.text
    d = r.json()
    fila = dict(zip(d["columns"], d["rows"][0]))
    assert fila["rc_password_enc"] == "(cifrado)" and fila["webhook_auth_secret_enc"] == "(cifrado)"
    assert fila["fetch_config_enc"] is None, "una columna cifrada vacía sigue diciendo NULL"
    assert set(d["cifradas"]) >= {"rc_password_enc", "webhook_auth_secret_enc", "fetch_config_enc"}
    busqueda = c.get("/api/db-viewer/query", params={"db_name": "system_config_global.db",
                                                     "table": "provider_config", "search": "pruebaDeTexto"},
                     auth=_auth()).json()
    assert busqueda["total"] == 0, "la búsqueda permitía sondear el texto cifrado"
    csv_ = c.get("/api/db-viewer/descargar", params={"origen": "base", "db_name": "system_config_global.db",
                                                     "table": "provider_config"}, auth=_auth())
    assert csv_.status_code == 200 and CIFRADO_DE_PRUEBA not in csv_.text and "(cifrado)" in csv_.text


def test_visor_no_edita_columnas_cifradas_y_la_edicion_normal_sigue(visor):
    c = _visor()
    pw = os.environ["DASHBOARD_PASSWORD"]
    r = c.post("/api/db-viewer/update_cell", json={"db_name": "system_config_global.db", "table": "provider_config",
                                                   "rowid": 1, "column_name": "rc_password_enc",
                                                   "new_value": "(cifrado)", "password": pw}, auth=_auth())
    assert r.status_code == 403
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    assert con.execute("SELECT rc_password_enc FROM provider_config").fetchone()[0] == CIFRADO_DE_PRUEBA
    con.close()
    r = c.post("/api/db-viewer/update_cell", json={"db_name": "system_config_global.db",
                                                   "table": "provider_dictionary", "rowid": 1,
                                                   "column_name": "dict_value", "new_value": "P999",
                                                   "password": pw}, auth=_auth())
    assert r.status_code == 200, r.text


def test_visor_valida_contra_el_esquema_real(visor):
    c = _visor()
    q = lambda **p: c.get("/api/db-viewer/query", params=p, auth=_auth())
    assert q(db_name="schmitz/prod.db", table="no_existe").status_code == 400
    assert q(db_name="schmitz/prod.db", table="normalized_rc_events; DROP TABLE x").status_code == 400
    assert q(db_name="../fuera.db", table="x").status_code == 400
    assert q(db_name="schmitz/otra.db", table="x").status_code == 404
    assert not os.path.exists(os.path.join("db", "schmitz", "otra.db")), "mirar creó la base"
    assert q(db_name="schmitz/prod.db", table="normalized_rc_events", limit=5000).status_code == 400
    pw = os.environ["DASHBOARD_PASSWORD"]
    r = c.post("/api/db-viewer/update_cell", json={"db_name": "system_config_global.db",
                                                   "table": "provider_dictionary", "rowid": 1,
                                                   "column_name": "no_existe", "new_value": "x", "password": pw},
               auth=_auth())
    assert r.status_code == 400


def test_visor_base_de_estado_de_tive_en_solo_lectura(visor):
    c = _visor()
    ruta = os.path.join("db", "tive", "prod_estado.db")
    antes = (_tablas(ruta), os.path.getmtime(ruta))
    bases = {d["name"]: d for d in c.get("/api/db-viewer/databases", auth=_auth()).json()}
    assert bases["tive/prod_estado.db"]["estado"] is True and bases["tive/prod_estado.db"]["orphan"] is False
    tablas = {t["name"]: t for t in c.get("/api/db-viewer/tables", params={"db_name": "tive/prod_estado.db"},
                                          auth=_auth()).json()["tables"]}
    assert {"pares", "vistos", "tramos_pendientes", "consultas"} <= set(tablas)
    assert not any(t["orphan"] for t in tablas.values())
    d = c.get("/api/db-viewer/query", params={"db_name": "tive/prod_estado.db", "table": "pares"},
              auth=_auth()).json()
    assert d["editable"] is False and d["total"] == 2
    r = c.post("/api/db-viewer/update_cell", json={"db_name": "tive/prod_estado.db", "table": "pares", "rowid": 1,
                                                   "column_name": "device_name", "new_value": "X",
                                                   "password": os.environ["DASHBOARD_PASSWORD"]}, auth=_auth())
    assert r.status_code == 403
    assert (_tablas(ruta), os.path.getmtime(ruta)) == antes, "mirar la base de estado la modificó"


def test_lineas_al_reves_sin_cargar_el_archivo(tmp_path):
    from app.api.routers.db_viewer import _lineas_al_reves
    lineas = [json.dumps({"i": i, "texto": "ñandú — envío " * (i % 7)}, ensure_ascii=False) for i in range(500)]
    ruta = tmp_path / "x.jsonl"
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    for bloque in (7, 64, 4096):
        assert [l.decode("utf-8") for l in _lineas_al_reves(str(ruta), bloque)] == lineas[::-1]


def test_visor_respaldo_de_procesados(visor):
    c = _visor()
    lista = c.get("/api/db-viewer/respaldos", auth=_auth()).json()
    protrack = next(i for i in lista["integraciones"] if i["integracion"] == "protrack_prod")
    assert (protrack["primer_dia"], protrack["ultimo_dia"], protrack["archivos"]) == ("2026-10-07", "2026-10-08", 2)
    assert "simulado" in lista["estados"]

    def respaldo(**p):
        return c.get("/api/db-viewer/respaldo", params={"integracion": "protrack_prod", **p}, auth=_auth())

    d = respaldo(desde="2026-10-07", hasta="2026-10-08").json()
    col = d["columns"].index("id")
    assert d["total"] == 8, "la línea cortada no cuenta"
    assert [f[col] for f in d["rows"]] == [8, 7, 6, 5, 4, 3, 2, 1], "lo más reciente primero"
    d = respaldo(desde="2026-10-07", hasta="2026-10-08", limit=3, offset=3).json()
    assert [f[col] for f in d["rows"]] == [5, 4, 3]
    d = respaldo(desde="2026-10-08", hasta="2026-10-08").json()
    assert [f[col] for f in d["rows"]] == [8, 7, 6, 5, 4], "el rango filtra por created_at"
    d = respaldo(desde="2026-10-07", hasta="2026-10-08", estado="simulado").json()
    assert [f[col] for f in d["rows"]] == [7, 5]
    d = respaldo(desde="2026-10-07", hasta="2026-10-08", patente="p000002").json()
    assert [f[col] for f in d["rows"]] == [2]
    d = respaldo(desde="2026-10-07", hasta="2026-10-08", envio="envío-Ñ").json()
    assert [f[col] for f in d["rows"]] == [8]
    assert respaldo(desde="2026-09-01", hasta="2026-10-08").status_code == 400, "sin tope de días"
    assert respaldo().status_code == 400, "sin rango"
    for integracion, esperado in (("../db", 400), ("protrack_prod/../..", 400), ("tive_prod", 404)):
        r = c.get("/api/db-viewer/respaldo", params={"integracion": integracion, "desde": "2026-10-08",
                                                     "hasta": "2026-10-08"}, auth=_auth())
        assert r.status_code == esperado, integracion


def test_visor_descarga_csv_con_filtros_y_todas_las_paginas(visor):
    import csv as csvmod
    import io
    c = _visor()
    r = c.get("/api/db-viewer/descargar", params={"origen": "base", "db_name": "schmitz/prod.db",
                                                  "table": "normalized_rc_events", "estado": "simulado"},
              auth=_auth())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.startswith("﻿")
    filas = list(csvmod.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
    encabezado, datos = filas[0], filas[1:]
    assert "__rowid__" not in encabezado
    col = encabezado.index("chassis_number")
    assert len(datos) == 30 and {f[encabezado.index("status")] for f in datos} == {"simulado"}
    assert datos[0][col] == "R0117", "mismo orden que la vista"
    r = c.get("/api/db-viewer/descargar", params={"origen": "respaldo", "integracion": "protrack_prod",
                                                  "desde": "2026-10-07", "hasta": "2026-10-08",
                                                  "estado": "simulado"}, auth=_auth())
    filas = list(csvmod.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
    assert [f[filas[0].index("id")] for f in filas[1:]] == ["7", "5"]
    assert c.get("/api/db-viewer/descargar", params={"origen": "otro"}, auth=_auth()).status_code == 400


def test_cableado_visor_js_real(visor, tmp_path):
    """
    El JS real del visor con respuestas reales del servidor: lo que pide, lo
    que dibuja, la celda completa, lo que copia y lo que descarga. Las URLs que
    arma el JS se vuelven a pedir al servidor para comprobar que las entiende.
    """
    from app.core import descartes
    c = _visor()
    filtros = {"estado": "simulado", "patente": "R01"}
    respuesta = c.get("/api/db-viewer/query", params={"db_name": "schmitz/prod.db", "table": "normalized_rc_events",
                                                      "limit": 50, "offset": 0, **filtros}, auth=_auth()).json()
    respaldo = c.get("/api/db-viewer/respaldo", params={"integracion": "protrack_prod", "desde": "2026-10-07",
                                                        "hasta": "2026-10-08", "limit": 50, "offset": 0},
                     auth=_auth()).json()
    descartes.registrar("tive", "prod", "tive", "con coordenadas", equipo="K1", latitud=-34.809466,
                        longitud=-58.540894)
    descartes.registrar("tive", "prod", "tive", "sin coordenadas", equipo="K2")
    descartes.esperar_escritura(3.0)
    from app.api.routers import dashboard
    lista_descartes = _app(dashboard.router).get("/api/diagnostico/descartes?limite=10", auth=_auth()).json()

    r = _node("verificar_visor_v198.js", {
        "base": {"db": "schmitz/prod.db", "tabla": "normalized_rc_events", "filtros": filtros,
                 "respuesta": respuesta, "fila": 0, "columna": "raw_data"},
        "respaldo": {"integracion": "protrack_prod", "desde": "2026-10-07", "hasta": "2026-10-08",
                     "filtros": {}, "respuesta": respaldo},
        "descartes": lista_descartes,
        "usuario_rc": "AC_avl_SchmitzCargoBull",
    }, tmp_path)

    b = r["base"]
    assert b["pedido"].startswith("/api/db-viewer/query?")
    eco = c.get(b["pedido"], auth=_auth()).json()
    assert eco == respuesta, "el servidor no entendió los parámetros que arma el JS"
    assert "estado=simulado" in b["pedido"] and "patente=R01" in b["pedido"]
    assert len(b["filas"]) == respuesta["total"] == 5
    col = respuesta["columns"].index("raw_data") - 1
    primera = respuesta["rows"][0]
    celda = b["filas"][0][col]
    assert "&lt;b&gt;" in celda["contenido"] and "<b>" not in celda["contenido"], "el contenido no se escapó"
    assert "verCeldaBd(0, " in celda["atributos"]
    completo = json.dumps(json.loads(primera[col + 1]), indent=2, ensure_ascii=False)
    assert b["detalle_valor"] == completo and b["detalle_visible"] == "block"
    assert b["detalle_titulo"].startswith("raw_data — fila 1")
    (valor, fila) = b["copiado"]
    assert valor == {"via": "clipboard", "texto": completo}
    assert json.loads(fila["texto"]) == dict(zip(respuesta["columns"][1:], primera[1:]))
    assert "lo más reciente primero" in b["info"]
    assert b["descarga"].startswith("/api/db-viewer/descargar?")
    csv_ = c.get(b["descarga"], auth=_auth())
    assert csv_.status_code == 200 and len(csv_.text.strip().splitlines()) == 1 + 5

    rr = r["respaldo"]
    assert c.get(rr["pedido"], auth=_auth()).json() == respaldo
    assert len(rr["filas"]) == 8 and "respaldo" in rr["insignia"]
    assert c.get(rr["descarga"], auth=_auth()).status_code == 200

    filas_desc = r["descartes"]["filas_ultimos"]
    assert "Lat, Lon" in r["descartes"]["encabezado_ultimos"]
    assert filas_desc[0][-1] == "—"                           # el último registrado no trae coordenadas
    assert filas_desc[1][-1] == "-34.80947, -58.54089"
    assert r["ancho_usuario_rc"] >= len("AC_avl_SchmitzCargoBull") + 3


def test_cableado_visor_copiar_sin_contexto_seguro(visor, tmp_path):
    """Por http en la red local no hay navigator.clipboard: copia igual."""
    c = _visor()
    respuesta = c.get("/api/db-viewer/query", params={"db_name": "protrack/prod.db", "table": "normalized_rc_events",
                                                      "limit": 50, "offset": 0}, auth=_auth()).json()
    r = _node("verificar_visor_v198.js", {
        "base": {"db": "protrack/prod.db", "tabla": "normalized_rc_events", "filtros": {},
                 "respuesta": respuesta, "fila": 0, "columna": "chassis_number"},
        "contexto_seguro": False}, tmp_path)
    assert r["base"]["copiado"][0] == {"via": "execCommand", "texto": "P734572"}
