# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque2.py (reorganización por tema: proveedores/schmitz).
"""
v1.9.9 · Bloque 2 — Seguridad.

2.1 B-9   Guardián de destinos: falla cerrado y vale para todo destino saliente.
2.2 B-12  Tokens y claves enmascarados en log, salud, consola y errores.
2.3 B-16  Producción sin MASTER_ENC_KEY: arranca, pero no guarda credenciales en claro.
2.4       Credenciales del diccionario cifradas (migración, panel, YAML, ciclo real).
2.5 B-22  Purga de logs: compare_digest y el mismo mínimo que la retención.
2.6 B-29  La clave de Schmitz guardada desde el panel vale al instante.

Todos EJECUTAN el código.
"""
import asyncio
import hashlib
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
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


def _fila(fila_id, **cambios):
    base = dict(id=fila_id, is_active=True, rc_user="AC_avl_SchmitzCargoBull", use_mock=True,
                purge_interval_min=5, run_interval_sec=5, queue_backend="sqlite")
    base.update(cambios)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# 2.6 B-29 — Clave de Schmitz al instante
# ═══════════════════════════════════════════════════════════════════════════

def test_b29_la_clave_nueva_de_schmitz_vale_al_instante(config_aislada, monkeypatch):
    from app.api.routers import admin_config, schmitz
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True, use_mock=True,
                          rc_user="AC_avl_SchmitzCargoBull", webhook_auth_secret_enc=encrypt("clave-vieja")))
    db.commit()
    fila_id = db.query(ProviderConfig).first().id
    db.close()
    schmitz.invalidar_cache_auth()
    monkeypatch.setattr(schmitz, "_webhook_queue", asyncio.Queue(maxsize=10))
    c = _cliente(admin_config.router, schmitz.router_spec)
    payload = {"ChassisNumber": "R5868BDP", "DeviceTime": "2026-10-04T22:23:36Z"}
    assert c.post("/Json/Data?env=prod", json=payload, headers={"x-api-key": "clave-vieja"}).status_code == 202
    r = c.post("/api/config", json=[_fila(fila_id, webhook_auth_secret="clave-nueva")], auth=_auth())
    assert r.status_code == 200, r.text
    assert c.post("/Json/Data?env=prod", json=payload, headers={"x-api-key": "clave-nueva"}).status_code == 202, \
        "la clave nueva no valió al instante"
    assert c.post("/Json/Data?env=prod", json=payload, headers={"x-api-key": "clave-vieja"}).status_code == 401
    schmitz.invalidar_cache_auth()
