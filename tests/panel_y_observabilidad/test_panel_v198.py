# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v198.py (reorganización por tema: panel_y_observabilidad).
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

RAIZ = Path(__file__).resolve().parents[2]


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
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / script), str(e), str(s)],
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
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    # v1.9.9 (B-21): el tablero ya no inventa una Schmitz que no está en la
    # base; la integración se crea, como en el test anterior.
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True))
    db.commit()
    db.close()
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    _insertar("schmitz", "prod", [dict(status="sent", chassis_number=f"R{i:04d}", code="Standard",
                                       created_at=ahora, date=ahora) for i in range(5)])
    c = _app(dashboard.router)
    sse = c.get("/api/stats", auth=_auth()).json()
    r = _node("verificar_panel_v198.js", {"filtro": {"proveedor": "all"}, "filtrada": sse, "sse": sse}, tmp_path)
    assert [p.lower() for p in r["proveedores_despues_del_sse"]] == ["schmitz"] * 5
    assert r["pedidos"] == [], "sin filtro no tiene que pedir nada al servidor"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Versión visible en el pie del panel y en la consola al arrancar
# ═══════════════════════════════════════════════════════════════════════════

def test_la_version_es_la_del_hub_y_se_ve_en_el_pie_del_panel(config_aislada):
    # v1.9.9: el nombre ya no lleva el número (era ..._es_1_9_8_...): cada
    # versión lo dejaba en rojo. El número exacto lo fija este test.
    from app.api.routers import dashboard
    from app.version import __version__
    assert __version__ == "1.9.11"
    r = _app(dashboard.router).get("/dashboard", auth=_auth())
    assert r.status_code == 200
    assert f'id="version-hub" title="Versión del hub en ejecución">v{__version__}</span>' in r.text


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
    from app.version import __version__
    assert f"Hub Telemático Assistcargo v{__version__}: arrancando." in caplog.text


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
    # v1.9.10 suma categoria e ingest_id después de latitud y longitud.
    assert columnas[-4:] == ["latitud", "longitud", "categoria", "ingest_id"] and columnas.count("latitud") == 1
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
    from tests.proveedores.protrack.test_protrack_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
    from tests.panel_y_observabilidad.test_v194_descartes import CRUDOS
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
