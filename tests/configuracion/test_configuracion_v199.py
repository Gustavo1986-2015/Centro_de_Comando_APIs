# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque4.py (reorganización por tema: configuracion).
"""
v1.9.9 · Bloque 4 — Código y configuración.

4.1 B-21  Siembra de integraciones: un solo lugar y un solo juego de valores;
          una base existente no cambia ninguna fila.
4.2 B-23  Valores rc_* en un solo lugar; active_queue_backend fuera sin romper
          la importación de un YAML viejo.
4.4       Tabla de cola vacía en tive/{env}_estado.db: se quita solo si está vacía.

(4.3 B-25, código muerto, y 4.5, texto de .env.example: ver la ENTREGA.)
Todos EJECUTAN el código.
"""
import logging
import os
import sqlite3

import pytest
import yaml


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _cliente(*routers):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


def _filas_config():
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM provider_config ORDER BY id")]
    finally:
        con.close()


ESPERADAS = [("schmitz", "prod", "push", 0), ("schmitz", "test", "push", 0),
             ("protrack", "prod", "pull", 1), ("protrack", "test", "pull", 1)]


# ═══════════════════════════════════════════════════════════════════════════
# 4.1 B-21 — Siembra
# ═══════════════════════════════════════════════════════════════════════════

def _resumen(filas):
    return [(f["provider_name"], f["env"], f["provider_type"], f["enable_state_dedup"]) for f in filas]


def test_41_base_nueva_desde_el_worker_crea_schmitz_push(config_aislada):
    from app.worker import processor
    processor._initialize_providers_sync()
    filas = _filas_config()
    assert _resumen(filas) == ESPERADAS
    assert {(f["is_active"], f["use_mock"]) for f in filas} == {(1, 1)}, "nacen activas y en modo simulado"


def test_41_base_nueva_desde_el_panel_crea_lo_mismo(config_aislada):
    from app.api.routers import admin_config
    r = _cliente(admin_config.router).get("/api/config", auth=_auth())
    assert r.status_code == 200
    assert _resumen(_filas_config()) == ESPERADAS, "el panel y el worker siembran distinto"


def test_41_el_tablero_no_inventa_integraciones(config_aislada):
    from app.api.routers import dashboard
    assert dashboard._fetch_providers_sync() == []
    assert _filas_config() == [], "leer el tablero no siembra"


# Estructura del YAML real del servidor (09/10), con valores de ejemplo: cinco
# integraciones, Schmitz PUSH, Protrack PULL con diccionario y Tive con HMAC.
YAML_SERVIDOR = {
    "formato": 1, "hub_version": "1.9.7", "incluye_credenciales": False,
    "proveedores": [
        {"nombre": "protrack", "entorno": "prod", "tipo": "pull", "activo": True, "modo_simulado": False,
         "deduplicacion": True, "intervalo_ejecucion_seg": 55, "intervalo_purga_min": 15, "motor_cola": "sqlite",
         "webhook_header": "x-api-key", "rc_usuario": "AC_avl_ejemplo", "limite_push_por_min": None,
         "telemetria": {"url": "http://api.ejemplo.invalid/api/track", "method": "GET", "auth_type": "protrack",
                        "auth_user": "cuenta.ejemplo"},
         "diccionario": {"enabled": True, "timezone_offset": 0,
                         "url": "http://api.ejemplo.invalid/api/device/list?access_token=ACCESS_TOKEN",
                         "method": "GET", "frequency": 23, "key_path": "record.0.imei",
                         "value_path": "record.0.platenumber", "auth_type": "protrack", "auth_user": "cuenta.ejemplo"},
         "mapeo": {"base_mapping": {"chassis_number": "imei", "latitude": "latitude", "longitude": "longitude"}}},
        {"nombre": "protrack", "entorno": "test", "tipo": "pull", "activo": False, "modo_simulado": False,
         "deduplicacion": False, "intervalo_ejecucion_seg": 55, "intervalo_purga_min": 15, "motor_cola": "sqlite",
         "webhook_header": "x-api-key", "rc_usuario": "AC_avl_ejemplo", "limite_push_por_min": None},
        {"nombre": "schmitz", "entorno": "prod", "tipo": "push", "activo": True, "modo_simulado": False,
         "deduplicacion": False, "intervalo_ejecucion_seg": 5, "intervalo_purga_min": 5, "motor_cola": "sqlite",
         "webhook_header": "x-api-key", "rc_usuario": "AC_avl_ejemplo", "limite_push_por_min": None},
        {"nombre": "schmitz", "entorno": "test", "tipo": "push", "activo": False, "modo_simulado": False,
         "deduplicacion": False, "intervalo_ejecucion_seg": 5, "intervalo_purga_min": 5, "motor_cola": "sqlite",
         "webhook_header": "x-api-key", "rc_usuario": "AC_avl_ejemplo", "limite_push_por_min": None},
        {"nombre": "tive", "entorno": "prod", "tipo": "push", "activo": True, "modo_simulado": False,
         "deduplicacion": False, "intervalo_ejecucion_seg": 30, "intervalo_purga_min": 15, "motor_cola": "sqlite",
         "webhook_header": "x-tive-signature", "webhook_autenticacion": {"modo": "hmac", "preset": "tive"},
         "rc_usuario": "ws_avl_ejemplo", "limite_push_por_min": None,
         "opciones_modulo": {"posiciones_terceros": True, "alertas_beacons": True, "alertas_trackers": True,
                             "resolver_nombres_api": True, "posiciones_equipos": False}},
    ],
    "configuracion_general": {"motor_cola_activo": "sqlite", "retencion_crudos_dias": 3,
                              "retencion_procesados_dias": 3, "respaldo_procesados_activo": True,
                              "retencion_base_horas": 8, "tope_dias_por_descarga": 3, "rc_liberacion_tanda": 500,
                              "rc_max_reintentos": 4, "rc_fallos_circuito": 5, "rc_recuperacion_umbral_seg": 600},
}


def _base_migrada_del_yaml(sin=()):
    """Una base existente como la del servidor: la del YAML, importada con el endpoint real."""
    from app.api.routers import config_backup
    datos = dict(YAML_SERVIDOR, proveedores=[p for p in YAML_SERVIDOR["proveedores"]
                                             if (p["nombre"], p["entorno"]) not in sin])
    r = _cliente(config_backup.router).post("/api/config/import", json={
        "contenido": yaml.safe_dump(datos, allow_unicode=True), "sobrescribir": True, "confirmacion": "IMPORTAR"},
        auth=_auth())
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("faltante", [(), (("protrack", "test"),), (("protrack", "prod"), ("protrack", "test"))])
def test_41_una_base_existente_no_cambia_ninguna_fila(config_aislada, faltante):
    """
    Con la base del YAML del servidor (y aunque le falte Protrack), ni el
    arranque del worker ni el panel agregan ni cambian una sola fila. Antes el
    worker agregaba protrack/prod o protrack/test si faltaban.
    """
    from app.api.routers import admin_config
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.worker import processor
    # Base existente: la que deja una instalación (Schmitz) más lo importado.
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push"))
    db.commit()
    db.close()
    _base_migrada_del_yaml(sin=faltante)
    antes = _filas_config()
    processor._initialize_providers_sync()
    _cliente(admin_config.router).get("/api/config", auth=_auth())
    assert _filas_config() == antes


# ═══════════════════════════════════════════════════════════════════════════
# 4.2 B-23 — Valores rc_* y active_queue_backend
# ═══════════════════════════════════════════════════════════════════════════

def test_42_los_valores_rc_salen_de_un_solo_lugar(config_aislada, monkeypatch):
    from app.api.routers import admin_config
    from app.database import get_session
    from app.models.config_models import RC_VALORES_POR_DEFECTO, SystemSettings
    from app.worker import processor
    monkeypatch.setitem(RC_VALORES_POR_DEFECTO, "rc_max_reintentos", 7)
    db = get_session("system_config", "global")
    db.query(SystemSettings).delete()
    db.add(SystemSettings())     # fila nueva: los defaults del modelo
    db.commit()
    db.close()
    datos = _cliente(admin_config.router).get("/api/config/rc-behavior", auth=_auth()).json()
    # Lo guardado por el modelo, lo que muestra el panel y lo que usa el worker.
    assert datos["rc_liberacion_tanda"] == 500 and datos["rc_fallos_circuito"] == 5
    # Sin valor guardado, el panel y el worker caen al MISMO valor central.
    monkeypatch.setattr(processor, "_params_cache", {"valores": None, "ts": 0})
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    con.execute("UPDATE system_settings SET rc_max_reintentos = NULL")
    con.commit()
    con.close()
    assert processor.obtener_parametros_rc()["max_reintentos"] == 7, "el worker no usa el valor central"
    assert _cliente(admin_config.router).get("/api/config/rc-behavior", auth=_auth()).json()["rc_max_reintentos"] == 7


def test_42_motor_cola_activo_ya_no_sale_y_un_yaml_viejo_se_importa_igual(config_aislada):
    from app.api.routers import config_backup
    c = _cliente(config_backup.router)
    texto = c.get("/api/config/export", auth=_auth()).text
    assert "motor_cola_activo" not in texto
    viejo = yaml.safe_dump(YAML_SERVIDOR, allow_unicode=True)
    assert "motor_cola_activo" in viejo
    plan = c.post("/api/config/import/simular", json={"contenido": viejo, "sobrescribir": True}, auth=_auth()).json()
    assert [x["campo"] for x in plan["claves_obsoletas"]] == ["motor_cola_activo"]
    r = c.post("/api/config/import", json={"contenido": viejo, "sobrescribir": True, "confirmacion": "IMPORTAR"},
               auth=_auth())
    assert r.status_code == 200, r.text
