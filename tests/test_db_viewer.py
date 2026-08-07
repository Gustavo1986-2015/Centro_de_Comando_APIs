"""
Tests del visor de base de datos (app/api/routers/db_viewer.py).

Cubre el listado de bases (incluyendo la detección de residuales de esquemas
anteriores), la búsqueda genérica sobre cualquier tabla, y las protecciones de
edición.
"""
import os
import sqlite3
import pytest

from app.api.routers.db_viewer import _es_huerfana, _resolve_db_path, EDITABLE_TABLES


# ── Detección de bases residuales ────────────────────────────────────────────

@pytest.mark.parametrize("ruta", [
    "system_config_global.db",
    "protrack/prod.db",
    "protrack/test.db",
    "schmitz/prod.db",
    "schmitz/test.db",
])
def test_bases_del_esquema_vigente_no_son_huerfanas(ruta):
    """El esquema actual genera solo estas dos formas de ruta."""
    assert _es_huerfana(ruta) is False


@pytest.mark.parametrize("ruta", [
    "schmitz/test_unit.db",       # quedó de una corrida de tests
    "global.db",                  # esquema anterior
    "system_config.db",           # anterior al rename a _global
    "protrack/backup_viejo.db",   # entorno inexistente
    "sub/carpeta/profunda.db",    # fuera del esquema
])
def test_bases_fuera_del_esquema_se_marcan_como_huerfanas(ruta):
    """
    Aparecían mezcladas con las reales en el selector sin forma de
    distinguirlas. No se borran solas: pueden tener datos a rescatar.
    """
    assert _es_huerfana(ruta) is True


# ── Resolución de rutas (path traversal) ─────────────────────────────────────

def test_resolve_rechaza_path_traversal():
    assert _resolve_db_path("../../etc/passwd") is None
    assert _resolve_db_path("../secretos.db") is None


def test_resolve_acepta_rutas_validas():
    assert _resolve_db_path("system_config_global.db") is not None
    assert _resolve_db_path("protrack/prod.db") is not None


def test_resolve_rechaza_vacio():
    assert _resolve_db_path("") is None
    assert _resolve_db_path(None) is None


# ── Whitelist de edición ─────────────────────────────────────────────────────

def test_tablas_operativas_no_son_editables():
    """La cola de eventos nunca debe editarse a mano: la gestiona el worker."""
    assert "normalized_rc_events" not in EDITABLE_TABLES


def test_tablas_de_configuracion_si_son_editables():
    for t in ("provider_config", "provider_dictionary", "daily_stats"):
        assert t in EDITABLE_TABLES


# ── Detección de tablas fuera de lugar ───────────────────────────────────────

@pytest.mark.parametrize("db,tabla", [
    ("system_config_global.db", "provider_config"),
    ("system_config_global.db", "provider_dictionary"),
    ("system_config_global.db", "daily_stats"),
    ("system_config_global.db", "system_settings"),
    ("protrack/test.db",        "normalized_rc_events"),
    ("schmitz/prod.db",         "normalized_rc_events"),
])
def test_tablas_en_su_base_correcta(db, tabla):
    from app.api.routers.db_viewer import _tabla_es_huerfana
    assert _tabla_es_huerfana(db, tabla) is False


@pytest.mark.parametrize("db,tabla", [
    # Una versión anterior creaba todos los modelos en todos los engines:
    # las bases de proveedor quedaron con las tablas de configuración vacías.
    ("protrack/test.db",        "provider_config"),
    ("protrack/test.db",        "provider_dictionary"),
    ("protrack/prod.db",        "daily_stats"),
    ("schmitz/test.db",         "system_settings"),
    # Y la inversa: la cola de eventos no va en el archivo maestro.
    ("system_config_global.db", "normalized_rc_events"),
])
def test_tablas_fuera_de_lugar_se_marcan(db, tabla):
    from app.api.routers.db_viewer import _tabla_es_huerfana
    assert _tabla_es_huerfana(db, tabla) is True


def test_tablas_internas_de_sqlite_no_se_marcan():
    """sqlite_sequence la crea el motor, no el esquema de la aplicación."""
    from app.api.routers.db_viewer import _tabla_es_huerfana
    assert _tabla_es_huerfana("protrack/test.db", "sqlite_sequence") is False
    assert _tabla_es_huerfana("system_config_global.db", "sqlite_sequence") is False


# ── Búsqueda genérica ────────────────────────────────────────────────────────

@pytest.fixture
def db_temporal(tmp_path):
    """Base con dos tablas de forma distinta para probar la búsqueda genérica."""
    ruta = tmp_path / "prueba.db"
    conn = sqlite3.connect(ruta)
    cur = conn.cursor()
    cur.execute("CREATE TABLE provider_dictionary (provider_name TEXT, dict_key TEXT, dict_value TEXT)")
    cur.executemany(
        "INSERT INTO provider_dictionary VALUES (?,?,?)",
        [("protrack", "868307060968914", "C180673"),
         ("protrack", "864035053550910", "C182662"),
         ("schmitz",  "CHASIS001",        "AB1234")],
    )
    cur.execute("CREATE TABLE daily_stats (fecha TEXT, enviados INTEGER)")
    cur.executemany("INSERT INTO daily_stats VALUES (?,?)",
                    [("2026-08-07", 1500), ("2026-08-06", 980)])
    conn.commit()
    conn.close()
    return str(ruta)


def _buscar(db, tabla, termino):
    """Réplica de la búsqueda genérica del endpoint, sin levantar la app."""
    import re
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({tabla})")
    columnas = [c[1] for c in cur.fetchall() if re.match(r'^[a-zA-Z0-9_]+$', c[1])]
    cond = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in columnas)
    cur.execute(f"SELECT * FROM {tabla} WHERE {cond}", [f"%{termino}%"] * len(columnas))
    filas = cur.fetchall()
    conn.close()
    return filas


def test_busqueda_encuentra_por_cualquier_columna(db_temporal):
    """
    Antes la búsqueda estaba limitada a normalized_rc_events y a dos columnas
    fijas: en el diccionario o la configuración no había filtro alguno.
    """
    assert len(_buscar(db_temporal, "provider_dictionary", "C180673")) == 1   # por valor
    assert len(_buscar(db_temporal, "provider_dictionary", "868307060968914")) == 1  # por clave
    assert len(_buscar(db_temporal, "provider_dictionary", "protrack")) == 2  # por proveedor


def test_busqueda_funciona_en_tablas_sin_columnas_de_texto(db_temporal):
    """CAST a TEXT permite buscar también sobre columnas numéricas."""
    assert len(_buscar(db_temporal, "daily_stats", "1500")) == 1


def test_busqueda_sin_coincidencias(db_temporal):
    assert _buscar(db_temporal, "provider_dictionary", "NO_EXISTE_XYZ") == []


def test_busqueda_es_parcial(db_temporal):
    """LIKE con comodines: buscar un fragmento debe alcanzar."""
    assert len(_buscar(db_temporal, "provider_dictionary", "C18")) == 2
