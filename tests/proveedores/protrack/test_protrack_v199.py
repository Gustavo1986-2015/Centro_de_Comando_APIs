# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque2.py (reorganización por tema: proveedores/protrack).
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
TOKEN = "TOKEN-SECRETO-PROTRACK-0123456789"


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
# 2.4 — Credenciales del diccionario cifradas
# ═══════════════════════════════════════════════════════════════════════════

# Estructura del diccionario de Protrack del YAML real del servidor (09/10).
DICCIONARIO_PROTRACK = {"enabled": True, "timezone_offset": 0,
                        "url": "http://api.protrack365.com/api/device/list?access_token=ACCESS_TOKEN",
                        "method": "GET", "frequency": 23, "key_path": "record.0.imei",
                        "value_path": "record.0.platenumber", "auth_type": "protrack", "auth_user": "prueba.maersk"}
CLAVE_DICCIONARIO = "clave-del-diccionario-77"


def _base_con_diccionario_en_claro():
    """provider_config como está HOY en producción: auth_pass en texto plano."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="protrack", env="prod", provider_type="pull", is_active=True,
                          use_mock=True, enrichment_config=dict(DICCIONARIO_PROTRACK, auth_pass=CLAVE_DICCIONARIO)))
    db.commit()
    db.close()


def _enrichment_crudo():
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    try:
        return json.loads(con.execute(
            "SELECT enrichment_config FROM provider_config WHERE provider_name='protrack'").fetchone()[0])
    finally:
        con.close()


def test_24_migracion_idempotente_cifra_sin_perder(config_aislada):
    from app.core.crypto import decrypt, is_encrypted
    from app.database import check_and_migrate_db
    _base_con_diccionario_en_claro()
    assert _enrichment_crudo()["auth_pass"] == CLAVE_DICCIONARIO
    check_and_migrate_db()
    primera = _enrichment_crudo()
    assert is_encrypted(primera["auth_pass"]) and decrypt(primera["auth_pass"]) == CLAVE_DICCIONARIO
    check_and_migrate_db()
    assert _enrichment_crudo() == primera, "la segunda corrida volvió a cifrar"
    assert {k: v for k, v in primera.items() if k != "auth_pass"} == DICCIONARIO_PROTRACK


async def test_cableado_24_despues_de_migrar_el_diccionario_se_sigue_sincronizando(config_aislada, monkeypatch):
    """
    PRUEBA OBLIGATORIA: config real de Protrack, migración, y un ciclo REAL del
    sincronizador contra un Protrack simulado que EXIGE esa contraseña (firma
    md5(md5(clave)+tiempo), como la API real).
    """
    from app.database import check_and_migrate_db, get_session
    from app.models.config_models import ProviderDictionary
    from app.worker import pull_engine
    _base_con_diccionario_en_claro()
    check_and_migrate_db()
    pedidos = []

    def protrack_simulado(request: httpx.Request):
        pedidos.append(request.url.path)
        q = dict(request.url.params)
        if request.url.path == "/api/authorization":
            esperada = hashlib.md5((hashlib.md5(CLAVE_DICCIONARIO.encode()).hexdigest() + q["time"]).encode()).hexdigest()
            if q.get("account") != "prueba.maersk" or q.get("signature") != esperada:
                return httpx.Response(200, json={"code": 10003, "message": "signature error"})
            return httpx.Response(200, json={"code": 0, "record": {"access_token": TOKEN, "expires_in": 7200}})
        if request.url.path == "/api/device/list" and q.get("access_token") == TOKEN:
            return httpx.Response(200, json={"code": 0, "record": [{"imei": "864035052734572", "platenumber": "PT734572"}]})
        return httpx.Response(200, json={"code": 10011, "message": "access_token error"})

    real = httpx.AsyncClient
    monkeypatch.setattr(pull_engine.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(protrack_simulado), **k))
    monkeypatch.setattr(pull_engine, "_verificar_destino_permitido", lambda url: None)
    pull_engine._TOKEN_CACHE.clear()

    async def cortar(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(pull_engine, "sleep_or_resync", cortar)
    with pytest.raises(asyncio.CancelledError):
        await pull_engine.dictionary_sync_loop("protrack", "prod")
    db = get_session("system_config", "global")
    filas = [(d.dict_key, d.dict_value) for d in db.query(ProviderDictionary).all()]
    db.close()
    assert filas == [("864035052734572", "PT734572")], f"no sincronizó; pedidos={pedidos}"
    assert pedidos[0] == "/api/authorization"
    pull_engine._TOKEN_CACHE.clear()


def test_24_el_panel_no_recibe_la_contrasena_y_guardar_vacio_la_conserva(config_aislada):
    from app.api.routers import admin_config
    from app.core.crypto import decrypt, is_encrypted
    from app.database import check_and_migrate_db
    _base_con_diccionario_en_claro()
    check_and_migrate_db()
    c = _cliente(admin_config.router)
    leido = c.get("/api/config/protrack/prod/enrichment", auth=_auth()).json()
    assert "auth_pass" not in leido and leido["tiene_auth_pass"] is True
    assert CLAVE_DICCIONARIO not in json.dumps(leido)
    # Guardar desde el panel sin tocar la contraseña (campo vacío): se conserva.
    guardado = c.post("/api/config/protrack/prod/enrichment", json=dict(DICCIONARIO_PROTRACK, frequency=12, auth_pass=""),
                      auth=_auth()).json()
    assert guardado["status"] == "success"
    crudo = _enrichment_crudo()
    assert crudo["frequency"] == 12 and decrypt(crudo["auth_pass"]) == CLAVE_DICCIONARIO
    # Una nueva se guarda cifrada.
    c.post("/api/config/protrack/prod/enrichment", json=dict(DICCIONARIO_PROTRACK, auth_pass="otra-clave-99"), auth=_auth())
    crudo = _enrichment_crudo()
    assert is_encrypted(crudo["auth_pass"]) and decrypt(crudo["auth_pass"]) == "otra-clave-99"


def test_24_el_respaldo_yaml_la_trata_como_las_demas(config_aislada):
    """Export: no sale, y figura en "credenciales a cargar". Import: se conserva."""
    import yaml
    from app.api.routers import config_backup
    from app.core.crypto import decrypt
    from app.database import check_and_migrate_db
    _base_con_diccionario_en_claro()
    check_and_migrate_db()
    c = _cliente(config_backup.router)
    texto = c.get("/api/config/export", auth=_auth()).text
    assert CLAVE_DICCIONARIO not in texto and "gAAAAA" not in texto
    protrack = next(p for p in yaml.safe_load(texto)["proveedores"] if p["nombre"] == "protrack")
    assert "auth_pass" not in protrack["diccionario"]
    assert any("sincronizador de diccionario" in x for x in protrack["credenciales_a_cargar"])
    r = c.post("/api/config/import", json={"contenido": texto, "sobrescribir": True, "confirmacion": "IMPORTAR"},
               auth=_auth())
    assert r.status_code == 200, r.text
    assert decrypt(_enrichment_crudo()["auth_pass"]) == CLAVE_DICCIONARIO
