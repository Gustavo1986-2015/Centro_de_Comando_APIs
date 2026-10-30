"""
v1.9.11, Bloque 1: el historial de envíos diarios mostraba el mismo día,
integración y entorno dos veces (caso real: SCHMITZ PROD 5439 y SCHMITZ PROD 0).

Causa (encontrada por el auditor y reproducida acá): al terminar un lote se
lanzan en paralelo increment_daily_stats (enviados/fallidos) y
update_daily_stats (latencias); los dos hacían "busco la fila de hoy, si no
está la creo" sin restricción única. Con el primer lote del día cada uno creaba
la suya y desde ahí todos tomaban .first(): la otra quedaba en 0.
"""
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

# Esquema de daily_stats tal como lo tiene hoy la base de producción (medido en
# la copia de db/system_config_global.db del 29/10/2026): sin índice único.
ESQUEMA_V1910 = """
CREATE TABLE daily_stats (
    id INTEGER NOT NULL, date DATE, provider VARCHAR, env VARCHAR,
    sent_count INTEGER, failed_count INTEGER,
    avg_transmission_latency_sec FLOAT, avg_hub_latency_sec FLOAT,
    avg_rc_latency_sec FLOAT, avg_push_latency_ms FLOAT, recovered_count INTEGER DEFAULT 0,
    PRIMARY KEY (id));
CREATE INDEX ix_daily_stats_env ON daily_stats (env);
CREATE INDEX ix_daily_stats_provider ON daily_stats (provider);
CREATE INDEX ix_daily_stats_date ON daily_stats (date);
CREATE INDEX ix_daily_stats_id ON daily_stats (id);
"""


def _hoy() -> str:
    return datetime.now().date().isoformat()


def _filas(ruta="db/system_config_global.db"):
    con = sqlite3.connect(ruta)
    try:
        return con.execute(
            "SELECT date, provider, env, sent_count, failed_count, recovered_count, "
            "avg_transmission_latency_sec, avg_hub_latency_sec, avg_rc_latency_sec, avg_push_latency_ms "
            "FROM daily_stats ORDER BY date, provider, env, id").fetchall()
    finally:
        con.close()


def _indices(ruta="db/system_config_global.db"):
    con = sqlite3.connect(ruta)
    try:
        return {n: u for _, n, u, *_ in con.execute("PRAGMA index_list(daily_stats)")}
    finally:
        con.close()


class _SesionConBarrera:
    """
    Sesión que, al leer la fila de hoy con .first(), espera a que el otro
    escritor también la haya leído. Fuerza el orden exacto de la carrera:
    los dos miran, los dos ven que no hay fila. Con el código de v1.9.11 no se
    usa (nadie hace query/first), y la barrera vence sola.
    """

    def __init__(self, sesion, barrera):
        self._s, self._b = sesion, barrera

    def query(self, *a, **k):
        sesion, barrera = self._s, self._b
        estado = {"q": sesion.query(*a, **k)}

        class _Consulta:
            def filter(self, *fa, **fk):
                estado["q"] = estado["q"].filter(*fa, **fk)
                return self

            def first(self):
                fila = estado["q"].first()
                try:
                    barrera.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
                return fila

        return _Consulta()

    def __getattr__(self, nombre):
        return getattr(self._s, nombre)


def _en_paralelo(*funciones):
    hilos = [threading.Thread(target=f) for f in funciones]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(30)


# ─── 1. La causa y la carrera ────────────────────────────────────────────────

def test_carrera_del_primer_lote_del_dia_no_duplica(config_aislada, monkeypatch):
    """
    Los dos escritores que lanza el primer lote del día, forzados a mirar a la
    vez. En v1.9.10 este mismo test deja DOS filas (5439 y 0, el caso real);
    medido corriéndolo sobre el tag v1.9.10 (ver ENTREGA).
    """
    from app import database
    from app.worker import processor

    database.get_engine("system_config", "global")
    barrera = threading.Barrier(2)
    real = database.get_session
    monkeypatch.setattr(processor, "get_session",
                        lambda p, e="prod": _SesionConBarrera(real(p, e), barrera))
    processor.registrar_latencias("schmitz", "prod", [{"rc": 0.4, "hub": 0.1, "transmission": 2.0}])

    _en_paralelo(lambda: processor.increment_daily_stats("schmitz", "prod", 5439, 0),
                 lambda: processor.update_daily_stats("schmitz", "prod"))

    filas = _filas()
    assert len(filas) == 1, f"La carrera creó {len(filas)} filas para el mismo día: {filas}"
    assert filas[0][:6] == (_hoy(), "schmitz", "prod", 5439, 0, 0)
    assert filas[0][8] == 0.4


def test_escrituras_concurrentes_reales_suman_exacto(config_aislada):
    """Sin forzar nada: muchos hilos a la vez sobre un día sin fila."""
    from app import database
    from app.worker import processor

    database.get_engine("system_config", "global")
    processor.registrar_latencias("protrack", "prod", [{"rc": 1.0, "hub": 0.2, "transmission": 3.0}])
    largada = threading.Barrier(36)

    def _tras_la_largada(f):
        def correr():
            largada.wait(10)
            f()
        return correr

    trabajos = ([_tras_la_largada(lambda: processor.increment_daily_stats("protrack", "prod", 10, 1))] * 20
                + [_tras_la_largada(lambda: processor.update_daily_stats("protrack", "prod"))] * 10
                + [_tras_la_largada(lambda: processor.sumar_recuperados("protrack", "prod", 3))] * 6)
    _en_paralelo(*trabajos)

    filas = _filas()
    assert len(filas) == 1, filas
    assert filas[0][3:6] == (200, 20, 18)
    assert filas[0][8] == 1.0


def test_los_tres_escritores_van_a_la_misma_fila(config_aislada):
    from app import database
    from app.worker import processor

    database.get_engine("system_config", "global")
    processor.sumar_recuperados("tive", "prod", 2)          # el primero del día puede ser cualquiera
    processor.update_daily_stats("tive", "prod")
    processor.increment_daily_stats("tive", "prod", 7, 1)
    assert _filas() == [(_hoy(), "tive", "prod", 7, 1, 2, None, None, None, None)]


def test_latencia_vacia_no_pisa_la_guardada(config_aislada):
    """Como antes: una media None deja la que había."""
    from app import database
    from app.core import estadisticas_diarias as ed

    database.get_engine("system_config", "global")
    ed.fijar_latencias("schmitz", "prod", avg_rc_latency_sec=0.5, avg_hub_latency_sec=0.1)
    ed.fijar_latencias("schmitz", "prod", avg_rc_latency_sec=None, avg_hub_latency_sec=0.3)
    fila = _filas()[0]
    assert (fila[7], fila[8]) == (0.3, 0.5)


# ─── 3. Normalización ────────────────────────────────────────────────────────

def test_proveedor_y_entorno_se_normalizan(config_aislada):
    from app import database
    from app.worker import processor

    database.get_engine("system_config", "global")
    processor.increment_daily_stats("Schmitz ", "PROD", 1, 0)
    processor.increment_daily_stats("schmitz", "prod", 2, 0)
    processor.increment_daily_stats(" SCHMITZ", " Prod ", 3, 0)
    assert [f[:4] for f in _filas()] == [(_hoy(), "schmitz", "prod", 6)]


def test_normalizar():
    from app.core.estadisticas_diarias import normalizar
    assert normalizar(" Schmitz ") == "schmitz"
    assert normalizar("PRO D") == "prod"
    assert normalizar(None) == ""


# ─── 4. Migración de una base de v1.9.10 ─────────────────────────────────────

def _base_v1910(tmp_path, filas):
    (tmp_path / "db").mkdir(exist_ok=True)
    ruta = tmp_path / "db" / "system_config_global.db"
    con = sqlite3.connect(ruta)
    con.executescript(ESQUEMA_V1910)
    con.executemany(
        "INSERT INTO daily_stats (id, date, provider, env, sent_count, failed_count, "
        "avg_transmission_latency_sec, avg_hub_latency_sec, avg_rc_latency_sec, avg_push_latency_ms, "
        "recovered_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)", filas)
    con.commit()
    con.close()
    return ruta


# El caso del historial (5439 y 0), los dos duplicados medidos en la base local
# (tive prod 2499/0 del 29/10 y protrack test 379/0 del 01/10, con sus
# latencias reales) y una fila que difiere solo en mayúsculas.
FILAS_V1910 = [
    (1, "2026-10-20", "schmitz", "prod", 5439, 3, 4.2, 0.12, 0.35, None, 0),
    (2, "2026-10-20", "schmitz", "prod", 0, 0, 9.9, None, 0.80, 15.5, 1),
    (14, "2026-10-01", "protrack", "test", 379, 0, 517640.839, 0.796, 0.0, None, 0),
    (15, "2026-10-01", "protrack", "test", 0, 0, 520318.483, 0.916, 0.001, None, 0),
    (31, "2026-10-29", "tive", "prod", 2499, 0, 280.875, 1.77, 3.171, 16.864, 0),
    (32, "2026-10-29", "tive", "prod", 0, 0, 214.307, 0.105, 8.491, 68.797, 0),
    (40, "2026-10-21", "Schmitz", "PROD", 10, 1, None, None, None, None, 0),
    (41, "2026-10-21", "schmitz", "prod", 20, 0, 1.0, None, None, None, 2),
    (50, "2026-10-21", "protrack", "prod", 7, 0, 2.0, 0.2, 0.3, None, 0),
]


def _totales(filas):
    return (sum(f[4] for f in filas), sum(f[5] for f in filas), sum(f[10] for f in filas))


def test_migracion_fusiona_los_duplicados_de_una_base_v1910(config_aislada, caplog):
    from app import database

    ruta = _base_v1910(config_aislada, FILAS_V1910)
    with caplog.at_level(logging.INFO):
        database.get_engine("system_config", "global")

    filas = _filas()
    assert filas == [
        ("2026-10-01", "protrack", "test", 379, 0, 0, 517640.839, 0.796, 0.0, None),
        ("2026-10-20", "schmitz", "prod", 5439, 3, 1, 4.2, 0.12, 0.35, 15.5),
        ("2026-10-21", "protrack", "prod", 7, 0, 0, 2.0, 0.2, 0.3, None),
        ("2026-10-21", "schmitz", "prod", 30, 1, 2, 1.0, None, None, None),
        ("2026-10-29", "tive", "prod", 2499, 0, 0, 280.875, 1.77, 3.171, 16.864),
    ]
    con = sqlite3.connect(ruta)
    totales = con.execute("SELECT SUM(sent_count), SUM(failed_count), SUM(recovered_count) FROM daily_stats").fetchone()
    con.close()
    assert totales == _totales(FILAS_V1910), "La fusión cambió los totales"
    assert _indices().get("ux_daily_stats_dia") == 1
    assert "4 fila(s) duplicada(s) fusionada(s)" in caplog.text


def test_latencias_la_no_nula_y_si_hay_dos_la_de_mas_enviados():
    from app.core.estadisticas_diarias import fusionar_filas
    fusion = fusionar_filas([
        {"id": 1, "date": "d", "provider": "p", "env": "e", "sent_count": 0, "avg_rc_latency_sec": 9.0,
         "avg_hub_latency_sec": 7.0},
        {"id": 2, "date": "d", "provider": "p", "env": "e", "sent_count": 5439, "avg_rc_latency_sec": 0.3,
         "avg_hub_latency_sec": None},
    ])
    assert fusion["id"] == 1 and fusion["sent_count"] == 5439
    assert fusion["avg_rc_latency_sec"] == 0.3      # dos no nulas: la de más enviados
    assert fusion["avg_hub_latency_sec"] == 7.0     # una sola no nula: esa


def test_migracion_idempotente(config_aislada, caplog):
    from app import database
    from app.core.estadisticas_diarias import fusionar_duplicados

    ruta = _base_v1910(config_aislada, FILAS_V1910)
    database.get_engine("system_config", "global")
    antes = _filas()
    con = sqlite3.connect(ruta)
    with caplog.at_level(logging.INFO):
        caplog.clear()
        assert fusionar_duplicados(con) == 0
        assert fusionar_duplicados(con) == 0
    con.close()
    assert _filas() == antes
    assert "fusionada" not in caplog.text


def test_base_nueva_nace_con_el_indice_unico(config_aislada):
    from app import database
    database.get_engine("system_config", "global")
    assert _indices().get("ux_daily_stats_dia") == 1


def test_un_duplicado_ya_no_se_puede_insertar(config_aislada):
    from app import database
    database.get_engine("system_config", "global")
    con = sqlite3.connect("db/system_config_global.db")
    con.execute("INSERT INTO daily_stats (date, provider, env, sent_count) VALUES ('2026-10-30','schmitz','prod',1)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO daily_stats (date, provider, env, sent_count) VALUES ('2026-10-30','schmitz','prod',0)")
    con.close()


def test_sin_indice_la_escritura_lo_crea_y_sigue(config_aislada):
    """Defensa: una base que no pudo migrarse al arrancar se migra al escribir."""
    from app import database
    from app.worker import processor

    database.get_engine("system_config", "global")
    con = sqlite3.connect("db/system_config_global.db")
    con.execute("DROP INDEX ux_daily_stats_dia")
    con.execute(f"INSERT INTO daily_stats (date, provider, env, sent_count, failed_count, recovered_count) "
                f"VALUES ('{_hoy()}','schmitz','prod',5,0,0), ('{_hoy()}','schmitz','prod',0,0,0)")
    con.commit()
    con.close()
    processor.increment_daily_stats("schmitz", "prod", 1, 0)
    assert [f[:4] for f in _filas()] == [(_hoy(), "schmitz", "prod", 6)]
    assert _indices().get("ux_daily_stats_dia") == 1


# ─── 5. Lecturas agrupadas ───────────────────────────────────────────────────

def test_historial_no_muestra_un_duplicado_aunque_quede(config_aislada):
    from fastapi.testclient import TestClient
    from app import database
    from main import app

    database.get_engine("system_config", "global")
    con = sqlite3.connect("db/system_config_global.db")
    con.execute("DROP INDEX ux_daily_stats_dia")      # un duplicado que hubiera quedado
    con.executemany("INSERT INTO daily_stats (date, provider, env, sent_count, failed_count, recovered_count, "
                    "avg_rc_latency_sec) VALUES (?,?,?,?,?,?,?)", [
                        ("2026-10-20", "schmitz", "prod", 5439, 2, 0, 0.35),
                        ("2026-10-20", "Schmitz", "PROD", 0, 0, 0, 0.80),
                        ("2026-10-19", "schmitz", "prod", 100, 0, 0, None)])
    con.commit()
    con.close()

    r = TestClient(app).get("/api/history", auth=("test_admin", "test_pass_123"))
    assert r.status_code == 200
    filas = [(f["date"], f["provider"], f["env"], f["sent_count"], f["failed_count"], f["avg_rc_latency_sec"])
             for f in r.json()]
    assert filas == [("2026-10-20", "SCHMITZ", "PROD", 5439, 2, 0.35),
                     ("2026-10-19", "SCHMITZ", "PROD", 100, 0, None)]


def test_tarjeta_del_dia_suma_aunque_quede_un_duplicado(config_aislada):
    from app import database
    from app.api.routers.dashboard import _totales_del_dia_sync

    database.get_engine("system_config", "global")
    con = sqlite3.connect("db/system_config_global.db")
    con.execute("DROP INDEX ux_daily_stats_dia")
    con.executemany("INSERT INTO daily_stats (date, provider, env, sent_count, failed_count, recovered_count) "
                    "VALUES (?,?,?,?,?,?)", [(_hoy(), "schmitz", "prod", 5439, 1, 0),
                                             (_hoy(), "schmitz", "prod", 0, 0, 2)])
    con.commit()
    con.close()
    assert _totales_del_dia_sync() == {"sent": 5439, "failed": 1, "recovered": 2}


# ─── 6. Volver a v1.9.10 sobre la base migrada ───────────────────────────────

def _incremento_como_v1910(provider, env, enviados, fallidos, dia):
    """El algoritmo de increment_daily_stats de v1.9.10, tal cual (ORM, .first())."""
    from app.database import get_session
    from app.models.config_models import DailyStat
    db = get_session("system_config", "global")
    try:
        stat = db.query(DailyStat).filter(DailyStat.date == dia, DailyStat.provider == provider,
                                          DailyStat.env == env).first()
        if not stat:
            db.add(DailyStat(date=dia, provider=provider, env=env, sent_count=enviados, failed_count=fallidos))
        else:
            stat.sent_count += enviados
            stat.failed_count += fallidos
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def test_volver_a_v1910_sobre_la_base_migrada_funciona(config_aislada):
    """
    v1.9.10 no conoce el índice y no lo borra. Su escritura (buscar y crear o
    sumar) sigue funcionando: suma a la fila existente y crea la del día nuevo.
    Lo único que cambia es que una carrera ya no deja una segunda fila: la
    segunda inserción falla (v1.9.10 lo registra como error y pierde ese
    incremento, en vez de esconderlo en una fila en 0).
    """
    from datetime import date
    from app import database

    _base_v1910(config_aislada, FILAS_V1910)
    database.get_engine("system_config", "global")

    _incremento_como_v1910("schmitz", "prod", 1, 0, date(2026, 10, 20))
    _incremento_como_v1910("schmitz", "prod", 4, 0, date(2026, 10, 30))
    filas = {(f[0], f[1], f[2]): f[3] for f in _filas()}
    assert filas[("2026-10-20", "schmitz", "prod")] == 5440
    assert filas[("2026-10-30", "schmitz", "prod")] == 4

    from sqlalchemy.exc import IntegrityError
    from app.database import get_session
    from app.models.config_models import DailyStat
    db = get_session("system_config", "global")
    db.add(DailyStat(date=date(2026, 10, 30), provider="schmitz", env="prod", sent_count=0, failed_count=0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_db_viewer_sigue_abriendo_una_tabla_daily_stats_ajena(tmp_path):
    """Una daily_stats con otras columnas (visor de bases) no rompe la migración."""
    from app.core.estadisticas_diarias import fusionar_duplicados
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("CREATE TABLE daily_stats (fecha TEXT, enviados INTEGER)")
    assert fusionar_duplicados(con) == 0
    con.close()
