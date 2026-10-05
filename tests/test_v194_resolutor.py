"""
v1.9.4 · Punto 3 — Nombre del tracker por la API de Tive.

Todo contra una API de Tive SIMULADA (httpx.MockTransport): ningún test sale
a la red. La API simulada responde como lo que verificó el usuario a mano:
autenticación multipart, y la serie 867860087520523 con x-tive-account-id
10287 devuelve deviceName = J1208355.

La prueba de cableado obligatoria es
test_cableado_tramo_real_tllu5171893_de_punta_a_punta.
"""
import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
import pytest

from app.core import oauth2
from app.providers.tive import estado, modulo, resolutor

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tive_crudos_2026-10.jsonl")
CRUDOS = {json.loads(l)["origen"]: json.loads(l)["payload"] for l in open(FIXTURE, encoding="utf-8")}
TRAMO = "2026-10-02:167"      # TLLU5171893, serie 867860087520523, cuenta 10287
POSICION_TRACKER = "2026-10-01:1"

SERIE, CUENTA, NOMBRE = "867860087520523", "10287", "J1208355"
CLIENT_ID = "Envios Assistcargo"           # texto libre con espacios, como el real
SECRETO = "secreto-de-la-api-de-prueba"
CRED = {"client_id": CLIENT_ID, "client_secret": SECRETO,
        "token_url": resolutor.TOKEN_URL_POR_DEFECTO, "base_url": resolutor.BASE_URL_POR_DEFECTO,
        "formato": "multipart"}

_AsyncClientReal = httpx.AsyncClient


class ApiTive:
    """API de Tive simulada. Registra cada pedido para poder verificarlos."""

    def __init__(self):
        self.pedidos = []
        self.dispositivos = {(CUENTA, SERIE): {"deviceId": SERIE, "deviceName": NOMBRE, "accountId": 10287}}
        self.cuentas = [10287]
        self.forma = "raiz"          # 'raiz' | 'data' | 'lista'
        self.respuestas_forzadas = []  # lista de httpx.Response a devolver primero en /Devices
        self.token = "tok-1"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        ruta = request.url.path
        if ruta.endswith("/authenticate"):
            cuerpo = request.content.decode("latin-1")
            if not request.headers.get("content-type", "").startswith("multipart/form-data"):
                return httpx.Response(400, text="Tive exige multipart/form-data")
            if CLIENT_ID not in cuerpo or SECRETO not in cuerpo or "client_credentials" not in cuerpo:
                return httpx.Response(401)
            return httpx.Response(200, json={"access_token": self.token, "token_type": "Bearer",
                                             "expires_in": "3600"})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401)
        if ruta.endswith("/Accounts"):
            return httpx.Response(200, json={"totalRecords": len(self.cuentas), "pageNumber": 1,
                                             "pageSize": 50, "next": None,
                                             "data": [{"accountId": c, "disabled": False} for c in self.cuentas]})
        if "/Devices/" in ruta:
            if self.respuestas_forzadas:
                return self.respuestas_forzadas.pop(0)
            serie = ruta.rsplit("/", 1)[-1]
            disp = self.dispositivos.get((request.headers.get("x-tive-account-id"), serie))
            if disp is None:
                return httpx.Response(400, text="The requested device is not available")
            if self.forma == "data":
                return httpx.Response(200, json={"data": disp})
            if self.forma == "lista":
                return httpx.Response(200, json={"totalRecords": 1, "data": [disp]})
            return httpx.Response(200, json=disp)
        return httpx.Response(404)

    def consultas_de_dispositivo(self):
        return [p for p in self.pedidos if "/Devices/" in p.url.path]

    def autenticaciones(self):
        return [p for p in self.pedidos if p.url.path.endswith("/authenticate")]


@pytest.fixture
def api(monkeypatch, tmp_path):
    from app.worker import pull_engine
    api = ApiTive()
    transporte = httpx.MockTransport(api)
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: _AsyncClientReal(*a, **{**k, "transport": transporte}))
    # Sin DNS: el guardián de destinos resolvería api.tive.com.
    monkeypatch.setattr(pull_engine, "_verificar_destino_permitido", lambda url: None)
    oauth2._TOKEN_CACHE.clear()
    resolutor._cuentas_cache.clear()
    resolutor._pausas_por_cuenta.clear()
    resolutor._ultimo_aviso_sin_credenciales.clear()
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    yield api
    estado.cerrar_todo()
    oauth2._TOKEN_CACHE.clear()


def _resolver(cuenta=CUENTA, intentos=0):
    estado.encolar_consulta("prod", SERIE, cuenta)
    return asyncio.run(resolutor.resolver_serie(CRED, "prod", SERIE, cuenta, intentos))


# ═══════════════════════════════════════════════════════════════════════════
# Consulta a la API
# ═══════════════════════════════════════════════════════════════════════════

def test_resuelve_el_caso_real_y_guarda_el_par_con_origen_api(api):
    assert _resolver() == NOMBRE
    assert estado.nombre_de("prod", SERIE) == NOMBRE
    assert estado.origen_de("prod", SERIE) == "api"
    (pedido,) = api.consultas_de_dispositivo()
    assert pedido.url.path == f"/public/v3/Devices/{SERIE}"
    assert pedido.headers["x-tive-account-id"] == CUENTA
    assert estado.consulta("prod", SERIE)["estado"] == "resuelta"


@pytest.mark.parametrize("forma", ["raiz", "data", "lista"])
def test_deviceName_en_la_raiz_o_dentro_de_data(api, forma):
    """No se registró dónde vino deviceName en la prueba real: se soportan las dos."""
    api.forma = forma
    assert _resolver() == NOMBRE


def test_un_deviceId_distinto_no_se_usa():
    assert resolutor.extraer_nombre({"deviceId": "999", "deviceName": "OTRO"}, SERIE) is None
    assert resolutor.extraer_nombre({"data": [{"deviceId": "999", "deviceName": "X"},
                                              {"deviceId": SERIE, "deviceName": NOMBRE}]}, SERIE) == NOMBRE


def test_la_autenticacion_es_multipart_y_se_reutiliza(api):
    _resolver()
    estado.encolar_consulta("prod", "111111111111111", CUENTA)
    asyncio.run(resolutor.resolver_serie(CRED, "prod", "111111111111111", CUENTA, 0))
    assert len(api.autenticaciones()) == 1, "el token de 1 hora se pidió de nuevo"


def test_plan_b_otra_cuenta_ante_un_400(api):
    api.dispositivos = {("555", SERIE): {"deviceId": SERIE, "deviceName": NOMBRE}}
    api.cuentas = [10287, 555]
    assert _resolver() == NOMBRE
    cuentas = [p.headers.get("x-tive-account-id") for p in api.consultas_de_dispositivo()]
    assert cuentas == [CUENTA, "555"]
    assert estado.consulta("prod", SERIE)["account_id"] == "555"


def test_no_encontrada_en_ninguna_cuenta_espera_24_h(api):
    api.dispositivos = {}
    api.cuentas = [10287, 555]
    assert _resolver() is None
    c = estado.consulta("prod", SERIE)
    assert c["estado"] == "no_encontrada"
    assert c["proximo"] - time.time() > 23 * 3600


def test_un_401_invalida_el_token_y_reintenta_una_vez(api):
    api.respuestas_forzadas = [httpx.Response(401)]
    assert _resolver() == NOMBRE
    assert len(api.autenticaciones()) == 2, "no se pidió un token nuevo tras el 401"


def test_dos_401_pausan_sin_romper(api):
    api.respuestas_forzadas = [httpx.Response(401), httpx.Response(401)]
    with pytest.raises(resolutor._Pausa):
        _resolver()
    assert estado.consulta("prod", SERIE)["estado"] == "reintentar"


def test_un_429_respeta_retry_after(api):
    api.respuestas_forzadas = [httpx.Response(429, headers={"Retry-After": "1064"})]
    with pytest.raises(resolutor._Pausa):
        _resolver()
    pausa = resolutor._pausas_por_cuenta[("prod", CUENTA)]
    assert 1000 < pausa - time.time() <= 1064
    # Mientras dure la pausa, esa cuenta no se vuelve a consultar.
    antes = len(api.consultas_de_dispositivo())
    with pytest.raises(resolutor._Pausa):
        asyncio.run(resolutor.resolver_serie(CRED, "prod", SERIE, CUENTA, 1))
    assert len(api.consultas_de_dispositivo()) == antes


def test_error_de_red_reintenta_con_espera_creciente(api):
    api.respuestas_forzadas = [httpx.Response(503)]
    assert _resolver(intentos=0) is None
    c = estado.consulta("prod", SERIE)
    assert c["estado"] == "reintentar" and c["intentos"] == 1
    assert 50 < c["proximo"] - time.time() <= 60
    api.respuestas_forzadas = [httpx.Response(503)]
    asyncio.run(resolutor.resolver_serie(CRED, "prod", SERIE, CUENTA, 1))
    assert 290 < estado.consulta("prod", SERIE)["proximo"] - time.time() <= 300


def test_las_credenciales_nunca_van_a_los_logs(api, caplog):
    api.respuestas_forzadas = [httpx.Response(401), httpx.Response(401)]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(resolutor._Pausa):
            _resolver()
        api.respuestas_forzadas = []
        _resolver()
    assert SECRETO not in caplog.text
    assert CLIENT_ID not in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# Retención del tramo en el módulo
# ═══════════════════════════════════════════════════════════════════════════

def test_con_el_interruptor_apagado_el_tramo_se_descarta_como_antes(api):
    assert modulo.procesar(copy.deepcopy(CRUDOS[TRAMO]), "prod", {}, "iid-1") == []
    assert estado.tramos_pendientes("prod") == []


def test_encendido_retiene_el_tramo_y_encola_solo_trackers(api):
    assert modulo.procesar(copy.deepcopy(CRUDOS[TRAMO]), "prod", {"resolver_nombres_api": True}, "iid-1") == []
    (tramo,) = estado.tramos_pendientes("prod")
    assert tramo["ingest_id"] == "iid-1" and tramo["account_id"] == CUENTA
    assert tramo["series"] == [SERIE], "el beacon A2A2A20173D4 no es un tracker: no se consulta"
    assert estado.consulta("prod", "A2A2A20173D4") is None


def test_retener_dos_veces_no_reinicia_la_antiguedad(api):
    payload = CRUDOS[TRAMO]
    modulo.procesar(copy.deepcopy(payload), "prod", {"resolver_nombres_api": True}, "iid-1")
    primera = estado.tramos_pendientes("prod")[0]["recibido"]
    time.sleep(0.05)
    modulo.procesar(copy.deepcopy(payload), "prod", {"resolver_nombres_api": True}, "iid-1")
    assert estado.tramos_pendientes("prod")[0]["recibido"] == primera


def test_un_par_de_webhook_confirma_al_de_api(api):
    estado.aprender_par("prod", SERIE, NOMBRE, origen="api")
    estado.aprender_par("prod", SERIE, NOMBRE)
    assert estado.origen_de("prod", SERIE) == "webhook"
    estado.aprender_par("prod", SERIE, NOMBRE, origen="api")
    assert estado.origen_de("prod", SERIE) == "webhook", "la API no pisa lo que confirmó el tracker"


# ═══════════════════════════════════════════════════════════════════════════
# Cableado de punta a punta
# ═══════════════════════════════════════════════════════════════════════════

SECRETO_HMAC = "secreto-hmac-de-prueba"


def _firmar(cuerpo: bytes) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    digest = hmac.new(SECRETO_HMAC.encode(), f"{ts}.".encode() + cuerpo, hashlib.sha256).digest()
    return {"content-type": "application/json",
            "x-tive-signature": f"t={ts},v1={base64.b64encode(digest).decode()}"}


@pytest.fixture
def hub(api, tmp_path, monkeypatch):
    """Routers reales del webhook y del panel sobre bases temporales."""
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import database
    from app.core import crypto, rate_limit, safety_net

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASTER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()
    monkeypatch.setattr(safety_net, "DIRECTORIO_BASE", str(tmp_path / "red"))
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()

    from app.api.routers import admin_config, dynamic_webhook
    from app.core.auth import verify_dashboard_auth
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent

    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="tive", env="prod", provider_type="push", is_active=True,
                          use_mock=True, webhook_auth_secret_enc=encrypt(SECRETO_HMAC),
                          webhook_auth_header="x-tive-signature",
                          webhook_auth_config={"modo": "hmac", "preset": "tive"},
                          module_options={"resolver_nombres_api": True}, enable_state_dedup=False))
    db.commit()
    db.close()
    NormalizedRCEvent.metadata.create_all(bind=database.get_engine("tive", "prod"))
    database.check_and_migrate_provider_db("tive", "prod")

    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    app.include_router(admin_config.router)
    app.dependency_overrides[verify_dashboard_auth] = lambda: None
    yield TestClient(app)

    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()


def _config_tive():
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    try:
        return db.query(ProviderConfig).filter_by(provider_name="tive", env="prod").first()
    finally:
        db.close()


def _filas_tive():
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("tive", "prod")
    try:
        return [(f.chassis_number, f.ingest_id, f.code, f.shipment, f.latitude, f.longitude)
                for f in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()


def _cargar_credenciales(cliente, client_id=CLIENT_ID, secreto=SECRETO):
    tive = next(c for c in cliente.get("/api/config").json() if c["provider_name"] == "TIVE")
    r = cliente.post("/api/config", json=[{
        "id": tive["id"], "is_active": True, "rc_user": "", "use_mock": True,
        "purge_interval_min": 15, "run_interval_sec": 5, "queue_backend": "sqlite",
        "webhook_auth_config": tive["webhook_auth_config"],
        "module_credentials": {"client_id": client_id, "client_secret": secreto},
    }])
    assert r.status_code == 200, r.text
    return r


def test_cableado_tramo_real_tllu5171893_de_punta_a_punta(hub, api, caplog):
    """
    Obligatoria. Un tramo de contenedor real entra por el endpoint firmado sin
    par, queda pendiente, el resolutor (API simulada) aprende J1208355, y el
    tramo termina en la base con patente J1208355 y su ingest_id original.
    """
    caplog.set_level(logging.DEBUG)
    _cargar_credenciales(hub)

    cuerpo = json.dumps(CRUDOS[TRAMO]).encode()
    r = hub.post("/webhook/dynamic/tive?env=prod", content=cuerpo, headers=_firmar(cuerpo))
    assert r.status_code == 200 and r.json()["events_count"] == 0, r.text
    assert _filas_tive() == [], "el webhook no tiene que esperar a la API"
    assert api.pedidos == [], "el webhook consultó la API en línea"

    (pendiente,) = estado.tramos_pendientes("prod")
    ingest_original = pendiente["ingest_id"]

    resumen = asyncio.run(resolutor.ciclo(_config_tive()))
    assert resumen["resueltas"] == 1 and resumen["liberados"] == 1, resumen

    (auth,) = api.autenticaciones()
    assert auth.headers["content-type"].startswith("multipart/form-data")
    (consulta,) = api.consultas_de_dispositivo()
    assert consulta.url.path == f"/public/v3/Devices/{SERIE}"
    assert consulta.headers["x-tive-account-id"] == CUENTA

    (fila,) = _filas_tive()
    assert fila[0] == NOMBRE, "la patente no es el nombre del tracker"
    assert fila[1] == f"{ingest_original}-0", "no salió con su ingest_id original"
    assert fila[2] == "1"
    assert fila[3] == "ID 612780-CONTEN TLLU5171893- MSC- DESTINO BOLIVIA"
    assert (fila[4], fila[5]) == (3.3092916667, -78.6166416667)
    assert estado.tramos_pendientes("prod") == []
    assert estado.origen_de("prod", SERIE) == "api"

    # Otro ciclo no duplica ni vuelve a consultar.
    asyncio.run(resolutor.ciclo(_config_tive()))
    assert len(_filas_tive()) == 1
    assert len(api.consultas_de_dispositivo()) == 1

    # Las credenciales nunca se escribieron en los logs.
    assert SECRETO not in caplog.text and CLIENT_ID not in caplog.text


def test_el_tramo_siguiente_sale_directo(hub, api):
    _cargar_credenciales(hub)
    estado.aprender_par("prod", SERIE, NOMBRE, origen="api")
    cuerpo = json.dumps(CRUDOS["2026-10-02:35"]).encode()
    r = hub.post("/webhook/dynamic/tive?env=prod", content=cuerpo, headers=_firmar(cuerpo))
    assert r.json()["events_count"] == 1
    assert _filas_tive()[0][0] == NOMBRE


def test_sin_credenciales_no_se_consulta_y_se_avisa(hub, api, caplog):
    cuerpo = json.dumps(CRUDOS[TRAMO]).encode()
    hub.post("/webhook/dynamic/tive?env=prod", content=cuerpo, headers=_firmar(cuerpo))
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.resolutor"):
        asyncio.run(resolutor.ciclo(_config_tive()))
    assert api.pedidos == []
    assert "no hay credenciales" in caplog.text
    assert len(estado.tramos_pendientes("prod")) == 1, "el tramo se perdió por falta de credenciales"


def test_a_las_24_h_el_tramo_se_descarta_con_aviso(hub, api, caplog):
    from app.core import descartes
    cuerpo = json.dumps(CRUDOS[TRAMO]).encode()
    hub.post("/webhook/dynamic/tive?env=prod", content=cuerpo, headers=_firmar(cuerpo))
    con = estado._conexion("prod")
    con.execute("UPDATE tramos_pendientes SET recibido = ?", (time.time() - 25 * 3600,))
    con.commit()
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.resolutor"):
        resumen = asyncio.run(resolutor.ciclo(_config_tive()))
    assert resumen["vencidos"] == 1
    assert estado.tramos_pendientes("prod") == []
    assert "retenido 24 h" in caplog.text and "TLLU5171893" in caplog.text
    descartes.esperar_escritura(3.0)
    assert any(u["motivo"].startswith("tramo retenido 24 h") for u in descartes.consultar(50)["ultimos"])


def test_las_credenciales_se_cargan_desde_el_panel_y_no_vuelven(hub, api):
    r = _cargar_credenciales(hub)
    tive = next(c for c in hub.get("/api/config").json() if c["provider_name"] == "TIVE")
    assert tive["module_credentials"] == {"client_id": CLIENT_ID, "secreto_cargado": True}
    respuesta = hub.get("/api/config").text
    assert SECRETO not in respuesta

    # Secreto vacío = conservar; el client_id con espacios se guarda tal cual.
    _cargar_credenciales(hub, client_id="  Otro Cliente Con Espacios  ", secreto="")
    cred = resolutor.credenciales(_config_tive())
    assert cred["client_id"] == "Otro Cliente Con Espacios"
    assert cred["client_secret"] == SECRETO
    assert cred["formato"] == "multipart"


def test_las_credenciales_en_la_base_estan_cifradas(hub, api):
    _cargar_credenciales(hub)
    conf = _config_tive()
    assert conf.fetch_config_enc and SECRETO not in conf.fetch_config_enc
    assert CLIENT_ID not in conf.fetch_config_enc
    assert not conf.fetch_config


# ═══════════════════════════════════════════════════════════════════════════
# _get_oauth2_token movido a app/core/oauth2.py sin cambiar comportamiento
# ═══════════════════════════════════════════════════════════════════════════

def test_pull_engine_usa_los_mismos_objetos():
    from app.worker import pull_engine
    assert pull_engine._get_oauth2_token is oauth2.get_oauth2_token
    assert pull_engine._TOKEN_CACHE is oauth2._TOKEN_CACHE
    assert pull_engine.ProviderAuthError is oauth2.ProviderAuthError
    assert pull_engine._TOKEN_LOCK is oauth2._TOKEN_LOCK


def test_un_secreto_de_solo_espacios_no_pisa_el_guardado(hub, api):
    _cargar_credenciales(hub)
    _cargar_credenciales(hub, secreto="   ")
    assert resolutor.credenciales(_config_tive())["client_secret"] == SECRETO
