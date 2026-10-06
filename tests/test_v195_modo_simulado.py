"""
v1.9.5 · Punto 1 — El modo simulado de schmitz/prod cambió solo.

Medido (ENTREGA_v1.9.5.md): no fue el panel. La suite corrida en la carpeta
del hub en vivo alternó 17 veces el modo de la primera integración de la base
real (tests/test_mock_mode.py, dos corridas: 9 + 8) y la dejó en REAL, sin una
línea en la consola.

Acá se cubre:
  - La suite corre aislada: nada se escribe en el directorio de lanzamiento.
  - Idempotencia del guardado del panel: el JavaScript REAL carga la tabla y
    guarda sin tocar nada; ninguna columna de ninguna fila cambia. Varias
    integraciones PUSH y PULL, con y sin módulo dedicado.
  - Todo cambio de modo simulado deja en la consola integración, valor
    anterior, valor nuevo y usuario; un guardado que falla no deja aviso.
"""
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import admin_config, config_backup
from app.core.crypto import encrypt

RAIZ = Path(__file__).resolve().parent.parent
LOGGER = "app.core.modo_simulado"

HMAC_PERSONALIZADO = {"modo": "hmac", "header": "x-firma", "patron": "sha256=(.+)",
                      "contenido": "{body}", "codificacion": "hex"}


@pytest.fixture
def auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


@pytest.fixture
def cliente(config_aislada):
    """Base propia con integraciones variadas, cargadas intercaladas para que
    el agrupado por proveedor del panel no coincida con el orden de la base."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    filas = [
        ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                       use_mock=False, rc_user="AC_avl_Schmitz", rc_password_enc=encrypt("rc-schmitz"),
                       webhook_auth_secret_enc=encrypt("Zx81clave"), webhook_auth_header="x-api-key",
                       rate_limit_per_min=600, enable_state_dedup=False, run_interval_sec=5,
                       purge_interval_min=15),
        ProviderConfig(provider_name="protrack", env="prod", provider_type="pull", is_active=False,
                       use_mock=False, rc_user="AC_avl_Protrack", rc_password_enc=encrypt("rc-protrack"),
                       fetch_config_enc=encrypt(json.dumps({"url": "https://api.protrack/x", "auth_user": "u"})),
                       enable_state_dedup=True, run_interval_sec=55, purge_interval_min=15,
                       mapping_schema={"base_mapping": {"chassis_number": "imei"}}),
        ProviderConfig(provider_name="tive", env="prod", provider_type="push", is_active=True, use_mock=True,
                       rc_user="", webhook_auth_header="x-tive-signature",
                       webhook_auth_config={"modo": "hmac", "preset": "tive"},
                       webhook_auth_secret_enc=encrypt("secreto-hmac"),
                       module_options={"posiciones_terceros": True, "alertas_beacons": True,
                                       "alertas_trackers": True, "resolver_nombres_api": True},
                       fetch_config_enc=encrypt(json.dumps({"auth_user": "Envios Assistcargo",
                                                            "auth_pass": "s3cr3t"})),
                       enable_state_dedup=False, run_interval_sec=30, purge_interval_min=15),
        ProviderConfig(provider_name="schmitz", env="test", provider_type="push", is_active=False,
                       use_mock=True, rc_user=None, webhook_auth_header=None, enable_state_dedup=False),
        ProviderConfig(provider_name="acme", env="prod", provider_type="push", is_active=True, use_mock=False,
                       rc_user="AC_avl_Acme", webhook_auth_header="x-firma",
                       webhook_auth_config=dict(HMAC_PERSONALIZADO),
                       webhook_auth_secret_enc=encrypt("otra"), rate_limit_per_min=1200,
                       run_interval_sec=10, purge_interval_min=30, enable_state_dedup=True,
                       mapping_schema={"base_mapping": {"chassis_number": "plate"}}),
        ProviderConfig(provider_name="protrack", env="test", provider_type="pull", is_active=True,
                       use_mock=True, rc_user="AC_avl_Protrack", enable_state_dedup=True, run_interval_sec=55),
        ProviderConfig(provider_name="tive", env="test", provider_type="push", is_active=False, use_mock=True,
                       webhook_auth_config={"modo": "hmac", "preset": "tive", "tolerancia_seg": 600},
                       webhook_auth_header="x-tive-signature", module_options=None),
    ]
    db = get_session("system_config", "global")
    db.add_all(filas)
    db.commit()
    db.close()
    app = FastAPI()
    app.include_router(admin_config.router)
    app.include_router(config_backup.router)
    return TestClient(app)


def _foto():
    """Todas las columnas de todas las filas, tal como están en el archivo."""
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    con.row_factory = sqlite3.Row
    try:
        return {r["id"]: dict(r) for r in con.execute("SELECT * FROM provider_config")}
    finally:
        con.close()


def _guardar_desde_el_panel(cliente, auth, tmp_path, *cambios):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    r = cliente.get("/api/config", auth=auth)
    assert r.status_code == 200
    entrada, salida = tmp_path / "config.json", tmp_path / "post.json"
    entrada.write_text(r.text, encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "verificar_panel_v195.js"), str(entrada), str(salida),
                        *cambios], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    enviado = json.loads(salida.read_text(encoding="utf-8"))
    return enviado, cliente.post("/api/config", json=enviado["updates"], auth=auth)


def _diferencias(antes, despues):
    return {(i, col): (antes[i][col], despues[i][col])
            for i in antes for col in antes[i] if antes[i][col] != despues[i][col]}


# ─── Idempotencia del panel ────────────────────────────────────────────────

def test_guardar_sin_tocar_nada_no_cambia_ningun_campo(cliente, auth, tmp_path, caplog):
    antes = _foto()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        enviado, r = _guardar_desde_el_panel(cliente, auth, tmp_path)
    assert r.status_code == 200, r.text
    assert not enviado["pidio_contrasena"], "el panel creyó que se activaba el modo simulado"
    assert len(enviado["updates"]) == 7
    assert _diferencias(antes, _foto()) == {}
    assert "MODO SIMULADO" not in caplog.text


def test_guardar_dos_veces_seguidas_tampoco(cliente, auth, tmp_path):
    antes = _foto()
    _guardar_desde_el_panel(cliente, auth, tmp_path)
    _guardar_desde_el_panel(cliente, auth, tmp_path)
    assert _diferencias(antes, _foto()) == {}


def test_tocar_un_control_cambia_solo_ese_campo_de_esa_fila(cliente, auth, tmp_path):
    """Si los campos se corrieran de fila, el cambio aparecería en otra."""
    antes = _foto()
    config = cliente.get("/api/config", auth=auth).json()
    idx = {(c["provider_name"], c["env"]): i for i, c in enumerate(config)}
    i_tive_test, i_schmitz, i_tive = idx[("TIVE", "TEST")], idx[("SCHMITZ", "PROD")], idx[("TIVE", "PROD")]
    _, r = _guardar_desde_el_panel(cliente, auth, tmp_path,
                                   f"mock_{i_tive_test}=false",
                                   f"ratelimit_{i_schmitz}=900",
                                   f"modopt_{i_tive}_alertas_trackers=false")
    assert r.status_code == 200, r.text
    dif = _diferencias(antes, _foto())
    ids = {(c["provider_name"], c["env"]): c["id"] for c in config}
    tive = json.loads(dif.pop((ids[("TIVE", "PROD")], "module_options"))[1])
    assert tive["alertas_trackers"] is False
    assert dif == {(ids[("TIVE", "TEST")], "use_mock"): (1, 0),
                   (ids[("SCHMITZ", "PROD")], "rate_limit_per_min"): (600, 900)}


# ─── Aviso de cada cambio de modo simulado ─────────────────────────────────

def _fila(cliente, auth, proveedor, env):
    for c in cliente.get("/api/config", auth=auth).json():
        if (c["provider_name"], c["env"]) == (proveedor, env):
            return c


def _update(c, use_mock, password=None):
    u = {"id": c["id"], "is_active": c["is_active"], "use_mock": use_mock, "rc_user": c["rc_user"] or "",
         "rc_password": "", "purge_interval_min": c["purge_interval_min"],
         "run_interval_sec": c["run_interval_sec"], "queue_backend": c["queue_backend"],
         "enable_state_dedup": c["enable_state_dedup"], "rate_limit_per_min": c["rate_limit_per_min"]}
    if password is not None:
        u["admin_password"] = password
    return u


def test_activar_deja_integracion_valores_y_usuario(cliente, auth, caplog):
    c = _fila(cliente, auth, "SCHMITZ", "PROD")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        r = cliente.post("/api/config", json=[_update(c, True, auth[1])], auth=auth)
    assert r.status_code == 200, r.text
    assert (f"MODO SIMULADO ACTIVADO en schmitz/prod: REAL -> SIMULADO | usuario={auth[0]} | origen=panel"
            in caplog.text)


def test_desactivar_tambien_y_como_warning(cliente, auth, caplog):
    c = _fila(cliente, auth, "SCHMITZ", "TEST")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        r = cliente.post("/api/config", json=[_update(c, False)], auth=auth)
    assert r.status_code == 200, r.text
    (registro,) = [x for x in caplog.records if "MODO SIMULADO" in x.getMessage()]
    assert registro.levelno == logging.WARNING
    assert (f"MODO SIMULADO DESACTIVADO en schmitz/test: SIMULADO -> REAL | usuario={auth[0]} | origen=panel"
            in registro.getMessage())


def test_un_guardado_que_falla_no_cambia_nada_ni_avisa(cliente, auth, caplog):
    """Una fila se desactiva bien, la otra intenta activar sin contraseña: 403."""
    real = _fila(cliente, auth, "SCHMITZ", "PROD")
    simulada = _fila(cliente, auth, "SCHMITZ", "TEST")
    antes = _foto()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        r = cliente.post("/api/config", json=[_update(simulada, False), _update(real, True)], auth=auth)
    assert r.status_code == 403
    assert _diferencias(antes, _foto()) == {}
    assert "MODO SIMULADO" not in caplog.text


def test_guardar_el_mismo_valor_no_avisa(cliente, auth, caplog):
    c = _fila(cliente, auth, "TIVE", "PROD")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        cliente.post("/api/config", json=[_update(c, True)], auth=auth)
    assert "MODO SIMULADO" not in caplog.text


def test_la_importacion_de_yaml_tambien_avisa_con_usuario(cliente, auth, caplog):
    yaml_ = ("formato: 1\nproveedores:\n- nombre: schmitz\n  entorno: prod\n  modo_simulado: true\n"
             "- nombre: nueva\n  entorno: prod\n  tipo: push\n  modo_simulado: false\n")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        r = cliente.post("/api/config/import", auth=auth,
                         json={"contenido": yaml_, "sobrescribir": True, "confirmacion": "IMPORTAR"})
    assert r.status_code == 200, r.text
    assert (f"MODO SIMULADO ACTIVADO en schmitz/prod: REAL -> SIMULADO | usuario={auth[0]} "
            f"| origen=importación YAML" in caplog.text)
    assert (f"MODO SIMULADO DESACTIVADO en nueva/prod: no existía -> REAL | usuario={auth[0]} "
            f"| origen=importación YAML" in caplog.text)


def test_un_cambio_por_codigo_sin_usuario_avisa_igual(cliente, caplog):
    """El camino del 05/10: alguien escribe la base sin pasar por el panel."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        db.query(ProviderConfig).filter_by(provider_name="schmitz", env="prod").one().use_mock = True
        db.commit()
    db.close()
    assert ("MODO SIMULADO ACTIVADO en schmitz/prod: REAL -> SIMULADO | usuario=sin usuario identificado"
            in caplog.text)


# ─── La suite corre aislada ────────────────────────────────────────────────

def test_la_suite_no_trabaja_en_la_carpeta_del_repositorio():
    from app import database
    from app.core import descartes, safety_net
    from app.providers.tive import estado
    cwd = Path.cwd().resolve()
    assert RAIZ not in (cwd, *cwd.parents), f"la suite corre dentro del repositorio: {cwd}"
    for ruta in (database.get_db_url("system_config", "global").replace("sqlite:///", ""),
                 descartes._ruta(), estado._ruta("prod"), safety_net.DIRECTORIO_BASE):
        absoluta = Path(ruta).resolve()
        assert RAIZ not in absoluta.parents, f"{ruta} cae dentro del repositorio"
