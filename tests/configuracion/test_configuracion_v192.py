# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v192.py (reorganización por tema: configuracion).
"""
v1.9.2 — Mejoras transversales: validación única del contrato, sin UNKNOWN,
datos de vehículo, humedad con decimales, velocidad N/A, panel.

Cada cambio de código compartido se prueba con las configuraciones y payloads
REALES de Protrack y Schmitz:
  · PROTRACK_MAPEO es el esquema de protrack/test de la base local (el de
    protrack/prod está vacío). recuperar_protrack.yaml no está en el repo: sus
    cuatro reglas son las mismas que ya prueba test_v191.py.
  · PROTRACK_ITEM es un registro real (audit/protrack_prod/2026-08/, 07/08).
  · SCHMITZ_REAL es un payload real (audit/schmitz_test/2026-08/, 10/08).
"""
import copy
import logging
import math
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import admision, contrato
from app.core.dynamic_mapper import DynamicMapper
from app.services.rc_soap import construir_evento_rc


@pytest.fixture(autouse=True)
def caches_limpios():
    from app.providers.schmitz import mapper
    admision.reset()
    mapper._STATE_CACHE.clear()
    yield
    admision.reset()
    mapper._STATE_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Persistencia de los interruptores: migración y respaldo YAML
# ═══════════════════════════════════════════════════════════════════════════

def test_la_migracion_agrega_module_options_a_una_base_vieja(tmp_path, monkeypatch):
    import sqlite3
    from app import database

    monkeypatch.chdir(tmp_path)
    os.makedirs("db")
    con = sqlite3.connect("db/system_config_global.db")
    con.execute("CREATE TABLE provider_config (id INTEGER PRIMARY KEY, provider_name TEXT, env TEXT)")
    con.commit()
    con.close()
    database.check_and_migrate_db()
    con = sqlite3.connect("db/system_config_global.db")
    columnas = {r[1] for r in con.execute("PRAGMA table_info(provider_config)")}
    con.close()
    assert "module_options" in columnas


def test_el_respaldo_lleva_los_interruptores_y_valida_al_importar():
    from fastapi import HTTPException
    from app.api.routers import config_backup

    assert config_backup.CAMPOS_SIMPLES["opciones_modulo"] == "module_options"
    config_backup._validar_proveedor({"nombre": "tive", "entorno": "prod",
                                      "opciones_modulo": {"alertas_trackers": True}}, 0)
    with pytest.raises(HTTPException):
        config_backup._validar_proveedor({"nombre": "tive", "entorno": "prod",
                                          "opciones_modulo": {"alertas_trackers": "si"}}, 0)
    with pytest.raises(HTTPException):
        config_backup._validar_proveedor({"nombre": "tive", "entorno": "prod",
                                          "opciones_modulo": "todo"}, 0)
