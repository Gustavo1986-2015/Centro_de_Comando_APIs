"""
Regresión de los tres arreglos del panel que salieron de la operación real.

Lo que protege cada uno:

  · El contador ENVIADOS (HOY) decía 1.939 mientras el historial del mismo día
    marcaba 24.915. No estaba congelado: contaba filas que todavía existían en
    la base, y la retención borra los despachados a las pocas horas. La tarjeta
    decía "HOY" y mostraba "lo que no se purgó todavía".

  · La columna PURGABLE prometía 1.945 y el botón borraba mucho menos, porque
    la purga solo toca lo que superó la retención. Un número que promete de más
    es peor que no tenerlo.

  · Una base de 406 MB con cero eventos no tenía forma de compactarse desde el
    panel: sin filas purgables el botón quedaba gris.
"""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def base_limpia(tmp_path, monkeypatch):
    """Base temporal aislada, con los cachés de módulo restaurados al salir."""
    from app import database

    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    database.check_and_migrate_provider_db("system_config", "global")
    yield
    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)


# ─── El contador del día ─────────────────────────────────────────────────────

def test_los_enviados_del_dia_salen_del_acumulado_no_de_la_tabla(base_limpia):
    """
    El caso real: 24.915 despachados, la mayoría ya purgados. La tarjeta tiene
    que decir 24.915, no cuántas filas sobrevivieron.
    """
    from app.api.routers.dashboard import _totales_del_dia_sync
    from app.database import get_session
    from app.models.config_models import DailyStat

    hoy = datetime.now(timezone.utc).date()
    db = get_session("system_config", "global")
    db.add(DailyStat(date=hoy, provider="protrack", env="prod",
                     sent_count=24915, failed_count=0))
    db.add(DailyStat(date=hoy, provider="schmitz", env="prod",
                     sent_count=332, failed_count=3))
    db.commit()
    db.close()

    totales = _totales_del_dia_sync()
    assert totales["sent"] == 25247, "No sumó todas las integraciones del día"
    assert totales["failed"] == 3


def test_no_mezcla_los_dias(base_limpia):
    """Un contador del día no puede arrastrar lo de ayer."""
    from app.api.routers.dashboard import _totales_del_dia_sync
    from app.database import get_session
    from app.models.config_models import DailyStat

    hoy = datetime.now(timezone.utc).date()
    db = get_session("system_config", "global")
    db.add(DailyStat(date=hoy, provider="protrack", env="prod", sent_count=100))
    db.add(DailyStat(date=hoy - timedelta(days=1), provider="protrack", env="prod",
                     sent_count=45011))
    db.commit()
    db.close()

    assert _totales_del_dia_sync()["sent"] == 100


def test_un_dia_sin_actividad_devuelve_cero_no_error(base_limpia):
    from app.api.routers.dashboard import _totales_del_dia_sync
    assert _totales_del_dia_sync() == {"sent": 0, "failed": 0}


def test_coincide_con_lo_que_muestra_el_historial(base_limpia):
    """
    La propiedad que se buscaba: las dos vistas leen la misma fuente, así que
    coinciden por construcción y no por casualidad.
    """
    from app.api.routers.dashboard import _totales_del_dia_sync
    from app.database import get_session
    from app.models.config_models import DailyStat

    hoy = datetime.now(timezone.utc).date()
    db = get_session("system_config", "global")
    for prov, n in (("protrack", 24915), ("schmitz", 332)):
        db.add(DailyStat(date=hoy, provider=prov, env="prod", sent_count=n))
    db.commit()

    del_historial = sum(
        s.sent_count for s in db.query(DailyStat).filter(DailyStat.date == hoy).all()
    )
    db.close()

    assert _totales_del_dia_sync()["sent"] == del_historial


# ─── Espacio recuperable ─────────────────────────────────────────────────────

def test_detecta_el_espacio_libre_de_un_archivo_inflado(tmp_path):
    """
    Reproduce el caso de SCHMITZ/TEST: 406 MB con cero eventos. SQLite no
    encoge al borrar, marca las páginas como reutilizables. Sin este dato, el
    panel muestra un tamaño que parece un error de conteo.
    """
    import sqlite3

    from app.api.routers.dashboard import _espacio_recuperable_mb

    ruta = str(tmp_path / "inflada.db")
    conn = sqlite3.connect(ruta)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, dato TEXT)")
    conn.executemany("INSERT INTO t (dato) VALUES (?)",
                     [("x" * 2000,) for _ in range(4000)])
    conn.commit()
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()

    recuperable = _espacio_recuperable_mb(ruta)
    assert recuperable > 1, f"No detectó el espacio libre: {recuperable} MB"


def test_una_base_compacta_no_reporta_espacio_recuperable(tmp_path):
    """Reportar de más haría aparecer el botón donde no hace falta."""
    import sqlite3

    from app.api.routers.dashboard import _espacio_recuperable_mb

    ruta = str(tmp_path / "compacta.db")
    conn = sqlite3.connect(ruta)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert _espacio_recuperable_mb(ruta) == 0.0


def test_un_archivo_inexistente_no_rompe_el_panel(tmp_path):
    from app.api.routers.dashboard import _espacio_recuperable_mb
    assert _espacio_recuperable_mb(str(tmp_path / "no_existe.db")) == 0.0


# ─── Retención: el mínimo bajó de 7 días a 1 ─────────────────────────────────

@pytest.fixture
def cliente_config(base_limpia):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routers import admin_config

    app = FastAPI()
    app.include_router(admin_config.router)
    return TestClient(app)


@pytest.fixture
def auth():
    import os
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


@pytest.mark.parametrize("dias", [1, 2, 3, 7])
def test_se_puede_retener_un_solo_dia(cliente_config, auth, dias):
    """
    Con la certificación a 40 msg/s, un día de crudos son varios GB. Obligar a
    guardar una semana llenaba el disco sin que nadie pudiera evitarlo desde
    el panel.
    """
    r = cliente_config.put(
        "/api/config/retention",
        json={"audit_retention_days": dias, "processed_retention_days": dias},
        auth=auth,
    )
    assert r.status_code == 200, r.text


def test_cero_dias_se_sigue_rechazando(cliente_config, auth):
    """Un día es el piso: nunca se borra lo del día en curso."""
    r = cliente_config.put(
        "/api/config/retention",
        json={"audit_retention_days": 0, "processed_retention_days": 7},
        auth=auth,
    )
    assert r.status_code == 400
    assert "1 y 90" in r.json()["detail"]


def test_los_topes_superiores_siguen_vigentes(cliente_config, auth):
    r = cliente_config.put(
        "/api/config/retention",
        json={"audit_retention_days": 200, "processed_retention_days": 7},
        auth=auth,
    )
    assert r.status_code == 400
