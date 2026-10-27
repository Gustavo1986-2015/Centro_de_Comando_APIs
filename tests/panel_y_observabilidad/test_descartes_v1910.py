"""
v1.9.10 — Bloque 4: descartes con categorías, consulta, filtros, agrupado por
equipo, exportación, crudo y retención configurable.

Ejecutan los endpoints, los caminos reales de descarte (contrato, admisión,
mapeo del Studio y Tive por su webhook real), la migración y el JS real del
panel.
"""
import ast
import copy
import csv
import io
import json
import os
import shutil
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import descartes

RAIZ = Path(__file__).resolve().parents[2]
AHORA = time.time()


@pytest.fixture(autouse=True)
def descartes_propios(tmp_path, monkeypatch):
    """Registro de descartes propio de cada test, vacío."""
    descartes.esperar_escritura(3.0)
    monkeypatch.setattr(descartes, "DIRECTORIO", str(tmp_path / "desc"))
    descartes._migradas.clear()
    descartes.reset()
    yield descartes
    descartes.esperar_escritura(3.0)


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _cliente():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import descartes_api
    app = FastAPI()
    app.include_router(descartes_api.router)
    return TestClient(app)


def _insertar(filas):
    """Filas con su ts elegido: (ts, proveedor, env, origen, categoria, motivo, equipo, envio, ingest_id)."""
    con = descartes._conectar()
    con.executemany(
        "INSERT INTO descartes (ts, proveedor, env, origen, categoria, motivo, equipo, envio, ingest_id,"
        " latitud, longitud) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [f + ((-34.6, -58.4) if len(f) == 9 else ()) for f in filas])
    con.commit()
    con.close()


def _categorias_guardadas():
    descartes.esperar_escritura(3.0)
    con = descartes._conectar()
    try:
        return [tuple(r) for r in con.execute("SELECT origen, categoria, motivo FROM descartes ORDER BY id")]
    finally:
        con.close()


# ─── 1. Catálogo: el código no puede registrar sin categoría ────────────────

def _violaciones(texto: str, archivo: str = "x.py") -> list[str]:
    """
    Llamados a registrar() de descartes sin `categoria=`, o con una categoría
    literal fuera del catálogo; y llamados a _descartar() de Tive con una
    categoría literal fuera del catálogo.
    """
    malos = []
    for nodo in ast.walk(ast.parse(texto)):
        if not isinstance(nodo, ast.Call):
            continue
        nombre = nodo.func.attr if isinstance(nodo.func, ast.Attribute) else getattr(nodo.func, "id", None)
        dueno = getattr(nodo.func, "value", None)
        if nombre == "registrar" and isinstance(dueno, ast.Name) and dueno.id == "descartes":
            kw = {k.arg: k.value for k in nodo.keywords}
            if "categoria" not in kw:
                malos.append(f"{archivo}:{nodo.lineno} registrar() sin categoría")
            elif isinstance(kw["categoria"], ast.Constant) and kw["categoria"].value not in descartes.CATEGORIAS:
                malos.append(f"{archivo}:{nodo.lineno} categoría fuera del catálogo: {kw['categoria'].value!r}")
        if nombre == "_descartar" and len(nodo.args) >= 2 and isinstance(nodo.args[1], ast.Constant):
            if nodo.args[1].value not in descartes.CATEGORIAS:
                malos.append(f"{archivo}:{nodo.lineno} _descartar con categoría fuera del catálogo: {nodo.args[1].value!r}")
    # Los duplicados de Tive viajan como ("categoria", "motivo").
    for nodo in ast.walk(ast.parse(texto)):
        if (isinstance(nodo, ast.Tuple) and len(nodo.elts) == 2 and isinstance(nodo.elts[0], ast.Constant)
                and isinstance(nodo.elts[0].value, str) and nodo.elts[0].value.startswith("tive_duplicado")
                and nodo.elts[0].value not in descartes.CATEGORIAS):
            malos.append(f"{archivo}:{nodo.lineno} duplicado con categoría fuera del catálogo")
    return malos


def test_ningun_registrar_sin_categoria_ni_fuera_del_catalogo():
    malos, llamados = [], 0
    for ruta in sorted((RAIZ / "app").rglob("*.py")):
        texto = ruta.read_text(encoding="utf-8")
        malos += _violaciones(texto, str(ruta.relative_to(RAIZ)))
        llamados += texto.count("descartes.registrar(")
    assert not malos, "\n".join(malos)
    assert llamados >= 5, "no encontró los llamados: el test dejó de mirar el código"


def test_el_test_del_catalogo_detecta_un_registrar_sin_categoria():
    """Si alguien agrega un registrar() sin categoría (o con una inventada), el test de arriba falla."""
    assert _violaciones('descartes.registrar("p", "e", "tive", "motivo", equipo="X")')
    assert _violaciones('descartes.registrar("p", "e", "tive", "motivo", categoria="inventada")')
    assert _violaciones('_descartar(env, "inventada", "motivo", payload)')
    assert not _violaciones('descartes.registrar("p", "e", "tive", "m", categoria="sin_patente")')


def test_registrar_sin_categoria_no_se_puede_llamar():
    with pytest.raises(TypeError):
        descartes.registrar("tive", "prod", "tive", "motivo")


def test_una_categoria_fuera_del_catalogo_se_guarda_como_otro_y_avisa(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="app.core.descartes"):
        descartes.registrar("tive", "prod", "tive", "m", categoria="inventada")
    assert _categorias_guardadas() == [("tive", "otro", "m")]
    assert "no está en el catálogo" in caplog.text


def test_el_catalogo_esta_completo():
    for codigo, c in descartes.CATEGORIAS.items():
        assert c.nombre and c.descripcion and c.tipo in descartes.TIPOS, codigo
    assert {"sin_patente", "sin_coordenadas", "sin_fecha", "tive_retenido_vencido", "tive_sin_nombre_equipo",
            "tive_posicion_tracker_rc_directo", "tive_duplicado_alerta", "tive_duplicado_posicion",
            "otro"} <= set(descartes.CATEGORIAS)


# ─── 2. Los caminos reales registran su categoría ──────────────────────────

def test_contrato_registra_su_categoria():
    from app.core.contrato import filtrar_validos
    eventos = [SimpleNamespace(chassis_number=None, date=1, latitude=1.0, longitude=2.0, code="1", serial_number=None,
                               shipment=None),
               SimpleNamespace(chassis_number="AB1", date=None, latitude=1.0, longitude=2.0, code="1",
                               serial_number=None, shipment=None),
               SimpleNamespace(chassis_number="AB1", date=1, latitude=None, longitude=2.0, code="1",
                               serial_number=None, shipment=None)]
    filtrar_validos(eventos, "schmitz", "prod")
    assert [c[1] for c in _categorias_guardadas()] == ["sin_patente", "sin_fecha", "sin_coordenadas"]


def test_admision_registra_filtro_o_mal_configurada():
    from app.core import admision
    admision.registrar_descarte("studio", "prod", "solo con envío", "patente=AB1")
    admision.registrar_descarte("studio", "prod", "condición de admisión #1 mal configurada: falta el campo")
    assert [c[1] for c in _categorias_guardadas()] == ["admision_filtro", "admision_mal_configurada"]


def test_mapeo_del_studio_sin_patente():
    from app.core.dynamic_mapper import DynamicMapper
    esquema = {"base_mapping": {"chassis_number": "placa", "latitude": "lat", "longitude": "lon", "date": "fecha"}}
    DynamicMapper.map_payload_multi({"imei": "864035052734572", "lat": 1, "lon": 2, "fecha": "2026-10-23T12:00:00Z"},
                                    esquema, "studio", "prod")
    assert ("mapeo", "sin_patente") in [c[:2] for c in _categorias_guardadas()]


def test_tive_registra_su_categoria_con_los_crudos_reales(tmp_path, monkeypatch):
    from app.providers.tive import estado, modulo
    from tests.proveedores.tive.test_v192_tive import POSICION_TRACKER, MUESTRA_PRUEBA, _por_origen
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    try:
        modulo.procesar(_por_origen(POSICION_TRACKER), "prod", {}, "i1")
        modulo.procesar(_por_origen(MUESTRA_PRUEBA), "prod", {}, "i2")
        con = {"posiciones_equipos": True}
        modulo.procesar(_por_origen(POSICION_TRACKER), "prod", con, "i3")
        modulo.procesar(_por_origen(POSICION_TRACKER), "prod", con, "i4")      # la misma lectura otra vez
    finally:
        estado.cerrar_todo()
    descartes.esperar_escritura(3.0)
    c = descartes._conectar()
    filas = [tuple(r) for r in c.execute("SELECT categoria, ingest_id FROM descartes ORDER BY id")]
    c.close()
    assert filas == [("tive_posicion_tracker_rc_directo", "i1"), ("tive_boton_prueba", "i2"),
                     ("tive_duplicado_posicion", "i4")]


# ─── 3. Migración de una base vieja ────────────────────────────────────────

def test_migracion_de_una_base_vieja_completa_la_categoria():
    os.makedirs(descartes.DIRECTORIO, exist_ok=True)
    ruta = os.path.join(descartes.DIRECTORIO, descartes.ARCHIVO)
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE descartes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, proveedor TEXT NOT NULL,"
                " env TEXT, origen TEXT NOT NULL, motivo TEXT NOT NULL, detalle TEXT, equipo TEXT, envio TEXT,"
                " alert_id TEXT, latitud REAL, longitud REAL)")
    viejas = [("tive", "posición de tracker, va por RC directo"), ("tive", "duplicado de ShockEvents (puntual) ya recibido"),
              ("tive", "actualización de una alerta abierta que ya se envió"), ("tive", "alerta de beacon, interruptor apagado"),
              ("tive", "tramo retenido 24 h sin conocer el nombre del tracker"), ("tive", "alerta sin patente resoluble"),
              ("tive", "evento sin nombre de equipo y sin coordenadas en Location: no es un tramo de tercero utilizable"),
              ("contrato", "falta patente, fecha"), ("contrato", "falta latitud, longitud"), ("contrato", "falta fecha"),
              ("mapeo", "sin patente real: el payload no trae el campo de patente configurado"),
              ("admision", "solo con envío"), ("admision", "condición de admisión #2 mal configurada: falta el campo"),
              ("tive", "algo que no existe")]
    con.executemany("INSERT INTO descartes (ts, proveedor, env, origen, motivo) VALUES (1, 'x', 'prod', ?, ?)", viejas)
    con.commit()
    con.close()
    for _ in range(2):                                     # idempotente
        descartes._migradas.clear()
        descartes._conectar().close()
    con = sqlite3.connect(ruta)
    categorias = [r[0] for r in con.execute("SELECT categoria FROM descartes ORDER BY id")]
    indices = {r[1] for r in con.execute("PRAGMA index_list(descartes)")}
    columnas = [r[1] for r in con.execute("PRAGMA table_info(descartes)")]
    con.close()
    assert categorias == ["tive_posicion_tracker_rc_directo", "tive_duplicado_alerta", "tive_duplicado_alerta",
                          "tive_alerta_interruptor_apagado", "tive_retenido_vencido", "tive_sin_nombre_equipo",
                          "sin_coordenadas", "sin_patente", "sin_coordenadas", "sin_fecha", "sin_patente",
                          "admision_filtro", "admision_mal_configurada", "otro"]
    assert {"idx_descartes_ts", "idx_descartes_categoria_ts", "idx_descartes_proveedor_env_ts"} <= indices
    assert columnas.count("categoria") == 1 and columnas.count("ingest_id") == 1


# ─── 4. Consulta: filtros, paginación y totales ────────────────────────────

def _caso():
    hora = 3600
    _insertar([
        (AHORA - 1 * hora, "tive", "prod", "tive", "tive_posicion_tracker_rc_directo", "posición de tracker", "K1", "V1", "i1"),
        (AHORA - 2 * hora, "tive", "prod", "tive", "tive_posicion_tracker_rc_directo", "posición de tracker", "K1", "V2", "i2"),
        (AHORA - 3 * hora, "tive", "prod", "tive", "tive_sin_nombre_equipo", "alerta sin patente resoluble", "K2", "V3", None),
        (AHORA - 4 * hora, "studio", "prod", "mapeo", "sin_patente", "sin patente real", "864035052734572", None, "i4"),
        (AHORA - 5 * hora, "schmitz", "test", "contrato", "sin_fecha", "falta fecha", "R%_1", "E_5", None),
        (AHORA - 30 * hora, "tive", "prod", "tive", "sin_coordenadas", "viejo", "K9", None, None),
    ])


def test_por_defecto_ultimas_24_h_y_todo_tipo():
    _caso()
    d = _cliente().get("/api/descartes", auth=_auth()).json()
    assert d["total"] == 5, "por defecto, las últimas 24 h"
    assert d["por_categoria"] == {"tive_posicion_tracker_rc_directo": 2, "tive_sin_nombre_equipo": 1,
                                  "sin_patente": 1, "sin_fecha": 1}
    assert abs(d["mas_viejo"] - (AHORA - 30 * 3600)) < 1


def test_filtros_por_categorias_tipo_integracion_y_texto():
    _caso()
    c = _cliente()
    q = lambda **p: c.get("/api/descartes", params=p, auth=_auth()).json()
    assert q(tipo="a_revisar")["total"] == 3
    assert q(tipo="esperado")["total"] == 2
    d = q(categorias="sin_patente,sin_fecha")
    assert d["total"] == 2 and {f["categoria"] for f in d["filas"]} == {"sin_patente", "sin_fecha"}
    assert d["por_categoria"]["tive_posicion_tracker_rc_directo"] == 2, "las tarjetas cuentan todo el rango"
    assert q(categorias="sin_patente", tipo="esperado")["total"] == 0
    assert q(provider="schmitz", env="test")["total"] == 1
    assert q(texto="k1")["total"] == 2 and q(texto="V3")["total"] == 1
    assert q(texto="%")["total"] == 1, "el % del texto se busca literal, no como comodín"
    assert q(texto="_")["total"] == 1
    desde = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(AHORA - 48 * 3600))
    assert q(desde=desde)["total"] == 6


def test_paginacion_en_el_servidor():
    _caso()
    c = _cliente()
    paginas = [c.get("/api/descartes", params={"limit": 2, "offset": o}, auth=_auth()).json() for o in (0, 2, 4)]
    assert [p["total"] for p in paginas] == [5, 5, 5]
    assert [len(p["filas"]) for p in paginas] == [2, 2, 1]
    ts = [f["ts"] for p in paginas for f in p["filas"]]
    assert ts == sorted(ts, reverse=True) and len(set(ts)) == 5


def test_agrupado_por_equipo():
    _caso()
    _insertar([(AHORA - 0.5 * 3600, "tive", "prod", "tive", "tive_posicion_tracker_rc_directo", "x", "K1", "V9", None)])
    d = _cliente().get("/api/descartes/por-equipo", params={"tipo": "todos"}, auth=_auth()).json()
    k1 = next(g for g in d["grupos"] if g["equipo"] == "K1")
    assert (k1["cantidad"], k1["proveedor"], k1["env"], k1["categoria"]) == (3, "tive", "prod", "tive_posicion_tracker_rc_directo")
    assert k1["ultimo_envio"] == "V9" and k1["primero"] < k1["ultimo"]   # el del más reciente, no el menor
    assert d["total"] == 4 and d["grupos"][0]["equipo"] == "K1"   # el de hace 30 h queda fuera de las 24 h


@pytest.mark.parametrize("params", [
    {"categorias": "inventada"}, {"tipo": "raro"}, {"provider": "../db"}, {"env": "prod;"}, {"desde": "ayer"},
    {"limit": "abc"}, {"limit": "0"}, {"limit": "501"}, {"offset": "-1"}, {"texto": "x" * 101},
    {"desde": "2026-10-23T10:00", "hasta": "2026-10-23T09:00"},
])
def test_parametros_invalidos_400(params):
    r = _cliente().get("/api/descartes", params=params, auth=_auth())
    assert r.status_code == 400, (params, r.text)


def test_exige_autenticacion():
    c = _cliente()
    for ruta in ("/api/descartes", "/api/descartes/catalogo", "/api/descartes/por-equipo",
                 "/api/descartes/exportar", "/api/descartes/retencion", "/api/descartes/1/crudo"):
        assert c.get(ruta).status_code == 401, ruta


# ─── 5. Exportación con los mismos filtros ─────────────────────────────────

def test_exportar_csv_con_los_mismos_filtros():
    _caso()
    c = _cliente()
    r = c.get("/api/descartes/exportar", params={"formato": "csv", "tipo": "a_revisar"}, auth=_auth())
    assert r.status_code == 200 and r.headers["x-descartes-recortado"] == "false"
    texto = r.content.decode("utf-8")
    assert texto.startswith("﻿")
    filas = list(csv.reader(io.StringIO(texto.lstrip("﻿")), delimiter=";"))
    assert filas[0][:3] == ["fecha_local", "fecha_utc", "integracion"]
    assert len(filas) - 1 == c.get("/api/descartes", params={"tipo": "a_revisar"}, auth=_auth()).json()["total"] == 3
    assert {f[4] for f in filas[1:]} == {"tive_sin_nombre_equipo", "sin_patente", "sin_fecha"}
    assert all(f[0] != f[1] or time.timezone == 0 for f in filas[1:]), "hora local y UTC"


def test_exportar_excel_con_los_mismos_filtros():
    _caso()
    r = _cliente().get("/api/descartes/exportar", params={"formato": "xlsx", "provider": "tive"}, auth=_auth())
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        hoja = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "xl/workbook.xml" in z.namelist()
    assert hoja.count("<row ") == 1 + 3                    # encabezado + los 3 de Tive del rango
    assert "tive_posicion_tracker_rc_directo" in hoja and "studio" not in hoja


def test_exportar_avisa_si_recorta(monkeypatch):
    from app.api.routers import descartes_api
    _caso()
    monkeypatch.setattr(descartes_api, "TOPE_EXPORTACION", 2)
    r = _cliente().get("/api/descartes/exportar", params={"formato": "csv", "tipo": "todos"}, auth=_auth())
    filas = list(csv.reader(io.StringIO(r.content.decode("utf-8").lstrip("﻿")), delimiter=";"))
    assert r.headers["x-descartes-recortado"] == "true"
    assert filas[1][0].startswith("AVISO: se exportan las 2 más recientes")
    assert len(filas) == 1 + 1 + 2


def test_exportar_formato_invalido():
    assert _cliente().get("/api/descartes/exportar", params={"formato": "pdf"}, auth=_auth()).status_code == 400


# ─── 6. Retención configurable ─────────────────────────────────────────────

def test_retencion_configurable_con_limites(config_aislada):
    from app.core import config_cache
    config_cache.invalidate()
    c = _cliente()
    d = c.get("/api/descartes/retencion", auth=_auth()).json()
    assert (d["dias"], d["max_filas"]) == (7, 50_000), "por defecto, lo de siempre"
    assert d["limites_dias"] == [1, 90] and d["limites_filas"] == [10_000, 1_000_000]
    for cuerpo in ({"dias": 0, "max_filas": 50_000}, {"dias": 91, "max_filas": 50_000},
                   {"dias": 7, "max_filas": 9_999}, {"dias": 7, "max_filas": 1_000_001}):
        assert c.put("/api/descartes/retencion", json=cuerpo, auth=_auth()).status_code == 400, cuerpo
    _insertar([(AHORA - 3 * 86400, "tive", "prod", "tive", "otro", "viejo", "K", None, None),
               (AHORA - 3600, "tive", "prod", "tive", "otro", "nuevo", "K", None, None)])
    r = c.put("/api/descartes/retencion", json={"dias": 2, "max_filas": 10_000}, auth=_auth())
    assert r.status_code == 200 and (r.json()["dias"], r.json()["max_filas"]) == (2, 10_000)
    con = descartes._conectar()
    assert [m for (m,) in con.execute("SELECT motivo FROM descartes")] == ["nuevo"], "se aplicó en el momento"
    con.close()
    assert abs(r.json()["mas_viejo"] - (AHORA - 3600)) < 1
    assert descartes.retencion_vigente() == (2, 10_000)


# ─── 7. Vínculo con el crudo, por el webhook real de Tive ──────────────────

def test_cableado_ver_el_crudo_de_un_descarte_de_tive(app_tive, tmp_path):
    from tests.proveedores.tive.test_v192_tive import POSICION_TRACKER, _por_origen, _post
    from app.core.auditor import AUDIT_DIR
    payload = _por_origen(POSICION_TRACKER)
    assert _post(app_tive, payload).status_code == 200      # va por RC directo: se descarta
    descartes.esperar_escritura(3.0)
    c = _cliente()
    (fila,) = c.get("/api/descartes", auth=_auth(), params={"tipo": "todos"}).json()["filas"]
    assert fila["categoria"] == "tive_posicion_tracker_rc_directo" and fila["ingest_id"]
    limite = time.time() + 10
    r = None
    while time.time() < limite:                              # el crudo se escribe en otro hilo
        r = c.get(f"/api/descartes/{fila['id']}/crudo", auth=_auth())
        if r.status_code == 200:
            break
        time.sleep(0.2)
    assert r.status_code == 200, r.text
    assert r.json()["payload"] == payload and r.json()["ingest_id"] == fila["ingest_id"]
    assert r.json()["archivo"].startswith(f"{AUDIT_DIR}/tive_prod/")
    # Purgado el crudo, se dice claro.
    shutil.rmtree(AUDIT_DIR)
    r = c.get(f"/api/descartes/{fila['id']}/crudo", auth=_auth())
    assert r.status_code == 404 and "ya no está disponible" in r.json()["detail"]


def test_sin_ingest_id_lo_dice():
    _insertar([(AHORA, "schmitz", "prod", "contrato", "sin_fecha", "falta fecha", "R1", None, None)])
    d = _cliente().get("/api/descartes", params={"tipo": "todos"}, auth=_auth()).json()
    r = _cliente().get(f"/api/descartes/{d['filas'][0]['id']}/crudo", auth=_auth())
    assert r.status_code == 404 and "no tiene vínculo" in r.json()["detail"]


# app_tive (fixture del webhook real de Tive) se importa para el test de arriba.
from tests.proveedores.tive.test_v192_tive import app_tive  # noqa: E402,F401


# ─── 8. JS real del panel ──────────────────────────────────────────────────

def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


def _respuestas_reales():
    c = _cliente()
    return {
        "/api/descartes/catalogo": {"status": 200, "cuerpo": c.get("/api/descartes/catalogo", auth=_auth()).json()},
        "/api/descartes": {"status": 200, "cuerpo": c.get("/api/descartes", params={"tipo": "todos"}, auth=_auth()).json()},
        "/api/descartes/por-equipo": {"status": 200, "cuerpo": c.get("/api/descartes/por-equipo", auth=_auth()).json()},
        "/api/config": {"status": 200, "cuerpo": [{"provider_name": "TIVE", "env": "prod"}]},
    }


CARGAR = ["_escapeHtml", "_escAttr", "_fechaLocalInput", "_textoFechaDescarte", "_categoriaDescarte",
          "_paramsDescartes", "_renderTarjetasDescartes", "_renderTablaDescartes", "_renderPorEquipoDescartes",
          "_renderPaginacionDescartes", "_enlaceMapa"]


def test_cableado_seccion_descartes_js_real(tmp_path):
    """
    Respuestas REALES del servidor → cargarSeccionDescartes y buscarDescartes REALES:
    por defecto últimas 24 h y "a revisar"; tarjetas por categoría; tabla con los
    datos escapados, enlace a mapa y "Ver crudo" solo con ingest_id; clic en una
    tarjeta filtra; pestaña "Por equipo"; exportar con los mismos filtros.
    """
    _caso()
    _insertar([(AHORA - 60, "tive", "prod", "tive", "sin_patente", "<b>m</b>", '<img src=x onerror="1">', "E1", None)])
    respuestas = _respuestas_reales()
    r = _node({"globales": {}, "cargar": CARGAR, "respuestas": respuestas, "llamadas": [
        {"fn": "cargarSeccionDescartes"},
        {"fn": "filtrarCategoriaDescartes", "args": ["sin_patente"]},
        {"fn": "pestanaDescartes", "args": ["equipos"]},
        {"fn": "exportarDescartes", "args": ["xlsx"]},
    ]}, tmp_path)
    urls = [p["url"] for p in r["fetch"]]
    primera = next(u for u in urls if u.startswith("/api/descartes?"))
    assert "tipo=a_revisar" in primera and "desde=" in primera and "limit=50" in primera
    filtrada = [u for u in urls if u.startswith("/api/descartes?")][1]
    assert "categorias=sin_patente" in filtrada and "tipo=todos" in filtrada
    el = r["elementos"]
    tarjetas = el["desc-tarjetas"]["innerHTML"]
    assert "Tive: posición de equipo (va por RC directo)" in tarjetas and "Sin patente" in tarjetas
    assert "data-categoria=\"sin_patente\"" in tarjetas
    # La última pestaña fue "Por equipo".
    assert "Último envío" in el["desc-tabla"]["innerHTML"]
    assert el["desc-mas-viejo"]["textContent"].startswith("Descarte más viejo disponible:")
    exportar = r["ubicacion"]
    assert exportar.startswith("/api/descartes/exportar?") and "formato=xlsx" in exportar
    assert "categorias=sin_patente" in exportar and "tipo=todos" in exportar, "exportó sin los filtros aplicados"


def test_cableado_tabla_escapa_y_ofrece_crudo_js_real(tmp_path):
    _caso()
    _insertar([(AHORA - 60, "tive", "prod", "tive", "sin_patente", "<b>m</b>", '<img src=x onerror="1">', "E1", None)])
    respuestas = _respuestas_reales()
    filas = respuestas["/api/descartes"]["cuerpo"]["filas"]
    r = _node({"globales": {"_desc": {"catalogo": respuestas["/api/descartes/catalogo"]["cuerpo"], "categorias": [],
                                      "pagina": 0, "porPagina": 50, "pestana": "lista"}},
               "cargar": CARGAR,
               "llamadas": [{"fn": "_renderTablaDescartes", "args": [filas]},
                            {"fn": "_renderPaginacionDescartes", "args": [120]}]}, tmp_path)
    tabla, paginas = r["resultados"]
    assert "<img" not in tabla and "&lt;img" in tabla and "&lt;b&gt;m&lt;/b&gt;" in tabla
    assert tabla.count("Ver crudo") == sum(1 for f in filas if f["ingest_id"])
    assert "https://www.google.com/maps?q=-34.6,-58.4" in tabla
    assert "Página 1 de 3" in paginas


def test_cableado_crudo_purgado_y_retencion_js_real(tmp_path):
    r = _node({"cargar": CARGAR, "respuestas": {
        "/api/descartes/7/crudo": {"status": 404, "cuerpo": {"detail": "El crudo de este descarte ya no está disponible: se purgó"}},
        "/api/descartes/retencion": {"status": 400, "cuerpo": {"detail": "Los días de retención de descartes tienen que estar entre 1 y 90."}},
    }, "llamadas": [
        {"fn": "verCrudoDescarte", "args": [7]},
        {"set": {"id": "desc-ret-dias", "value": "0"}},
        {"set": {"id": "desc-ret-filas", "value": "50000"}},
        {"fn": "guardarRetencionDescartes"},
    ]}, tmp_path)
    el = r["elementos"]
    assert el["desc-crudo-contenido"]["textContent"].startswith("El crudo de este descarte ya no está disponible")
    assert el["desc-crudo-modal"]["style"]["display"] == "flex"
    assert el["desc-ret-info"]["textContent"].startswith("No se guardó: Los días de retención")
    pedido = next(p for p in r["fetch"] if p["url"] == "/api/descartes/retencion")
    assert json.loads(pedido["opciones"]["body"]) == {"dias": 0, "max_filas": 50000}


def test_cableado_webhook_generico_vincula_admision_y_mapeo_con_su_crudo(config_aislada):
    """Studio por el webhook real: el descarte por admisión y el de sin patente quedan con el ingest_id de su crudo."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    esquema = {"base_mapping": {"chassis_number": "placa", "latitude": "lat", "longitude": "lon", "date": "fecha"},
               "admision": [{"field": "tipo", "operator": "neq", "value": "prueba", "label": "sin pruebas"}]}
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="studio", env="prod", provider_type="push", is_active=True, use_mock=True,
                          webhook_auth_secret_enc=encrypt("CLAVE"), mapping_schema=esquema))
    db.commit()
    db.close()
    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    c = TestClient(app)
    base = {"lat": 1.0, "lon": 2.0, "fecha": "2026-10-23T12:00:00Z"}
    assert c.post("/webhook/dynamic/studio?env=prod", json=dict(base, placa="AB1", tipo="prueba"),
                  headers={"x-api-key": "CLAVE"}).status_code == 200
    assert c.post("/webhook/dynamic/studio?env=prod", json=dict(base, imei="864035052734572", tipo="real"),
                  headers={"x-api-key": "CLAVE"}).status_code == 200
    descartes.esperar_escritura(3.0)
    api = _cliente()
    filas = api.get("/api/descartes", params={"tipo": "todos"}, auth=_auth()).json()["filas"]
    por_categoria = {f["categoria"]: f for f in filas}
    assert set(por_categoria) == {"admision_filtro", "sin_patente"}
    assert all(f["ingest_id"] for f in filas) and len({f["ingest_id"] for f in filas}) == 2
    for categoria, tipo in (("admision_filtro", "prueba"), ("sin_patente", "real")):
        limite, r = time.time() + 10, None
        while time.time() < limite:
            r = api.get(f"/api/descartes/{por_categoria[categoria]['id']}/crudo", auth=_auth())
            if r.status_code == 200:
                break
            time.sleep(0.2)
        assert r.status_code == 200 and r.json()["payload"]["tipo"] == tipo, (categoria, r.text)


def test_la_pagina_tiene_la_seccion_el_enlace_y_la_retencion(config_aislada):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard
    app = FastAPI()
    app.include_router(dashboard.router)
    html = TestClient(app).get("/dashboard", auth=_auth()).text
    assert 'id="tab-descartes"' in html and 'id="view-descartes"' in html
    assert 'id="desc-enlace-seccion"' in html and "switchView('descartes')" in html, "falta el enlace desde Diagnóstico"
    assert 'id="desc-ret-dias"' in html and 'id="desc-ret-filas"' in html
    assert 'value="a_revisar"' in html


def test_la_fecha_se_muestra_en_24_h_hora_local_js_real(tmp_path):
    """Con es-419, toLocaleString() daba 12 h sin a. m./p. m.: 19:31 se leía 07:31."""
    import datetime as _dt
    ts = _dt.datetime(2026, 10, 23, 19, 31, 43).timestamp()      # hora local del sistema
    r = _node({"cargar": CARGAR, "llamadas": [{"fn": "_textoFechaDescarte", "args": [ts]}]}, tmp_path)
    assert r["resultados"] == ["2026-10-23 19:31:43"]
