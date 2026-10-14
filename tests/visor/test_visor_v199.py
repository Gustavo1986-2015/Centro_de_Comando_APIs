# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque3.py (reorganización por tema: visor).
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


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _cliente(*routers):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


@pytest.fixture
def config_con_filas(config_aislada):
    from app.database import get_session
    from app.models.config_models import ProviderConfig, ProviderDictionary
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          use_mock=False, rc_user="AC_avl_SchmitzCargoBull", run_interval_sec=5))
    db.add(ProviderDictionary(provider_name="protrack", env="prod", dict_key="864035052734572", dict_value="PT734572"))
    db.commit()
    ids = {"config": db.query(ProviderConfig).first().id, "dicc": db.query(ProviderDictionary).first().id}
    db.close()
    return ids


def _editar(c, tabla, fila, columna, valor):
    return c.post("/api/db-viewer/update_cell", json={
        "db_name": "system_config_global.db", "table": tabla, "rowid": fila, "column_name": columna,
        "new_value": valor, "password": os.environ["DASHBOARD_PASSWORD"]}, auth=_auth())


def _valor(tabla, columna, fila):
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    try:
        return con.execute(f"SELECT {columna} FROM {tabla} WHERE rowid = ?", (fila,)).fetchone()[0]
    finally:
        con.close()


# ═══════════════════════════════════════════════════════════════════════════
# 3.1 Edición de celdas
# ═══════════════════════════════════════════════════════════════════════════

def test_31_el_valor_nuevo_se_escribe_de_verdad(config_con_filas, caplog):
    from app.api.routers import db_viewer
    c = _cliente(db_viewer.router)
    with caplog.at_level(logging.INFO, logger="app.core.auditor"):
        r = _editar(c, "provider_dictionary", config_con_filas["dicc"], "dict_value", "PT999999")
    assert r.status_code == 200, r.text
    assert (r.json()["antes"], r.json()["despues"]) == ("PT734572", "PT999999")
    assert _valor("provider_dictionary", "dict_value", config_con_filas["dicc"]) == "PT999999"
    # Registro de quién cambió qué.
    linea = next(rec.getMessage() for rec in caplog.records if "edicion_celda_visor" in rec.getMessage())
    usuario = os.environ["DASHBOARD_USER"]
    for dato in (f"user={usuario}", '"base": "system_config_global.db"', '"tabla": "provider_dictionary"',
                 f'"fila": {config_con_filas["dicc"]}', '"columna": "dict_value"', '"antes": "PT734572"',
                 '"despues": "PT999999"'):
        assert dato in linea, dato


@pytest.mark.parametrize("columna,valor,motivo", [
    ("run_interval_sec", "cinco", "entero"),
    ("is_active", "tal vez", "true/false"),
    ("mapping_schema", "{no es json", "mapping_schema"),
    ("id", "99", "clave primaria"),
])
def test_31_validacion_de_tipo_contra_el_esquema_real(config_con_filas, columna, valor, motivo):
    from app.api.routers import db_viewer
    r = _editar(_cliente(db_viewer.router), "provider_config", config_con_filas["config"], columna, valor)
    assert r.status_code == 400 and motivo in r.json()["detail"], r.text


def test_31_tipos_validos_se_guardan_convertidos(config_con_filas):
    from app.api.routers import db_viewer
    c = _cliente(db_viewer.router)
    fila = config_con_filas["config"]
    assert _editar(c, "provider_config", fila, "run_interval_sec", "12").status_code == 200
    assert _valor("provider_config", "run_interval_sec", fila) == 12
    assert _editar(c, "provider_config", fila, "mapping_schema", '{"base_mapping": {"chassis_number": "placa"}}').status_code == 200
    assert json.loads(_valor("provider_config", "mapping_schema", fila)) == {"base_mapping": {"chassis_number": "placa"}}


def test_31_las_columnas_cifradas_siguen_sin_editarse(config_con_filas):
    from app.api.routers import db_viewer
    r = _editar(_cliente(db_viewer.router), "provider_config", config_con_filas["config"], "rc_password_enc", "x")
    assert r.status_code == 403


def test_31_b13_use_mock_e_is_active_pasan_por_el_aviso(config_con_filas, caplog):
    from app.api.routers import db_viewer
    c = _cliente(db_viewer.router)
    fila = config_con_filas["config"]
    with caplog.at_level(logging.WARNING, logger="app.core.modo_simulado"):
        assert _editar(c, "provider_config", fila, "use_mock", "1").status_code == 200
        assert _editar(c, "provider_config", fila, "is_active", "false").status_code == 200
    usuario = os.environ["DASHBOARD_USER"]
    assert f"MODO SIMULADO ACTIVADO en schmitz/prod: REAL -> SIMULADO | usuario={usuario} | origen=visor de BD" \
        in caplog.text
    assert f"INTEGRACIÓN DESACTIVADA: schmitz/prod: activa -> inactiva | usuario={usuario} | origen=visor de BD" \
        in caplog.text
    assert _valor("provider_config", "use_mock", fila) == 1 and _valor("provider_config", "is_active", fila) == 0


def test_31_el_panel_de_configuracion_tambien_avisa_la_activacion(config_con_filas, caplog):
    from app.api.routers import admin_config
    fila = dict(id=config_con_filas["config"], is_active=False, rc_user="AC_avl_SchmitzCargoBull", use_mock=False,
                purge_interval_min=5, run_interval_sec=5, queue_backend="sqlite")
    with caplog.at_level(logging.WARNING, logger="app.core.modo_simulado"):
        assert _cliente(admin_config.router).post("/api/config", json=[fila], auth=_auth()).status_code == 200
    assert "INTEGRACIÓN DESACTIVADA: schmitz/prod" in caplog.text and "origen=panel" in caplog.text


def test_cableado_31_el_js_real_manda_el_valor_escrito_y_confirma_antes(config_con_filas, tmp_path):
    """
    Endpoint real de consulta → openEditModal REAL → el usuario escribe el
    valor → primer clic: muestra anterior y nuevo, sin mandar nada → segundo
    clic: manda el valor escrito. Esa petición se repite contra el servidor.
    """
    from app.api.routers import db_viewer
    c = _cliente(db_viewer.router)
    datos = c.get("/api/db-viewer/query", params={"db_name": "system_config_global.db",
                                                  "table": "provider_dictionary"}, auth=_auth()).json()
    col = datos["columns"].index("dict_value") - 1
    vista = {"columns": datos["columns"][1:], "rows": [f[1:] for f in datos["rows"]],
             "rowids": [f[0] for f in datos["rows"]], "editable": True, "cifradas": [], "tipos": datos["tipos"]}
    r = _node({
        "globales": {"_dbVista": vista, "_dbPage": {"size": 50, "offset": 0, "total": 1},
                     "_dbEditorState": {"db": "system_config_global.db", "table": "provider_dictionary",
                                        "editable": True, "pendingEdit": None}},
        "cargar": ["_escapeHtml", "_textoEditable", "_valorNuevoEnEdicion", "_cambioEnEdicion", "closeEditModal"],
        "stubs": ["loadQueryData"],
        "respuestas": {"/api/db-viewer/update_cell": {"status": 200, "cuerpo": {"status": "success"}}},
        "llamadas": [
            {"fn": "openEditModal", "args": [vista["rowids"][0], "dict_value", {"style": {}}, 0, col]},
            {"set": {"id": "db-edit-nuevo", "value": "PT111111"}},
            {"fn": "confirmCellEdit"},                       # 1.er clic: revisar
            {"set": {"id": "db-edit-password", "value": os.environ["DASHBOARD_PASSWORD"]}},
            {"fn": "confirmCellEdit"},                       # 2.º clic: guardar
        ],
    }, tmp_path)
    el = r["elementos"]
    assert el["db-edit-modal-anterior"]["textContent"] == "PT734572"
    assert "PT734572" in el["db-edit-resumen"]["innerHTML"] and "PT111111" in el["db-edit-resumen"]["innerHTML"]
    (pedido,) = r["fetch"]   # el primer clic no mandó nada
    cuerpo = json.loads(pedido["opciones"]["body"])
    assert cuerpo["new_value"] == "PT111111", "el JS volvió a mandar el valor original"
    eco = c.post("/api/db-viewer/update_cell", json=cuerpo, auth=_auth())
    assert eco.status_code == 200 and _valor("provider_dictionary", "dict_value", cuerpo["rowid"]) == "PT111111"


def test_37_descartes_db_no_es_residual(config_aislada):
    from app.api.routers import db_viewer
    from app.core import descartes
    os.makedirs("db", exist_ok=True)
    con = sqlite3.connect(os.path.join("db", "descartes.db"))
    con.execute("CREATE TABLE descartes (id INTEGER PRIMARY KEY, ts REAL)")
    con.commit()
    con.close()
    c = _cliente(db_viewer.router)
    bases = {d["name"]: d for d in c.get("/api/db-viewer/databases", auth=_auth()).json()}
    assert bases["descartes.db"]["orphan"] is False
    (tabla,) = c.get("/api/db-viewer/tables", params={"db_name": "descartes.db"}, auth=_auth()).json()["tables"]
    assert (tabla["name"], tabla["orphan"]) == ("descartes", False)
    assert descartes.ARCHIVO == db_viewer.ARCHIVO_DESCARTES
