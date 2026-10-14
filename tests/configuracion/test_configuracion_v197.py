# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v197.py (reorganización por tema: configuracion).
"""
v1.9.7 — Arreglos previos al despliegue (auditoría completa del 06/10/2026).

  1. B-2: la migración de arranque borraba la configuración PULL. Con
     fetch_config guardado como el TEXTO 'null' (lo dejaban el panel y la
     importación al asignar None a la columna JSON), la migración lo cifraba
     ENCIMA de fetch_config_enc. Ahora: None se guarda como NULL de SQL, el
     texto vacío no se cifra y lo cifrado nunca se pisa.
  2. B-14: lo simulado ya no se guarda como 'sent'. Queda 'simulado' y no
     cuenta como enviado a RC (contador diario, latencia de RC), pero sí lo
     purgan la retención y el respaldo, y lo lista la descarga con su estado.

Todos los tests EJECUTAN el código (migración, worker, purga, endpoints y el
JavaScript del panel): ninguno busca texto en el fuente.
"""
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.core.crypto import decrypt, encrypt
CONFIG_PULL = {"url": "http://api.protrack365.com/api/track", "method": "GET",
               "auth_type": "protrack", "auth_user": "prueba", "auth_pass": "secreta"}


def _ruta_config():
    return os.path.join("db", "system_config_global.db")


def _crear_protrack(**columnas):
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="protrack", env="prod", provider_type="pull", is_active=True,
                          use_mock=False, mapping_schema={"base_mapping": {"chassis_number": "imei"}},
                          **columnas))
    db.commit()
    db.close()


def _crudo(columnas="fetch_config, fetch_config_enc, rc_password, rc_password_enc"):
    con = sqlite3.connect(_ruta_config())
    try:
        return con.execute(f"SELECT {columnas} FROM provider_config WHERE provider_name='protrack'").fetchone()
    finally:
        con.close()


def _escribir_crudo(**valores):
    con = sqlite3.connect(_ruta_config())
    sets = ", ".join(f"{k} = ?" for k in valores)
    con.execute(f"UPDATE provider_config SET {sets} WHERE provider_name='protrack'", list(valores.values()))
    con.commit()
    con.close()


# ═══════════════════════════════════════════════════════════════════════════
# 1. B-2 — La migración no borra la configuración PULL
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("legado", ["null", "None", "", "{}", " NULL "])
def test_la_migracion_no_pisa_la_config_pull_con_un_vacio_legado(config_aislada, legado):
    """El caso de producción: fetch_config con el texto 'null' y la credencial cifrada."""
    from app import database
    _crear_protrack(fetch_config_enc=encrypt(json.dumps(CONFIG_PULL)))
    _escribir_crudo(fetch_config=legado)
    database.check_and_migrate_db()
    plano, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL, "la migración pisó la credencial cifrada"
    assert plano is None, "el vacío legado tiene que quedar como NULL de SQL"


def test_un_plano_real_sin_cifrar_se_sigue_cifrando(config_aislada):
    """La migración de siempre (texto plano heredado) sigue funcionando."""
    from app import database
    _crear_protrack()
    _escribir_crudo(fetch_config=json.dumps(CONFIG_PULL), fetch_config_enc=None)
    database.check_and_migrate_db()
    plano, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL
    assert plano is None


def test_con_cifrado_y_plano_real_no_pisa_y_avisa(config_aislada, caplog):
    from app import database
    _crear_protrack(fetch_config_enc=encrypt(json.dumps(CONFIG_PULL)))
    _escribir_crudo(fetch_config=json.dumps({"url": "http://otro/viejo"}))
    with caplog.at_level(logging.WARNING, logger="app.database"):
        database.check_and_migrate_db()
    _, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL
    assert "No se pisa la cifrada" in caplog.text


def test_la_contrasena_de_rc_cifrada_tampoco_se_pisa(config_aislada):
    from app import database
    _crear_protrack(rc_password_enc=encrypt("la-buena"))
    _escribir_crudo(rc_password="una-vieja-en-plano")
    database.check_and_migrate_db()
    _, _, _, rc_cifrada = _crudo()
    assert decrypt(rc_cifrada) == "la-buena"


def test_el_arranque_real_sobre_una_base_v186_conserva_protrack(config_aislada):
    """
    Cableado: el camino del despliegue. Una base como la de producción v1.8.6
    (fetch_config = 'null' y la credencial cifrada) y el arranque de la versión
    nueva, que corre la migración al abrir el motor de la base.
    """
    from app import database
    _crear_protrack(fetch_config_enc=encrypt(json.dumps(CONFIG_PULL)))
    _escribir_crudo(fetch_config="null")
    database._engines.clear()       # como un proceso recién arrancado
    database._sessions.clear()
    database.get_engine("system_config", "global")
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.worker.pull_engine import _load_fetch_config
    db = get_session("system_config", "global")
    try:
        conf = db.query(ProviderConfig).filter_by(provider_name="protrack").one()
        assert _load_fetch_config(conf) == CONFIG_PULL
    finally:
        db.close()


def _cliente_panel():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import admin_config, config_backup
    app = FastAPI()
    app.include_router(admin_config.router)
    app.include_router(config_backup.router)
    return TestClient(app)


def test_cableado_guardar_desde_el_panel_y_reiniciar(config_aislada):
    """Panel → base → reinicio: la configuración PULL sobrevive."""
    from app import database
    _crear_protrack()
    c = _cliente_panel()
    auth = (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])
    (fila,) = c.get("/api/config", auth=auth).json()
    r = c.post("/api/config", auth=auth, json=[{
        "id": fila["id"], "is_active": True, "use_mock": False, "rc_user": "", "rc_password": "",
        "purge_interval_min": 15, "run_interval_sec": 55, "queue_backend": "sqlite",
        "enable_state_dedup": True, "fetch_config": json.dumps(CONFIG_PULL)}])
    assert r.status_code == 200, r.text
    con = sqlite3.connect(_ruta_config())
    assert con.execute("SELECT typeof(fetch_config) FROM provider_config").fetchone()[0] == "null", \
        "el panel guardó el texto 'null' en vez de NULL de SQL"
    con.close()
    database.check_and_migrate_db()
    _, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL


def test_cableado_la_importacion_guarda_null_de_sql(config_aislada):
    c = _cliente_panel()
    auth = (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])
    yaml_ = ("formato: 1\nproveedores:\n- nombre: protrack\n  entorno: prod\n  tipo: pull\n"
             "  telemetria:\n    url: http://api.protrack365.com/api/track\n    auth_type: protrack\n")
    r = c.post("/api/config/import", auth=auth, json={"contenido": yaml_, "sobrescribir": True,
                                                       "confirmacion": "IMPORTAR"})
    assert r.status_code == 200, r.text
    con = sqlite3.connect(_ruta_config())
    tipo, cifrado = con.execute("SELECT typeof(fetch_config), fetch_config_enc FROM provider_config "
                                "WHERE provider_name='protrack'").fetchone()
    con.close()
    assert tipo == "null"
    assert json.loads(decrypt(cifrado))["url"] == "http://api.protrack365.com/api/track"


def test_capa_modelo_asignar_none_por_el_orm_guarda_null_de_sql(config_aislada):
    """Cualquier camino que asigne None (no solo los cuatro conocidos) deja NULL."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    _crear_protrack(fetch_config={"url": "x"})
    db = get_session("system_config", "global")
    db.query(ProviderConfig).filter_by(provider_name="protrack").one().fetch_config = None
    db.commit()
    db.close()
    assert _crudo("typeof(fetch_config)")[0] == "null"


def test_capa_panel_guarda_null_aunque_el_modelo_no_lo_haga(config_aislada, monkeypatch):
    """Los cuatro lugares escriben NULL por sí mismos, sin depender del modelo."""
    from app.models.config_models import ProviderConfig
    monkeypatch.setattr(ProviderConfig.__table__.c.fetch_config.type, "none_as_null", False)
    _crear_protrack()
    c = _cliente_panel()
    auth = (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])
    (fila,) = c.get("/api/config", auth=auth).json()
    r = c.post("/api/config", auth=auth, json=[{
        "id": fila["id"], "is_active": True, "use_mock": False, "rc_user": "", "rc_password": "",
        "purge_interval_min": 15, "run_interval_sec": 55, "queue_backend": "sqlite",
        "enable_state_dedup": True, "fetch_config": json.dumps(CONFIG_PULL)}])
    assert r.status_code == 200, r.text
    assert _crudo("typeof(fetch_config)")[0] == "null"
