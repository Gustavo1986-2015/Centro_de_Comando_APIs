"""
v1.9.3 — Ajustes del panel.

  1. Columna "PUSH API Key": con clave guardada, el panel muestra sus dos
     primeros caracteres ("d0•••• cargada"). El servidor manda SOLO esos dos
     caracteres, nunca la clave. Transversal: toda integración con clave.
  2. "Última Actividad Global": con la grilla vacía el contador dice
     "0 eventos" y no se queda en "Cargando...".
"""
import json
import os
import shutil
import subprocess

import pytest

CLAVE_TIVE = "d0f3a9c1e7b24c55"
CLAVE_SCHMITZ = "Zx81QwPv7Lk2Jh9T"


@pytest.fixture
def api_config(tmp_path, monkeypatch):
    """El router real de configuración sobre una base temporal."""
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import database
    from app.core import crypto

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASTER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()

    from app.api.routers import admin_config
    from app.core.auth import verify_dashboard_auth
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig

    otra_llave = Fernet(Fernet.generate_key())
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add_all([
        ProviderConfig(provider_name="tive", env="prod", provider_type="push",
                       webhook_auth_secret_enc=encrypt(CLAVE_TIVE)),
        ProviderConfig(provider_name="schmitz", env="prod", provider_type="push",
                       webhook_auth_secret_enc=encrypt(CLAVE_SCHMITZ)),
        ProviderConfig(provider_name="schmitz", env="test", provider_type="push"),
        ProviderConfig(provider_name="corta", env="prod", provider_type="push",
                       webhook_auth_secret_enc=encrypt("ab12")),
        # Cifrada con otra llave maestra (por ejemplo, tras rotarla mal).
        ProviderConfig(provider_name="rotada", env="prod", provider_type="push",
                       webhook_auth_secret_enc=otra_llave.encrypt(b"x9-no-legible").decode()),
        ProviderConfig(provider_name="protrack", env="prod", provider_type="pull"),
    ])
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(admin_config.router)
    app.dependency_overrides[verify_dashboard_auth] = lambda: None
    yield TestClient(app)

    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)


def _por_integracion(cliente):
    r = cliente.get("/api/config")
    assert r.status_code == 200, r.text
    return {(c["provider_name"], c["env"]): c for c in r.json()}, r.text


def test_el_servidor_manda_solo_los_dos_primeros_caracteres(api_config):
    configs, _ = _por_integracion(api_config)
    assert configs[("TIVE", "PROD")]["webhook_auth_hint"] == "d0"
    assert configs[("SCHMITZ", "PROD")]["webhook_auth_hint"] == "Zx"


def test_la_clave_nunca_sale_del_servidor(api_config):
    """
    Ni completa ni pedazos de ella en ninguna parte de la respuesta. Desde 4
    caracteres: con 3, una coincidencia casual con otro texto sería posible.
    """
    _, texto = _por_integracion(api_config)
    for clave in (CLAVE_TIVE, CLAVE_SCHMITZ):
        for largo in range(4, len(clave) + 1):
            for inicio in range(0, len(clave) - largo + 1):
                assert clave[inicio:inicio + largo] not in texto, (
                    f"La respuesta contiene un pedazo de la clave: {clave[inicio:inicio + largo]!r}"
                )


def test_sin_clave_no_hay_pista(api_config):
    configs, _ = _por_integracion(api_config)
    sin = configs[("SCHMITZ", "TEST")]
    assert sin["has_webhook_auth"] is False and sin["webhook_auth_hint"] is None
    assert configs[("PROTRACK", "PROD")]["webhook_auth_hint"] is None


def test_una_clave_muy_corta_no_se_insinua(api_config):
    """Con cuatro caracteres, mostrar dos sería revelar media clave."""
    configs, _ = _por_integracion(api_config)
    corta = configs[("CORTA", "PROD")]
    assert corta["has_webhook_auth"] is True and corta["webhook_auth_hint"] is None


def test_una_clave_ilegible_no_rompe_la_configuracion(api_config):
    configs, _ = _por_integracion(api_config)
    rotada = configs[("ROTADA", "PROD")]
    assert rotada["has_webhook_auth"] is True and rotada["webhook_auth_hint"] is None


@pytest.mark.skipif(not shutil.which("node"), reason="Node no está disponible en este entorno")
def test_el_js_real_del_panel_v193():
    """
    Ejecuta loadConfig y renderRecentTable reales de dashboard.js con Node:
    prueba el cableado (que la celda use la pista y que el contador se
    actualice), no solo la lógica.
    """
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    configs = [
        {"id": 1, "provider_name": "TIVE", "env": "PROD", "provider_type": "push",
         "has_webhook_auth": True, "webhook_auth_hint": "d0", "webhook_auth_header": "x-tive-signature",
         "webhook_auth_config": {"modo": "hmac", "preset": "tive"}, "modulo_dedicado": True,
         "module_options": {"alertas_trackers": False}, "module_options_labels": {}},
        {"id": 2, "provider_name": "SCHMITZ", "env": "TEST", "provider_type": "push",
         "has_webhook_auth": False, "webhook_auth_hint": None, "webhook_auth_header": "x-api-key",
         "webhook_auth_config": {"modo": "header"}, "modulo_dedicado": False},
        {"id": 3, "provider_name": "PROTRACK", "env": "PROD", "provider_type": "pull",
         "has_webhook_auth": False, "webhook_auth_hint": None, "modulo_dedicado": False},
    ]
    r = subprocess.run(["node", os.path.join("tools", "verificar_panel_v193.js"), json.dumps(configs)],
                       cwd=raiz, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
