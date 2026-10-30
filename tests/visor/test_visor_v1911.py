"""
v1.9.11 · Bloque 5 — Visor de base de datos.

Sin perder lo de v1.9.8/v1.9.9 (filtros, búsqueda, paginación, respaldos,
descarga, celda completa, edición con confirmación), el visor suma:
  1. celdas JSON con resumen en la grilla y el JSON formateado en un panel
     lateral con Copiar;
  2. orden en el servidor por cualquier columna del esquema real, y columnas
     visibles a elección, recordadas en el navegador;
  3. exportación CSV y Excel de lo filtrado y ordenado, con tope y aviso;
  4. total y conteo por estado encima de la grilla;
  5. la cola es de solo lectura; en el resto, el registro lleva usuario,
     base, tabla, columna, id, antes y después;
  6. "Sin posición" en las filas de cola sin latitud ni longitud.

Todos EJECUTAN el código: endpoints reales y el JS real del panel (arneses de
tools/panel). Datos sintéticos (fixture `visor` de test_visor_v198).
"""
import csv as csvmod
import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import zipfile
from datetime import date, datetime

import pytest

from tests.visor.test_visor_v198 import RAIZ, _auth, _insertar, _node as _node_v198, _visor, visor  # noqa: F401

COLA = {"db_name": "schmitz/prod.db", "table": "normalized_rc_events"}


def _q(c, ruta="/api/db-viewer/query", **p):
    r = c.get(ruta, params=p, auth=_auth())
    assert r.status_code == 200, r.text
    d = r.json()
    return d, [dict(zip(d["columns"], f)) for f in d["rows"]]


def _csv(r):
    return list(csvmod.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))


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
# 2. Orden en el servidor
# ═══════════════════════════════════════════════════════════════════════════

def test_orden_por_cualquier_columna_en_el_servidor(visor):
    c = _visor()
    d, filas = _q(c, **COLA, orden="chassis_number", sentido="asc", limit=5)
    assert [f["chassis_number"] for f in filas] == [f"R{i:04d}" for i in range(5)]
    assert d["orden"] == {"columna": "chassis_number", "sentido": "asc"}
    _, filas = _q(c, **COLA, orden="chassis_number", sentido="desc", limit=5)
    assert [f["chassis_number"] for f in filas] == [f"R{i:04d}" for i in range(119, 114, -1)]
    # Sobre todas las páginas, no solo la visible.
    _, pag2 = _q(c, **COLA, orden="chassis_number", sentido="asc", limit=5, offset=5)
    assert [f["chassis_number"] for f in pag2] == [f"R{i:04d}" for i in range(5, 10)]
    # A igualdad de valor, lo más reciente primero (como sin orden).
    _, filas = _q(c, **COLA, orden="status", sentido="asc", limit=3)
    assert [f["status"] for f in filas] == ["failed"] * 3
    assert [f["__rowid__"] for f in filas] == sorted((f["__rowid__"] for f in filas), reverse=True)
    # Con filtros.
    d, filas = _q(c, **COLA, estado="sent", orden="chassis_number", sentido="desc")
    assert d["total"] == 30 and filas[0]["chassis_number"] == "R0116"
    # Sin orden: como antes.
    d, filas = _q(c, **COLA, limit=1)
    assert d["orden"] is None and filas[0]["chassis_number"] == "R0119"


@pytest.mark.parametrize("orden,sentido,motivo", [
    ("no_existe", "asc", "no es una columna"),
    ("status; DROP TABLE normalized_rc_events", "asc", "no es una columna"),
    ("rowid", "asc", "no es una columna"),
    ("chassis_number", "arriba", "asc"),
])
def test_orden_validado_contra_el_esquema_real(visor, orden, sentido, motivo):
    c = _visor()
    r = c.get("/api/db-viewer/query", params={**COLA, "orden": orden, "sentido": sentido}, auth=_auth())
    assert r.status_code == 400 and motivo in r.json()["detail"]
    con = sqlite3.connect(os.path.join("db", "schmitz", "prod.db"))
    assert con.execute("SELECT COUNT(*) FROM normalized_rc_events").fetchone()[0] == 120
    con.close()


def test_no_se_ordena_por_una_columna_cifrada(visor):
    """El orden diría algo del contenido cifrado."""
    c = _visor()
    for ruta in ("/api/db-viewer/query", "/api/db-viewer/descargar"):
        r = c.get(ruta, params={"db_name": "system_config_global.db", "table": "provider_config",
                                "orden": "rc_password_enc"}, auth=_auth())
        assert r.status_code == 400 and "cifrada" in r.json()["detail"], ruta


def test_orden_del_respaldo(visor):
    c = _visor()
    p = {"integracion": "protrack_prod", "desde": "2026-10-07", "hasta": "2026-10-08"}
    d, filas = _q(c, "/api/db-viewer/respaldo", **p)
    assert d["orden"] is None and [f["id"] for f in filas] == [8, 7, 6, 5, 4, 3, 2, 1]
    _, filas = _q(c, "/api/db-viewer/respaldo", **p, orden="id", sentido="asc", limit=3, offset=3)
    assert [f["id"] for f in filas] == [4, 5, 6]
    d, filas = _q(c, "/api/db-viewer/respaldo", **p, orden="chassis", sentido="desc")
    assert [f["chassis"] for f in filas][:2] == ["P000008", "P000007"] and d["total"] == 8
    # NULL primero en ascendente, como SQLite; a igualdad, el orden del respaldo.
    _, filas = _q(c, "/api/db-viewer/respaldo", **p, orden="shipment", sentido="asc")
    assert [f["id"] for f in filas] == [7, 6, 5, 4, 3, 2, 1, 8]
    r = c.get("/api/db-viewer/respaldo", params={**p, "orden": "raw_data"}, auth=_auth())
    assert r.status_code == 400, "raw_data no es columna del respaldo"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Total y conteo por estado
# ═══════════════════════════════════════════════════════════════════════════

def test_total_y_conteo_por_estado_de_la_consulta(visor):
    c = _visor()
    d, _ = _q(c, **COLA)
    assert d["cola"] is True and d["total"] == 120
    assert d["conteo_por_estado"] == {"sent": 30, "simulado": 30, "failed": 30, "pending": 30}
    d, _ = _q(c, **COLA, envio="ENVIO-A")
    assert d["total"] == 40 and sum(d["conteo_por_estado"].values()) == 40, "el conteo es de lo filtrado"
    d, _ = _q(c, db_name="system_config_global.db", table="provider_dictionary")
    assert d["cola"] is False and d["conteo_por_estado"] is None
    d, _ = _q(c, "/api/db-viewer/respaldo", integracion="protrack_prod", desde="2026-10-07", hasta="2026-10-08")
    assert d["conteo_por_estado"] == {"sent": 6, "simulado": 2}


# ═══════════════════════════════════════════════════════════════════════════
# 1 y 6. Columnas JSON y "Sin posición"
# ═══════════════════════════════════════════════════════════════════════════

def test_columnas_json_declaradas_por_el_servidor(visor):
    c = _visor()
    d, _ = _q(c, **COLA)
    assert {"raw_data", "rc_response", "rc_payload", "velocidad_detalle"} <= set(d["columnas_json"])
    assert "chassis_number" not in d["columnas_json"]
    # El detalle de descartes (db/descartes.db) y toda columna declarada JSON.
    assert visor._columnas_json(["id", "motivo", "detalle"]) == ["detalle"]
    assert visor._columnas_json(["mapping_schema", "x_enc"], {"mapping_schema": "JSON", "x_enc": "JSON"}) == ["mapping_schema"]
    d, _ = _q(c, "/api/db-viewer/respaldo", integracion="protrack_prod", desde="2026-10-07", hasta="2026-10-08")
    assert d["columnas_json"] == ["response"]


def test_sin_posicion_en_las_filas_de_cola(visor):
    c = _visor()
    _insertar("tive", "prod", [
        dict(status="sent", chassis_number="K1", code="Connectivity", latitude=None, longitude=None),
        dict(status="sent", chassis_number="K2", code="Standard", latitude=0.0, longitude=0.0),
        dict(status="sent", chassis_number="K3", code="Standard", latitude=-34.6, longitude=-58.4),
    ])
    d, filas = _q(c, db_name="tive/prod.db", table="normalized_rc_events")
    assert d["sin_posicion"]["texto"] == "Sin posición (Tive no pudo ubicar)"
    assert [filas[i]["chassis_number"] for i in d["sin_posicion"]["filas"]] == ["K1"], "0,0 no es sin posición"
    d, _ = _q(c, db_name="system_config_global.db", table="provider_config")
    assert d["sin_posicion"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. Exportación CSV y Excel
# ═══════════════════════════════════════════════════════════════════════════

def _hoja(r) -> list[list[str]]:
    import re
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    filas = []
    for fila in re.findall(r"<row[^>]*>(.*?)</row>", xml):
        filas.append([m[0] or m[1] for m in re.findall(r"<v>(.*?)</v>|<t[^>]*>(.*?)</t>", fila)])
    return filas


def test_exporta_lo_filtrado_y_ordenado_con_las_columnas_elegidas(visor):
    c = _visor()
    p = {"origen": "base", **COLA, "estado": "sent", "orden": "chassis_number", "sentido": "asc",
         "columnas": "chassis_number,status,raw_data"}
    r = c.get("/api/db-viewer/descargar", params=p, auth=_auth())
    assert r.status_code == 200 and r.headers["x-visor-recortado"] == "false" and r.headers["x-visor-filas"] == "30"
    filas = _csv(r)
    assert filas[0] == ["chassis_number", "status", "raw_data"]
    _, vista = _q(c, **COLA, estado="sent", orden="chassis_number", sentido="asc", limit=1000)
    assert [f[0] for f in filas[1:]] == [v["chassis_number"] for v in vista], "el mismo orden que la grilla"
    assert json.loads(filas[1][2])["nota"] == "<b>x</b>"
    # Excel: lo mismo.
    x = c.get("/api/db-viewer/descargar", params={**p, "formato": "xlsx"}, auth=_auth())
    assert x.status_code == 200 and x.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert 'filename="visor_schmitz_prod.db_normalized_rc_events.xlsx"' in x.headers["content-disposition"]
    hoja = _hoja(x)
    assert hoja[0] == ["chassis_number", "status", "raw_data"] and [f[0] for f in hoja[1:]] == [f[0] for f in filas[1:]]
    # Respaldo ordenado, en Excel.
    x = c.get("/api/db-viewer/descargar", params={"origen": "respaldo", "integracion": "protrack_prod",
                                                  "desde": "2026-10-07", "hasta": "2026-10-08", "orden": "id",
                                                  "sentido": "asc", "columnas": "id,chassis", "formato": "xlsx"},
              auth=_auth())
    assert _hoja(x)[1:] == [[str(i), f"P00000{i}"] for i in range(1, 9)]


@pytest.mark.parametrize("extra,motivo", [
    ({"columnas": "chassis_number,no_existe"}, "no_existe"),
    ({"formato": "pdf"}, "csv o xlsx"),
    ({"orden": "inventada"}, "no es una columna"),
])
def test_exportacion_valida_sus_parametros(visor, extra, motivo):
    c = _visor()
    r = c.get("/api/db-viewer/descargar", params={"origen": "base", **COLA, **extra}, auth=_auth())
    assert r.status_code == 400 and motivo in r.json()["detail"]


def test_exportacion_con_tope_avisa_el_recorte(visor, monkeypatch, caplog):
    monkeypatch.setattr(visor, "TOPE_EXPORTACION", 7)
    c = _visor()
    d, _ = _q(c, **COLA)
    assert d["tope_exportacion"] == 7
    with caplog.at_level(logging.INFO, logger="app.core.auditor"):
        r = c.get("/api/db-viewer/descargar", params={"origen": "base", **COLA, "orden": "chassis_number"},
                  auth=_auth())
    assert r.headers["x-visor-recortado"] == "true" and r.headers["x-visor-filas"] == "7"
    filas = _csv(r)
    assert filas[1][0].startswith("AVISO: la vista tiene 120 filas y se exportan las primeras 7")
    col = filas[0].index("chassis_number")
    assert [f[col] for f in filas[2:]] == [f"R{i:04d}" for i in range(7)]
    linea = next(rec.getMessage() for rec in caplog.records if "descarga_visor_bd" in rec.getMessage())
    assert '"recortado": true' in linea and '"filas": 7' in linea
    # Respaldo, sin orden y con orden.
    p = {"origen": "respaldo", "integracion": "protrack_prod", "desde": "2026-10-07", "hasta": "2026-10-08"}
    for extra, ids in (({}, ["8", "7", "6", "5", "4", "3", "2"]), ({"orden": "id"}, [str(i) for i in range(1, 8)])):
        filas = _csv(c.get("/api/db-viewer/descargar", params={**p, **extra}, auth=_auth()))
        assert filas[1][0].startswith("AVISO: la vista tiene 8 filas")
        assert [f[filas[0].index("id")] for f in filas[2:]] == ids, extra
    # Sin recorte, sin aviso (el CSV de siempre).
    monkeypatch.setattr(visor, "TOPE_EXPORTACION", 1000)
    filas = _csv(c.get("/api/db-viewer/descargar", params=p, auth=_auth()))
    assert len(filas) == 9 and not filas[1][0].startswith("AVISO")


# ═══════════════════════════════════════════════════════════════════════════
# 5. Edición: la cola es de solo lectura; el resto, con registro completo
# ═══════════════════════════════════════════════════════════════════════════

def _editar(c, db, tabla, fila, columna, valor):
    return c.post("/api/db-viewer/update_cell", json={
        "db_name": db, "table": tabla, "rowid": fila, "column_name": columna, "new_value": valor,
        "password": os.environ["DASHBOARD_PASSWORD"]}, auth=_auth())


def test_la_cola_es_de_solo_lectura(visor):
    c = _visor()
    ruta = os.path.join("db", "schmitz", "prod.db")
    con = sqlite3.connect(ruta)
    antes = con.execute("SELECT status, chassis_number FROM normalized_rc_events ORDER BY id").fetchall()
    con.close()
    for columna, valor in (("status", "pending"), ("chassis_number", "OTRA"), ("raw_data", "{}")):
        r = _editar(c, "schmitz/prod.db", "normalized_rc_events", 1, columna, valor)
        assert r.status_code == 403 and "solo lectura" in r.json()["detail"] and "RC" in r.json()["detail"]
    con = sqlite3.connect(ruta)
    assert con.execute("SELECT status, chassis_number FROM normalized_rc_events ORDER BY id").fetchall() == antes
    con.close()
    d, _ = _q(c, **COLA)
    assert d["editable"] is False


def test_tabla_de_configuracion_fuera_de_su_base_no_se_edita(visor):
    """Una tabla residual con nombre de configuración en una base de cola."""
    ruta = os.path.join("db", "schmitz", "prod.db")
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE provider_dictionary (id INTEGER PRIMARY KEY, dict_value TEXT)")
    con.execute("INSERT INTO provider_dictionary (dict_value) VALUES ('X')")
    con.commit()
    con.close()
    c = _visor()
    d, _ = _q(c, db_name="schmitz/prod.db", table="provider_dictionary")
    assert d["editable"] is False
    r = _editar(c, "schmitz/prod.db", "provider_dictionary", 1, "dict_value", "Y")
    assert r.status_code == 403
    con = sqlite3.connect(ruta)
    assert con.execute("SELECT dict_value FROM provider_dictionary").fetchone()[0] == "X"
    con.close()


def test_registro_de_la_edicion_con_usuario_base_tabla_columna_id_antes_y_despues(visor, caplog):
    c = _visor()
    with caplog.at_level(logging.INFO, logger="app.core.auditor"):
        r = _editar(c, "system_config_global.db", "provider_dictionary", 1, "dict_value", "P111")
    assert r.status_code == 200, r.text
    linea = next(rec.getMessage() for rec in caplog.records if "edicion_celda_visor" in rec.getMessage())
    params = json.loads(linea.split("params=", 1)[1])
    assert params == {"usuario": os.environ["DASHBOARD_USER"], "base": "system_config_global.db",
                      "tabla": "provider_dictionary", "id": 1, "fila": 1, "columna": "dict_value",
                      "antes": "P734572", "despues": "P111"}


def test_choque_con_el_indice_unico_de_daily_stats_responde_409(visor):
    from app.database import get_session
    from app.models.config_models import DailyStat
    db = get_session("system_config", "global")
    db.add(DailyStat(date=date(2026, 10, 1), provider="schmitz", env="prod", sent_count=5))
    db.add(DailyStat(date=date(2026, 10, 2), provider="schmitz", env="prod", sent_count=7))
    db.commit()
    otro = db.query(DailyStat).filter(DailyStat.date == date(2026, 10, 2)).one().id
    db.close()
    r = _editar(_visor(), "system_config_global.db", "daily_stats", otro, "date", "2026-10-01")
    assert r.status_code == 409, r.text
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    assert con.execute("SELECT date FROM daily_stats ORDER BY id").fetchall() == [("2026-10-01",), ("2026-10-02",)]
    con.close()
    # Un día libre se guarda como lo escribe SQLAlchemy (AAAA-MM-DD), no con hora.
    r = _editar(_visor(), "system_config_global.db", "daily_stats", otro, "date", "2026-10-03")
    assert r.status_code == 200 and r.json()["despues"] == "2026-10-03"
    r = _editar(_visor(), "system_config_global.db", "daily_stats", otro, "date", "2026-10-03 10:00")
    assert r.status_code == 400, "una columna DATE no lleva hora"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Seguridad
# ═══════════════════════════════════════════════════════════════════════════

def test_todo_el_visor_pide_autenticacion(visor):
    c = _visor()
    for ruta, p in (("/api/db-viewer/query", COLA), ("/api/db-viewer/descargar", {"origen": "base", **COLA}),
                    ("/api/db-viewer/respaldo", {"integracion": "protrack_prod"})):
        assert c.get(ruta, params=p).status_code == 401, ruta
    r = c.post("/api/db-viewer/update_cell", json={"db_name": "system_config_global.db", "table": "provider_dictionary",
                                                   "rowid": 1, "column_name": "dict_value", "new_value": "Z",
                                                   "password": os.environ["DASHBOARD_PASSWORD"]})
    assert r.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# JS real
# ═══════════════════════════════════════════════════════════════════════════

def test_js_celda_json_resumen_en_la_grilla_y_json_formateado_al_lado(visor, tmp_path):
    """Arnés del visor v1.9.8: consulta → grilla → clic en raw_data → Copiar."""
    c = _visor()
    respuesta = c.get("/api/db-viewer/query", params={**COLA, "limit": 50, "offset": 0}, auth=_auth()).json()
    r = _node_v198("verificar_visor_v198.js", {"base": {"db": COLA["db_name"], "tabla": COLA["table"], "filtros": {},
                                                        "respuesta": respuesta, "fila": 0, "columna": "raw_data"}},
                   tmp_path)
    b = r["base"]
    col = respuesta["columns"].index("raw_data") - 1
    celda = b["filas"][0][col]
    assert 'class="db-celda db-celda-json"' in celda["atributos"]
    assert celda["contenido"].startswith('<span class="db-json-resumen">{3} {"ChassisNumber":"R0119","nota":"&lt;b&gt;x&lt;/b&gt;"')
    completo = json.dumps(json.loads(respuesta["rows"][0][col + 1]), indent=2, ensure_ascii=False)
    assert b["detalle_valor"] == completo and b["detalle_visible"] == "block"
    assert b["copiado"][0] == {"via": "clipboard", "texto": completo}
    # Una columna que no es JSON no se resume.
    celda_patente = b["filas"][0][respuesta["columns"].index("chassis_number") - 1]
    assert celda_patente["contenido"] == "R0119" and "db-celda-json" not in celda_patente["atributos"]


_CARGAR = ["_escapeHtml", "_escAttr", "_origenBd", "_parametrosVisorBd", "_claveVistaBd", "loadQueryData",
           "_asegurarBuscadorBd", "cerrarDetalleBd", "renderVisorBd", "_columnasOcultasBd", "_guardarOcultasBd",
           "_textoCeldaBd", "_resumenJsonBd", "_resumenConsultaBd", "_selectorColumnasBd", "_renderPagination",
           "ordenarBd", "alternarColumnaBd", "mostrarTodasColumnasBd", "descargarVistaBd"]


def _globales():
    return {"_dbPage": {"size": 50, "offset": 0, "total": 0},
            "_dbEditorState": {"db": None, "table": None, "editable": False, "pendingEdit": None},
            "_dbVista": None, "_dbSeleccion": None, "_dbUltimaConsulta": None, "_dbOrden": None,
            "_dbUltimaRespuesta": None, "_dbOcultas": {}}


def _seleccion(db=COLA["db_name"], tabla=COLA["table"]):
    return [{"set": {"id": "db-origen", "value": "base"}}, {"set": {"id": "db-select", "value": db}},
            {"set": {"id": "table-select", "value": tabla}}]


def test_js_ordenar_con_clic_en_el_encabezado(visor, tmp_path):
    c = _visor()
    base = c.get("/api/db-viewer/query", params={**COLA, "limit": 50, "offset": 0}, auth=_auth()).json()
    asc = c.get("/api/db-viewer/query", params={**COLA, "orden": "chassis_number", "sentido": "asc",
                                                "limit": 50, "offset": 0}, auth=_auth()).json()
    i = base["columns"].index("chassis_number") - 1
    r = _node({"globales": _globales(), "cargar": _CARGAR,
               "respuestas": {"/api/db-viewer/query": {"status": 200, "cuerpo": asc}},
               "llamadas": [*_seleccion(), {"fn": "loadQueryData"},
                            {"fn": "ordenarBd", "args": [i]}, {"fn": "ordenarBd", "args": [i]},
                            {"fn": "ordenarBd", "args": [i]}]}, tmp_path)
    urls = [p["url"] for p in r["fetch"]]
    assert "orden=" not in urls[0]
    assert "orden=chassis_number&sentido=asc" in urls[1]
    assert "orden=chassis_number&sentido=desc" in urls[2]
    assert "orden=" not in urls[3], "tercer clic: vuelve a lo más reciente primero"
    for url in urls:
        assert c.get(url, auth=_auth()).status_code == 200, url
    assert c.get(urls[1], auth=_auth()).json() == asc, "el servidor entiende lo que arma el JS"
    el = r["elementos"]
    assert f'onclick="ordenarBd({i})"' in el["db-viewer-thead"]["innerHTML"]
    assert "chassis_number ▲" in el["db-viewer-thead"]["innerHTML"]
    assert "ordenado por chassis_number (ascendente)" in el["db-viewer-info"]["textContent"]


def test_js_resumen_conteo_insignia_y_sin_posicion(visor, tmp_path):
    c = _visor()
    _insertar("tive", "prod", [dict(status="sent", chassis_number="K1", code="Connectivity",
                                    latitude=None, longitude=None)])
    cola = c.get("/api/db-viewer/query", params={**COLA, "limit": 50, "offset": 0}, auth=_auth()).json()
    tive = c.get("/api/db-viewer/query", params={"db_name": "tive/prod.db", "table": "normalized_rc_events",
                                                 "limit": 50, "offset": 0}, auth=_auth()).json()
    r = _node({"globales": _globales(), "cargar": _CARGAR,
               "llamadas": [*_seleccion(), {"fn": "renderVisorBd", "args": [cola, "cola"]}]}, tmp_path)
    el = r["elementos"]
    assert el["db-resumen"]["textContent"] == (
        "Total de la consulta: 120 · pending: 30 · sent: 30 · failed: 30 · simulado: 30")
    assert "editarla a mano puede mandar a RC" in el["db-edit-badge"]["textContent"]
    assert "ondblclick" not in el["db-viewer-tbody"]["innerHTML"], "la cola no se edita desde la grilla"
    r = _node({"globales": _globales(), "cargar": _CARGAR,
               "llamadas": [*_seleccion("tive/prod.db"), {"fn": "renderVisorBd", "args": [tive, "tive"]}]}, tmp_path)
    cuerpo = r["elementos"]["db-viewer-tbody"]["innerHTML"]
    assert cuerpo.count('<em class="db-sin-posicion">Sin posición (Tive no pudo ubicar)</em>') == 2, "latitud y longitud"
    assert "<em>NULL</em>" in cuerpo, "las demás columnas vacías siguen diciendo NULL"


def test_js_columnas_elegidas_se_recuerdan_y_se_exportan(visor, tmp_path):
    c = _visor()
    cola = c.get("/api/db-viewer/query", params={**COLA, "limit": 50, "offset": 0}, auth=_auth()).json()
    columnas = cola["columns"][1:]
    i_raw = columnas.index("raw_data")
    clave = "visorBd.ocultas.base|schmitz/prod.db|normalized_rc_events"
    r = _node({"globales": _globales(), "cargar": _CARGAR,
               "respuestas": {"/api/db-viewer/query": {"status": 200, "cuerpo": cola}},
               "llamadas": [*_seleccion(), {"fn": "loadQueryData"},
                            {"fn": "alternarColumnaBd", "args": [i_raw]},
                            {"fn": "descargarVistaBd", "args": ["xlsx"]}]}, tmp_path)
    assert json.loads(r["almacen"][clave]) == ["raw_data"]
    el = r["elementos"]
    assert ">raw_data<" not in el["db-viewer-thead"]["innerHTML"] and ">chassis_number<" in el["db-viewer-thead"]["innerHTML"]
    assert "1 columna oculta." in el["db-viewer-info"]["textContent"]
    assert f'onchange="alternarColumnaBd({i_raw})"' in el["db-columnas-panel"]["innerHTML"]
    url = r["ubicacion"]
    assert "formato=xlsx" in url and "columnas=" in url
    x = c.get(url, auth=_auth())
    assert x.status_code == 200 and _hoja(x)[0] == [col for col in columnas if col != "raw_data"]

    # Otra visita: el navegador lo recuerda.
    r = _node({"globales": _globales(), "cargar": _CARGAR, "almacen": {clave: '["raw_data"]'},
               "llamadas": [*_seleccion(), {"fn": "renderVisorBd", "args": [cola, "cola"]}]}, tmp_path)
    assert ">raw_data<" not in r["elementos"]["db-viewer-thead"]["innerHTML"]
    # Ventana privada (sin almacenamiento): funciona igual, en memoria.
    r = _node({"globales": _globales(), "cargar": _CARGAR, "sin_almacen": True,
               "respuestas": {"/api/db-viewer/query": {"status": 200, "cuerpo": cola}},
               "llamadas": [*_seleccion(), {"fn": "loadQueryData"}, {"fn": "alternarColumnaBd", "args": [i_raw]}]},
              tmp_path)
    assert ">raw_data<" not in r["elementos"]["db-viewer-thead"]["innerHTML"]
    assert not [x for x in r["consola"] if x[0] == "error"]


def test_js_aviso_de_recorte_antes_de_exportar(visor, monkeypatch, tmp_path):
    monkeypatch.setattr(visor, "TOPE_EXPORTACION", 5)
    c = _visor()
    cola = c.get("/api/db-viewer/query", params={**COLA, "limit": 50, "offset": 0}, auth=_auth()).json()
    r = _node({"globales": _globales(), "cargar": _CARGAR,
               "respuestas": {"/api/db-viewer/query": {"status": 200, "cuerpo": cola}},
               "llamadas": [*_seleccion(), {"fn": "loadQueryData"}, {"fn": "descargarVistaBd"}]}, tmp_path)
    assert r["elementos"]["db-export-aviso"]["textContent"].startswith(
        "La vista tiene 120 filas: se exportan las primeras 5 en el orden elegido")
    filas = _csv(c.get(r["ubicacion"], auth=_auth()))
    assert filas[1][0].startswith("AVISO") and len(filas) == 2 + 5


def test_js_contenido_de_datos_siempre_escapado(visor, tmp_path):
    """Un nombre de columna o un valor con HTML no se interpreta en la grilla ni en el selector."""
    malo = '<img src=x onerror=alert(1)>'
    data = {"origen": "base", "columns": ["__rowid__", malo, "raw_data"], "rows": [[1, malo, json.dumps({"k": malo})]],
            "total": 1, "limit": 50, "offset": 0, "filtros_no_aplicados": [], "cifradas": [], "tipos": {},
            "editable": False, "cola": True, "orden": {"columna": malo, "sentido": "asc"},
            "columnas_json": ["raw_data"], "conteo_por_estado": {malo: 1},
            "sin_posicion": {"texto": malo, "filas": []}, "tope_exportacion": 10}
    r = _node({"globales": _globales(), "cargar": _CARGAR,
               "llamadas": [*_seleccion(), {"fn": "renderVisorBd", "args": [data, "x"]}]}, tmp_path)
    el = r["elementos"]
    for nombre in ("db-viewer-thead", "db-viewer-tbody", "db-columnas-panel"):
        assert "<img" not in el[nombre]["innerHTML"], nombre
    assert malo in el["db-resumen"]["textContent"], "texto plano (textContent), no HTML"
