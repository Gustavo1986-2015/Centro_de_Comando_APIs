# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v198.py (reorganización por tema: operacion).
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
