"""
v1.9.11 · Bloque 2b — Coordenadas 0,0 informadas por el proveedor.

Opción por integración en provider_config.coordenadas_cero:
  · "enviar" (o sin valor): como hasta v1.9.10, sale con aviso en consola.
  · "filtrar": latitud Y longitud exactamente 0 → no sale, Descartes con la
    categoría coordenadas_cero (tipo "esperado").
Una sola coordenada en 0 es un punto real y pasa siempre.

Se ejecuta cada camino de ingreso: Schmitz (/Json/Data → _persist_batch),
Protrack (pull_engine.process_and_enqueue), Studio (webhook dinámico) y
Tive (su módulo por el webhook), y el envío hasta el sobre SOAP.
"""
import asyncio
import copy
import logging

import pytest

from app.core import contrato, descartes
from tests.proveedores.protrack.test_protrack_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
from tests.proveedores.schmitz.test_schmitz_v192 import SCHMITZ_REAL
from tests.rc import rc_falso


@pytest.fixture(autouse=True)
def cache_limpia():
    contrato.invalidar_coordenadas_cero()
    yield
    contrato.invalidar_coordenadas_cero()


def _configurar(proveedor, env, ceros, **extra):
    from app import database
    from app.models.config_models import ProviderConfig
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(provider_name=proveedor, env=env, is_active=True, use_mock=False,
                          coordenadas_cero=ceros, enable_state_dedup=False, **extra))
    db.commit()
    db.close()
    contrato.invalidar_coordenadas_cero()


def _cola(proveedor, env):
    from app import database
    from app.models.db_models import NormalizedRCEvent
    database.check_and_migrate_provider_db(proveedor, env)
    db = database.get_session(proveedor, env)
    try:
        return [(f.chassis_number, f.latitude, f.longitude) for f in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()


def _filtrados(proveedor):
    descartes.esperar_escritura(2.0)
    return [d for d in descartes.consultar(1000)["ultimos"]
            if d["categoria"] == "coordenadas_cero" and d["proveedor"] == proveedor]


def _schmitz(lat, lon, patente="CERO01"):
    p = copy.deepcopy(SCHMITZ_REAL)
    p["ChassisNumber"] = p["Plate"] = patente
    p["StatusData"][0]["Position"]["Latitude"] = lat
    p["StatusData"][0]["Position"]["Longitude"] = lon
    return p


# ─── Contrato ────────────────────────────────────────────────────────────────

def test_la_categoria_es_esperada():
    assert descartes.CATEGORIAS["coordenadas_cero"].tipo == descartes.ESPERADO


def test_sin_valor_guardado_es_enviar(config_aislada):
    _configurar("schmitz", "prod", None)
    assert contrato.opcion_coordenadas_cero("schmitz", "prod") == contrato.CEROS_ENVIAR
    assert contrato.opcion_coordenadas_cero("nadie", "prod") == contrato.CEROS_ENVIAR


def test_el_nombre_se_compara_exacto_no_como_patron(config_aislada):
    _configurar("pro_track", "prod", "filtrar")
    assert contrato.opcion_coordenadas_cero("pro_track", "PROD") == "filtrar"
    assert contrato.opcion_coordenadas_cero("proxtrack", "prod") == "enviar", "'_' no es comodín"
    # El comodín estaría en lo que se BUSCA: 'pro_track' no puede encontrar a 'proxtrack'.
    _configurar("proxtrack", "test", "filtrar")
    assert contrato.opcion_coordenadas_cero("proxtrack", "test") == "filtrar"
    assert contrato.opcion_coordenadas_cero("pro_track", "test") == "enviar", "'_' no es comodín"


# ─── Schmitz ─────────────────────────────────────────────────────────────────

def test_schmitz_enviar_sale_igual_que_en_v1910_hasta_el_sobre(config_aislada, monkeypatch, caplog):
    from app.api.routers import schmitz
    _configurar("schmitz", "prod", "enviar", rc_user="u", rc_password="p")
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        schmitz._persist_batch([(_schmitz(0, 0), "prod", "z1")])
    cola = _cola("schmitz", "prod")
    assert cola and set(cola) == {("CERO01", 0.0, 0.0)}
    assert "Coordenadas 0,0 — probable GPS sin señal" in caplog.text
    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "schmitz", "prod")
    nodos = falso.nodos()
    assert len(nodos) == len(cola)
    assert {(n["latitude"], n["longitude"]) for n in nodos} == {("0.0", "0.0")}


def test_schmitz_filtrar_va_a_descartes(config_aislada, caplog):
    from app.api.routers import schmitz
    _configurar("schmitz", "prod", "filtrar")
    with caplog.at_level(logging.WARNING, logger="app.core.contrato"):
        schmitz._persist_batch([(_schmitz(0, 0), "prod", "z2"), (_schmitz(0, -58.4, "UNA01"), "prod", "z3"),
                                (_schmitz(-34.6, 0, "UNA02"), "prod", "z4")])
    assert {f[0] for f in _cola("schmitz", "prod")} == {"UNA01", "UNA02"}, "una sola en 0 pasa"
    filtrados = [d for d in _filtrados("schmitz") if d["equipo"] == "CERO01"]
    assert filtrados and not [d for d in _filtrados("schmitz") if d["equipo"] != "CERO01"]
    d = filtrados[0]
    assert d["motivo"] == contrato.MOTIVO_CEROS_FILTRADAS and d["latitud"] == 0 and d["longitud"] == 0
    assert "NO se envía a RC: coordenadas 0,0" in caplog.text


# ─── Protrack (PULL) ─────────────────────────────────────────────────────────

def test_protrack_pull_enviar_y_filtrar(config_aislada):
    from app.worker import pull_engine
    cero = dict(PROTRACK_ITEM, latitude=0, longitude=0)
    _configurar("protrack", "test", "enviar")
    asyncio.run(pull_engine.process_and_enqueue("protrack", "test", [copy.deepcopy(cero)],
                                                PROTRACK_MAPEO, enable_state_dedup=False))
    assert {(f[1], f[2]) for f in _cola("protrack", "test")} == {(0.0, 0.0)}

    _configurar("protrack", "prod", "filtrar")
    otro = dict(cero, imei="864035052700001")
    una = dict(PROTRACK_ITEM, imei="864035052700002", latitude=0)
    asyncio.run(pull_engine.process_and_enqueue("protrack", "prod", [otro, una],
                                                PROTRACK_MAPEO, enable_state_dedup=False))
    assert {f[0] for f in _cola("protrack", "prod")} == {"864035052700002"}
    assert any(d["equipo"] == "864035052700001" for d in _filtrados("protrack"))


# ─── Studio (webhook dinámico) ───────────────────────────────────────────────

def test_studio_webhook_enviar_y_filtrar(config_aislada):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    secreto = encrypt("CLAVE")
    _configurar("studio", "prod", "filtrar", provider_type="push", webhook_auth_secret_enc=secreto,
                mapping_schema=PROTRACK_MAPEO)
    _configurar("studio", "test", "enviar", provider_type="push", webhook_auth_secret_enc=secreto,
                mapping_schema=PROTRACK_MAPEO)
    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    c = TestClient(app)
    cero = dict(PROTRACK_ITEM, latitude=0, longitude=0)
    r = c.post("/webhook/dynamic/studio?env=prod", json=cero, headers={"x-api-key": "CLAVE"})
    assert r.status_code == 200 and r.json()["events_count"] == 0
    assert _cola("studio", "prod") == []
    assert _filtrados("studio")
    r = c.post("/webhook/dynamic/studio?env=test", json=cero, headers={"x-api-key": "CLAVE"})
    assert r.json()["events_count"] == 3, "con enviar sale igual que hoy"


# ─── Tive ────────────────────────────────────────────────────────────────────

from tests.proveedores.tive.test_v192_tive import SHOCK_A, _por_origen, _post, app_tive  # noqa: E402,F401


def _tive_ceros(valor):
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    cfg = db.query(ProviderConfig).filter_by(provider_name="tive", env="prod").one()
    cfg.coordenadas_cero = valor
    cfg.module_options = {"alertas_trackers": True}
    db.commit()
    db.close()
    from app.core import config_cache
    config_cache.invalidate()
    contrato.invalidar_coordenadas_cero()


def test_tive_filtrar_y_el_sin_posicion_sigue_saliendo(app_tive, tmp_path, monkeypatch):
    from app.providers.tive import estado
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado_tive"))
    _tive_ceros("filtrar")
    cero = _por_origen(SHOCK_A)
    cero["Location"].update(Latitude=0, Longitude=0)
    assert _post(app_tive, cero).json()["events_count"] == 0
    assert _filtrados("tive")
    from tests.proveedores.tive.test_v192_tive import CONNECTIVITY
    assert _post(app_tive, _por_origen(CONNECTIVITY)).json()["events_count"] == 1
    assert _cola("tive", "prod") == [("K1153407", None, None)]
    estado.cerrar_todo()


def test_el_panel_guarda_la_opcion_y_vale_desde_el_proximo_evento(app_tive):
    actual = next(c for c in app_tive.get("/api/config").json() if c["provider_name"] == "TIVE")
    assert actual["coordenadas_cero"] == "enviar"
    contrato.opcion_coordenadas_cero("tive", "prod")  # queda en caché
    cuerpo = {"id": actual["id"], "is_active": True, "rc_user": "u", "use_mock": True,
              "purge_interval_min": 60, "run_interval_sec": 5, "queue_backend": "sqlite",
              "coordenadas_cero": "filtrar"}
    assert app_tive.post("/api/config", json=[cuerpo]).status_code == 200
    assert contrato.opcion_coordenadas_cero("tive", "prod") == "filtrar", "la caché no se invalidó"
    assert next(c for c in app_tive.get("/api/config").json()
                if c["provider_name"] == "TIVE")["coordenadas_cero"] == "filtrar"
    r = app_tive.post("/api/config", json=[dict(cuerpo, coordenadas_cero="borrar")])
    assert r.status_code == 400 and "coordenadas_cero" in r.json()["detail"]
