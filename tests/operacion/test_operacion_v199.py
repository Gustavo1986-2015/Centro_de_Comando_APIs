# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque3.py (reorganización por tema: operacion).
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


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _cliente(*routers):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════════════════
# 3.2 Descarga "Enviados": solo bases de cola
# ═══════════════════════════════════════════════════════════════════════════

def test_32_enviados_no_abre_ni_ofrece_bases_de_estado(config_aislada, monkeypatch):
    from app.api.routers import exports
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    from app.providers.tive import estado
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", os.path.join(".", "db", "tive"))
    estado.aprender_par("prod", "865648069052454", "K393478", "webhook")
    estado.cerrar_todo()
    db = get_session("tive", "prod")
    db.add(NormalizedRCEvent(provider="tive", status="sent", chassis_number="K393478", code="1", raw_data="{}"))
    db.commit()
    db.close()
    os.makedirs(os.path.join("audit", "nadie_prod"))   # un par que solo existe en audit/
    ruta_estado = os.path.join("db", "tive", "prod_estado.db")

    def tablas():
        con = sqlite3.connect(ruta_estado)
        try:
            return sorted(t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        finally:
            con.close()

    antes = tablas()
    c = _cliente(exports.router)
    opciones = c.get("/api/export/opciones", auth=_auth()).json()["combinaciones"]
    assert {"provider": "tive", "env": "prod_estado"} not in opciones and {"provider": "tive", "env": "prod"} in opciones
    hoy = __import__("datetime").date.today().isoformat()
    r = c.get("/api/export/enviados", params={"provider": "todos", "env": "todos", "desde": hoy, "hasta": hoy},
              auth=_auth())
    assert r.status_code == 200 and "K393478" in r.text
    assert tablas() == antes, "la descarga le creó una tabla a la base de estado"
    assert not os.path.exists(os.path.join("db", "nadie", "prod.db")), "la descarga creó una base vacía"
