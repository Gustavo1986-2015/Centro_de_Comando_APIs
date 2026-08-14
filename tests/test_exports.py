"""
Regresión de las descargas de crudos y de enviado-a-RC.

Puntos que cubre, cada uno por una razón concreta:
  · Auth obligatoria — son datos operativos completos del cliente.
  · Tope de días — un día de crudos a caudal de certificación son ~2 GB.
  · Recorrido de rutas — provider y env viajan al filesystem.
  · Crudos SIN transformar — la gracia es mostrarle al proveedor lo que mandó.
  · Enviados unificando base + respaldos, sin duplicar ni perder filas.
  · Filtro por created_at real y no por nombre de archivo: el archivo se nombra
    por el día de la PURGA, que puede ser posterior al día del evento.
"""
import csv
import io
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.routers import exports


@pytest.fixture
def auth():
    """
    Credenciales leídas en el momento, no al importar el módulo.

    Dos tests de la suite (test_operacion_y_seguridad y test_backoff_recovery)
    escriben DASHBOARD_USER/DASHBOARD_PASSWORD con os.environ directo, sin
    monkeypatch y sin restaurarlos. Hardcodear acá los valores de conftest hacía
    que estos tests pasaran aislados y fallaran con 401 dentro de la suite, según
    el orden de ejecución. Leyendo el entorno vigente el resultado no depende de
    quién corrió antes.
    """
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


@pytest.fixture
def app_exports(tmp_path, monkeypatch):
    """App mínima con solo el router de exportaciones, sobre un disco temporal."""
    from fastapi import FastAPI

    monkeypatch.chdir(tmp_path)
    app = FastAPI()
    app.include_router(exports.router)
    return TestClient(app)


@pytest.fixture
def settings_tope(monkeypatch):
    """Fija el tope de días sin depender de la base real."""
    def _fijar(dias=7, audit=30, procesados=30):
        class _S:
            export_max_days = dias
            audit_retention_days = audit
            processed_retention_days = procesados
        monkeypatch.setattr(exports.config_cache, "get_settings", lambda: _S())
    return _fijar


def _escribir_jsonl(base, carpeta, prefijo, dia, registros):
    ruta = os.path.join(base, carpeta, dia[:7])
    os.makedirs(ruta, exist_ok=True)
    destino = os.path.join(ruta, f"{prefijo}_{dia}.jsonl")
    with open(destino, "a", encoding="utf-8") as f:
        for r in registros:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return destino


# ─── Autenticación ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("ruta", ["/api/export/crudos", "/api/export/enviados"])
def test_las_exportaciones_exigen_autenticacion(app_exports, settings_tope, ruta):
    """Sin auth se filtraría la telemetría completa del cliente."""
    settings_tope()
    r = app_exports.get(
        ruta, params={"provider": "schmitz", "env": "test",
                      "desde": "2026-08-01", "hasta": "2026-08-01"}
    )
    assert r.status_code == 401


def test_opciones_exige_autenticacion(app_exports, settings_tope):
    settings_tope()
    assert app_exports.get("/api/export/opciones").status_code == 401


# ─── Validación de rango ─────────────────────────────────────────────────────

def test_rango_mayor_al_tope_se_frena_con_mensaje_claro(app_exports, settings_tope, auth):
    """Gustavo pidió explícitamente que frene antes de tumbar el navegador."""
    settings_tope(dias=7)
    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-31"},
    )
    assert r.status_code == 400
    detalle = r.json()["detail"]
    assert "31 días" in detalle and "7" in detalle


def test_el_tope_es_configurable_y_se_respeta(app_exports, settings_tope, auth):
    settings_tope(dias=31)
    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-31"},
    )
    assert r.status_code == 200


def test_rango_invertido_se_rechaza(app_exports, settings_tope, auth):
    settings_tope()
    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-10", "hasta": "2026-08-01"},
    )
    assert r.status_code == 400


def test_fecha_con_formato_invalido_se_rechaza(app_exports, settings_tope, auth):
    settings_tope()
    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "01/08/2026", "hasta": "2026-08-01"},
    )
    assert r.status_code == 400


# ─── Seguridad de rutas ──────────────────────────────────────────────────────

@pytest.mark.parametrize("malicioso", ["../../etc", "schmitz/../..", "a b", "prov;rm"])
def test_provider_no_puede_escaparse_del_directorio(app_exports, settings_tope, malicioso, auth):
    """provider y env se concatenan a rutas de disco: lista blanca estricta."""
    settings_tope()
    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": malicioso, "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    assert r.status_code == 400


# ─── Crudos ──────────────────────────────────────────────────────────────────

def test_los_crudos_salen_sin_transformar(app_exports, settings_tope, tmp_path, auth):
    """
    El payload tiene que salir idéntico a como lo mandó el proveedor: la función
    existe para poder mostrárselo al proveedor y discutir con el dato en la mano.
    """
    settings_tope()
    payload = {"ChassisNumber": "AB1234", "Position": {"Latitude": -34.6}, "Nested": {"a": [1, 2]}}
    _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", "2026-08-01",
                    [{"timestamp": "2026-08-01T10:00:00+00:00", "provider": "schmitz",
                      "env": "test", "payload": payload}])

    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    assert r.status_code == 200
    lineas = [json.loads(l) for l in r.text.strip().split("\n")]
    assert len(lineas) == 1
    assert lineas[0]["payload"] == payload


def test_crudos_fuera_del_rango_no_se_incluyen(app_exports, settings_tope, tmp_path, auth):
    settings_tope()
    for dia in ("2026-07-31", "2026-08-01", "2026-08-02"):
        _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", dia,
                        [{"timestamp": f"{dia}T10:00:00+00:00", "payload": {"d": dia}}])

    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    dias = [json.loads(l)["payload"]["d"] for l in r.text.strip().split("\n")]
    assert dias == ["2026-08-01"]


def test_crudos_con_todos_los_proveedores_marca_el_origen(app_exports, settings_tope, tmp_path, auth):
    """Con provider=todos hay que poder separar de dónde vino cada línea."""
    settings_tope()
    for carpeta in ("schmitz_test", "protrack_prod"):
        _escribir_jsonl(tmp_path / "audit", carpeta, "crudos", "2026-08-01",
                        [{"timestamp": "2026-08-01T10:00:00+00:00", "payload": {"x": 1}}])

    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "todos", "env": "todos",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    origenes = {(json.loads(l)["provider"], json.loads(l)["env"])
                for l in r.text.strip().split("\n")}
    assert origenes == {("schmitz", "test"), ("protrack", "prod")}


def test_env_filtra_aunque_el_proveedor_sea_todos(app_exports, settings_tope, tmp_path, auth):
    settings_tope()
    for carpeta in ("schmitz_test", "schmitz_prod"):
        _escribir_jsonl(tmp_path / "audit", carpeta, "crudos", "2026-08-01",
                        [{"timestamp": "2026-08-01T10:00:00+00:00", "payload": {"c": carpeta}}])

    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "todos", "env": "prod",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    assert {json.loads(l)["env"] for l in r.text.strip().split("\n")} == {"prod"}


def test_sin_archivos_devuelve_vacio_y_no_error(app_exports, settings_tope, auth):
    """Pedir un rango sin datos es normal, no una falla."""
    settings_tope()
    r = app_exports.get(
        "/api/export/crudos", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    assert r.status_code == 200
    assert r.text.strip() == ""


# ─── Enviado a RC ────────────────────────────────────────────────────────────

def _leer_csv(texto):
    return list(csv.DictReader(io.StringIO(texto.lstrip("\ufeff")), delimiter=";"))


def test_enviados_incluye_code_y_job_id(app_exports, settings_tope, tmp_path, auth):
    """El CSV tiene que servir para auditar QUÉ se despachó y con qué acuse."""
    settings_tope()
    _escribir_jsonl(
        tmp_path / "db" / "backups_diarios", "schmitz_test", "procesados", "2026-08-01",
        [{"id": 1, "provider": "schmitz", "env": "test", "chassis": "AB1234",
          "status": "sent", "created_at": "2026-08-01T10:00:00", "code": "11",
          "job_id": "J-99", "response": '{"idJob":"J-99"}'}],
    )
    r = app_exports.get(
        "/api/export/enviados", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    filas = _leer_csv(r.text)
    assert len(filas) == 1
    assert filas[0]["code"] == "11"
    assert filas[0]["job_id"] == "J-99"
    assert filas[0]["origen"] == "respaldo"


def test_enviados_filtra_por_fecha_del_evento_no_del_archivo(app_exports, settings_tope, tmp_path, auth):
    """
    El archivo se nombra por el día de la PURGA. Un evento del 1 puede terminar
    escrito en procesados_2026-08-02.jsonl. Filtrar por nombre de archivo lo
    perdería, y en una auditoría eso es inaceptable.
    """
    settings_tope()
    _escribir_jsonl(
        tmp_path / "db" / "backups_diarios", "schmitz_test", "procesados", "2026-08-02",
        [
            {"id": 1, "chassis": "A", "created_at": "2026-08-01T23:59:00", "code": "1"},
            {"id": 2, "chassis": "B", "created_at": "2026-08-02T00:30:00", "code": "1"},
        ],
    )
    r = app_exports.get(
        "/api/export/enviados", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    filas = _leer_csv(r.text)
    assert [f["chassis"] for f in filas] == ["A"], "Se perdió el evento del día 1"


def test_enviados_unifica_base_y_respaldo_sin_duplicar(app_exports, settings_tope, tmp_path, auth):
    """
    Las dos fuentes en una sola descarga: si hay que ir a un lado para lo de hace
    una hora y a otro para lo de ayer, la función no sirve. Y si la purga escribió
    el respaldo pero falló al borrar, la fila no puede salir dos veces.
    """
    settings_tope()
    from app.database import get_session, check_and_migrate_provider_db
    from app.models.db_models import NormalizedRCEvent

    hoy = datetime.now().date()
    check_and_migrate_provider_db("schmitz", "test")
    db = get_session("schmitz", "test")
    momento = datetime.combine(hoy, datetime.min.time()) + timedelta(hours=10)
    db.add(NormalizedRCEvent(id=7, provider="schmitz", status="sent",
                             chassis_number="EN_BASE", code="1", created_at=momento))
    db.commit()
    db.close()

    _escribir_jsonl(
        tmp_path / "db" / "backups_diarios", "schmitz_test", "procesados",
        hoy.isoformat(),
        [
            # id 7 = el mismo que quedó en la base: no debe repetirse
            {"id": 7, "chassis": "EN_BASE", "created_at": momento.isoformat(), "code": "1"},
            {"id": 8, "chassis": "PURGADO", "created_at": momento.isoformat(), "code": "11"},
        ],
    )

    r = app_exports.get(
        "/api/export/enviados", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": hoy.isoformat(), "hasta": hoy.isoformat()},
    )
    filas = _leer_csv(r.text)
    chassis = sorted(f["chassis"] for f in filas)
    assert chassis == ["EN_BASE", "PURGADO"]
    origenes = {f["chassis"]: f["origen"] for f in filas}
    assert origenes["EN_BASE"] == "base"
    assert origenes["PURGADO"] == "respaldo"


def test_el_csv_lleva_bom_para_que_excel_no_rompa_acentos(app_exports, settings_tope, auth):
    settings_tope()
    r = app_exports.get(
        "/api/export/enviados", auth=auth,
        params={"provider": "schmitz", "env": "test",
                "desde": "2026-08-01", "hasta": "2026-08-01"},
    )
    assert r.text.startswith("\ufeff")
    assert "code" in r.text.split("\n")[0]


def test_el_nombre_del_archivo_identifica_el_contenido(app_exports, settings_tope, auth):
    """Bajar cinco reportes y no saber cuál es cuál ya pasó."""
    settings_tope()
    r = app_exports.get(
        "/api/export/enviados", auth=auth,
        params={"provider": "schmitz", "env": "prod",
                "desde": "2026-08-01", "hasta": "2026-08-03"},
    )
    cd = r.headers["content-disposition"]
    assert "enviado_a_rc_schmitz_prod_2026-08-01_a_2026-08-03.csv" in cd


# ─── Opciones ────────────────────────────────────────────────────────────────

def test_opciones_lista_solo_combinaciones_con_datos(app_exports, settings_tope, tmp_path, auth):
    settings_tope(dias=5)
    _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", "2026-08-01", [{"x": 1}])
    _escribir_jsonl(tmp_path / "db" / "backups_diarios", "protrack_prod",
                    "procesados", "2026-08-01", [{"x": 1}])

    datos = app_exports.get("/api/export/opciones", auth=auth).json()
    combos = {(c["provider"], c["env"]) for c in datos["combinaciones"]}
    assert ("schmitz", "test") in combos
    assert ("protrack", "prod") in combos
    assert datos["max_dias"] == 5


# ─── Inventario en disco ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _inventario_sin_cache():
    """
    El caché de 60 s es correcto en producción pero envenena los tests: el
    segundo test vería el inventario del primero. Se limpia antes y después.
    """
    exports._inventario_cache.update({"datos": None, "expira": 0.0})
    yield
    exports._inventario_cache.update({"datos": None, "expira": 0.0})


def test_inventario_exige_autenticacion(app_exports, settings_tope):
    settings_tope()
    assert app_exports.get("/api/export/inventario").status_code == 401


def test_inventario_informa_rango_dias_y_tamano(app_exports, settings_tope, tmp_path, auth):
    """
    La pregunta que contesta el inventario es "¿qué tengo y desde cuándo?".
    La fecha más vieja es el borde de lo que todavía se puede rescatar.
    """
    settings_tope()
    for dia in ("2026-08-01", "2026-08-02", "2026-08-03"):
        _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", dia,
                        [{"payload": {"relleno": "x" * 100}}])

    fila = app_exports.get("/api/export/inventario", auth=auth).json()["filas"][0]
    assert (fila["provider"], fila["env"]) == ("schmitz", "test")
    assert fila["crudos"]["desde"] == "2026-08-01"
    assert fila["crudos"]["hasta"] == "2026-08-03"
    assert fila["crudos"]["dias_con_datos"] == 3
    assert fila["crudos"]["bytes"] > 300


def test_inventario_delata_los_dias_faltantes(app_exports, settings_tope, tmp_path, auth):
    """
    Con un hueco en el medio (corte de ingesta, proveedor que dejó de mandar),
    los días con datos y el rango calendario no coinciden. Verlo antes de pedir
    la descarga evita creer que salió vacía por un error.
    """
    settings_tope()
    for dia in ("2026-08-01", "2026-08-05"):
        _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", dia, [{"x": 1}])

    crudos = app_exports.get("/api/export/inventario", auth=auth).json()["filas"][0]["crudos"]
    assert crudos["dias_con_datos"] == 2
    assert crudos["dias_rango"] == 5


def test_inventario_separa_crudos_de_enviados(app_exports, settings_tope, tmp_path, auth):
    """Son dos descargas distintas y pueden cubrir rangos distintos."""
    settings_tope()
    _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", "2026-07-20", [{"x": 1}])
    _escribir_jsonl(tmp_path / "db" / "backups_diarios", "schmitz_test",
                    "procesados", "2026-08-10", [{"x": 1}])

    fila = app_exports.get("/api/export/inventario", auth=auth).json()["filas"][0]
    assert fila["crudos"]["desde"] == "2026-07-20"
    assert fila["enviados"]["desde"] == "2026-08-10"


def test_inventario_marca_en_nulo_lo_que_no_existe(app_exports, settings_tope, tmp_path, auth):
    """Un proveedor con crudos pero sin respaldos no puede inventar un rango."""
    settings_tope()
    _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", "2026-08-01", [{"x": 1}])

    fila = app_exports.get("/api/export/inventario", auth=auth).json()["filas"][0]
    assert fila["crudos"] is not None
    assert fila["enviados"] is None


def test_inventario_vacio_no_es_un_error(app_exports, settings_tope, auth):
    settings_tope()
    r = app_exports.get("/api/export/inventario", auth=auth)
    assert r.status_code == 200
    assert r.json()["filas"] == []


def test_inventario_informa_la_retencion_vigente(app_exports, settings_tope, tmp_path, auth):
    """Sin la retención al lado, el rango no dice cuánto tiempo queda."""
    settings_tope(audit=14, procesados=21)
    _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", "2026-08-01", [{"x": 1}])

    datos = app_exports.get("/api/export/inventario", auth=auth).json()
    assert datos["audit_retention_days"] == 14
    assert datos["processed_retention_days"] == 21


def test_el_inventario_se_cachea_60_segundos(app_exports, settings_tope, tmp_path, auth):
    """
    El panel lo pide al abrir la vista y el recorrido puede tocar miles de
    archivos. Sin caché, cada apertura sería un escaneo completo de disco.
    """
    settings_tope()
    _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", "2026-08-01", [{"x": 1}])
    primero = app_exports.get("/api/export/inventario", auth=auth).json()

    # Un archivo nuevo NO debe aparecer mientras el caché siga vigente.
    _escribir_jsonl(tmp_path / "audit", "protrack_prod", "crudos", "2026-08-02", [{"x": 1}])
    segundo = app_exports.get("/api/export/inventario", auth=auth).json()
    assert segundo["calculado"] == primero["calculado"]
    assert len(segundo["filas"]) == 1

    exports._inventario_cache["expira"] = 0.0
    tercero = app_exports.get("/api/export/inventario", auth=auth).json()
    assert len(tercero["filas"]) == 2


def test_el_ttl_del_cache_es_de_60_segundos():
    """Fijado explícitamente: es el mismo criterio que config_cache."""
    assert exports._INVENTARIO_TTL_SEG == 60


def test_el_inventario_no_abre_los_archivos(app_exports, settings_tope, tmp_path, auth, monkeypatch):
    """
    Las fechas salen del NOMBRE del archivo. Si el inventario leyera contenido,
    responder movería gigabytes por consulta.
    """
    settings_tope()
    _escribir_jsonl(tmp_path / "audit", "schmitz_test", "crudos", "2026-08-01", [{"x": 1}])

    original = open
    def _open_vigilado(archivo, *a, **k):
        assert not str(archivo).endswith(".jsonl"), f"El inventario abrió {archivo}"
        return original(archivo, *a, **k)

    monkeypatch.setattr("builtins.open", _open_vigilado)
    assert app_exports.get("/api/export/inventario", auth=auth).status_code == 200
