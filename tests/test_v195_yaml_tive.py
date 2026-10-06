"""
v1.9.4 · Punto 4 — YAML de Tive para el servidor (v1.9.5: suma el
interruptor "posiciones_equipos", apagado).

tests/fixtures/tive_prod_servidor.yaml es el mismo archivo que se entrega en
docs/entrega_v1.9.5/. Se importa por los endpoints reales del respaldo
(simular y aplicar) sobre una base temporal con otras integraciones, como el
servidor.
"""
import os

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import config_backup

YAML = open(os.path.join(os.path.dirname(__file__), "fixtures", "tive_prod_servidor.yaml"),
            encoding="utf-8").read()


@pytest.fixture
def auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    from app import database
    from app.core import rate_limit
    from app.models.config_models import ProviderConfig

    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add_all([ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                               use_mock=False, mapping_schema={}),
                ProviderConfig(provider_name="protrack", env="prod", provider_type="pull", is_active=False,
                               mapping_schema={"base_mapping": {"chassis_number": "imei"}})])
    db.commit()
    db.close()
    app = FastAPI()
    app.include_router(config_backup.router)
    yield TestClient(app)
    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()


def _filas():
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    try:
        return {(c.provider_name, c.env): c for c in db.query(ProviderConfig).all()}
    finally:
        db.close()


def test_el_archivo_tiene_solo_a_tive_y_sin_secretos():
    datos = yaml.safe_load(YAML)
    assert [(p["nombre"], p["entorno"]) for p in datos["proveedores"]] == [("tive", "prod")]
    assert "configuracion_general" not in datos and "entorno" not in datos
    assert datos["incluye_credenciales"] is False
    (tive,) = datos["proveedores"]
    assert "mapeo" not in tive and "telemetria" not in tive


def test_simular_no_escribe_y_anuncia_una_creacion(cliente, auth):
    r = cliente.post("/api/config/import/simular", json={"contenido": YAML, "sobrescribir": False}, auth=auth)
    assert r.status_code == 200, r.text
    assert r.json()["resumen"].startswith("1 a crear")
    assert ("tive", "prod") not in _filas()


def test_la_importacion_real_deja_tive_como_se_pidio(cliente, auth):
    antes = {k: (c.is_active, c.use_mock, c.provider_type) for k, c in _filas().items()}
    r = cliente.post("/api/config/import", auth=auth,
                     json={"contenido": YAML, "sobrescribir": False, "confirmacion": "IMPORTAR"})
    assert r.status_code == 200, r.text
    filas = _filas()
    tive = filas[("tive", "prod")]
    assert (tive.provider_type, tive.is_active, tive.use_mock) == ("push", True, True)
    assert tive.webhook_auth_config == {"modo": "hmac", "preset": "tive"}
    assert tive.webhook_auth_header == "x-tive-signature"
    assert tive.module_options == {"posiciones_terceros": True, "alertas_beacons": True,
                                   "alertas_trackers": False, "resolver_nombres_api": False,
                                   "posiciones_equipos": False}
    assert not tive.mapping_schema, "no tiene que traer mapeo del Studio"
    assert not tive.webhook_auth_secret_enc and not tive.fetch_config_enc, "no trae credenciales"
    despues = {k: (c.is_active, c.use_mock, c.provider_type) for k, c in filas.items() if k != ("tive", "prod")}
    assert despues == antes, "tocó otra integración"


def test_los_interruptores_coinciden_con_los_valores_por_defecto_del_modulo():
    from app.providers.tive import modulo
    (tive,) = yaml.safe_load(YAML)["proveedores"]
    assert tive["opciones_modulo"] == modulo.INTERRUPTORES


def test_es_el_mismo_archivo_que_se_entrega():
    entregado = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "docs", "entrega_v1.9.5", "tive_prod_servidor.yaml")
    if not os.path.exists(entregado):
        pytest.skip("docs/ no está en este checkout (está en .gitignore)")
    assert open(entregado, encoding="utf-8").read() == YAML
