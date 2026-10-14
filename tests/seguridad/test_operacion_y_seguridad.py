"""
Tests de cuatro cambios de operación y seguridad.

Dos vienen de necesidades concretas que aparecieron operando el sistema:

  · Ver la API key guardada. Estaban cifradas y el panel solo mostraba puntos,
    así que la única forma de verificar una era probarla contra el endpoint.
    Eso costó tiempo diagnosticando 5.320 rechazos que resultaron ser un
    desajuste entre lo cargado y lo que enviaba el simulador.

  · Renovar el token de un proveedor PULL. Cuando el proveedor lo invalida por
    su lado, el hub sigue usando el viejo hasta que expire, fallando en cada
    ciclo mientras tanto.

Los otros dos son hallazgos de la auditoría externa (B2, B3).
"""
import base64

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def cliente():
    # Las variables se setean con MonkeyPatch y se restauran al terminar el
    # módulo. Antes se escribían con os.environ directo y quedaban pisadas para
    # el RESTO de la sesión: cualquier test posterior que usara autenticación
    # recibía 401 según el orden de ejecución. Ya costó 19 fallos intermitentes.
    # El contexto se mantiene abierto durante los tests porque
    # verify_dashboard_auth lee el entorno en cada request, no al importar.
    with pytest.MonkeyPatch.context() as entorno:
        entorno.setenv("DASHBOARD_USER", "t")
        entorno.setenv("DASHBOARD_PASSWORD", "clave_de_prueba_larga")
        entorno.setenv("APP_ENV", "development")
        from main import app
        yield TestClient(app)


@pytest.fixture(scope="module")
def auth():
    return {
        "Authorization": "Basic " + base64.b64encode(b"t:clave_de_prueba_larga").decode()
    }


# ═══════════════════════════════════════════════════════════════════
# Ver la API key guardada
# ═══════════════════════════════════════════════════════════════════

def test_revelar_la_clave_exige_revalidar_la_contrasena(cliente, auth):
    """
    No alcanza con la sesión del panel: ver una credencial es una operación
    sensible aunque sea la propia.
    """
    r = cliente.post("/api/config/reveal-key", headers=auth, json={
        "provider_name": "schmitz", "env": "test", "password": "incorrecta",
    })
    assert r.status_code == 403


def test_revelar_la_clave_requiere_autenticacion(cliente):
    r = cliente.post("/api/config/reveal-key", json={
        "provider_name": "schmitz", "env": "test", "password": "clave_de_prueba_larga",
    })
    assert r.status_code == 401


def test_con_la_contrasena_correcta_devuelve_la_clave(cliente, auth, monkeypatch):
    """
    Se parchea el descifrado en lugar de leer la base real: el estado de la
    base local no debe determinar si el test pasa. Al correr la suite, la llave
    de cifrado es una de prueba, distinta de la que cifró los datos reales.
    """
    from unittest.mock import MagicMock

    config = MagicMock()
    config.webhook_auth_secret_enc = "cifrado"
    config.webhook_auth_header = "x-api-key"

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = config

    monkeypatch.setattr("app.api.routers.admin_config.get_session", lambda *a, **k: db)
    monkeypatch.setattr("app.api.routers.admin_config.decrypt", lambda _: "CLAVE_REAL")

    r = cliente.post("/api/config/reveal-key", headers=auth, json={
        "provider_name": "schmitz", "env": "test", "password": "clave_de_prueba_larga",
    })

    assert r.status_code == 200
    d = r.json()
    assert d["configurada"] is True
    assert d["api_key"] == "CLAVE_REAL"
    assert d["header"] == "x-api-key"


def test_una_clave_indescifrable_se_informa_con_claridad(cliente, auth, monkeypatch):
    """
    Si MASTER_ENC_KEY cambió, la clave está guardada pero es ilegible. El
    mensaje tiene que decirlo: es la causa más probable y la más costosa de
    diagnosticar a ciegas.
    """
    from unittest.mock import MagicMock

    config = MagicMock()
    config.webhook_auth_secret_enc = "cifrado_con_otra_llave"
    config.webhook_auth_header = "x-api-key"

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = config

    monkeypatch.setattr("app.api.routers.admin_config.get_session", lambda *a, **k: db)
    monkeypatch.setattr("app.api.routers.admin_config.decrypt", lambda _: None)

    r = cliente.post("/api/config/reveal-key", headers=auth, json={
        "provider_name": "schmitz", "env": "test", "password": "clave_de_prueba_larga",
    })

    assert r.status_code == 500
    assert "MASTER_ENC_KEY" in r.json()["detail"]


def test_un_proveedor_inexistente_se_informa_con_claridad(cliente, auth):
    r = cliente.post("/api/config/reveal-key", headers=auth, json={
        "provider_name": "no_existe", "env": "test", "password": "clave_de_prueba_larga",
    })
    assert r.status_code == 404


def test_la_consulta_de_una_clave_queda_registrada(config_aislada, caplog):
    """
    Ver una credencial deja rastro de quién lo hizo. v1.9.9 (B-20): se EJECUTA
    el endpoint (antes se buscaba "logger.warning" en el fuente).
    """
    import logging
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import admin_config
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push",
                          webhook_auth_secret_enc=encrypt("CLAVE-VISTA")))
    db.commit()
    db.close()
    app = FastAPI()
    app.include_router(admin_config.router)
    usuario = os.environ["DASHBOARD_USER"]
    with caplog.at_level(logging.WARNING, logger="app.api.routers.admin_config"):
        r = TestClient(app).post("/api/config/reveal-key", json={"provider_name": "schmitz", "env": "prod",
                                                                 "password": os.environ["DASHBOARD_PASSWORD"]},
                                 auth=(usuario, os.environ["DASHBOARD_PASSWORD"]))
    assert r.status_code == 200 and r.json()["api_key"] == "CLAVE-VISTA"
    assert f"revelada en el panel por '{usuario}'" in caplog.text


# ═══════════════════════════════════════════════════════════════════
# Renovar el token de un proveedor PULL
# ═══════════════════════════════════════════════════════════════════

def test_renovar_token_requiere_autenticacion(cliente):
    assert cliente.post("/api/config/refresh-token/protrack/prod").status_code == 401


def test_renovar_token_valida_los_nombres(cliente, auth):
    """Los nombres se usan para buscar configuración: no pueden ser arbitrarios."""
    r = cliente.post("/api/config/refresh-token/..%2F..%2Fetc/prod", headers=auth)
    assert r.status_code in (400, 404)


def test_renovar_token_responde_aunque_no_hubiera_ninguno(cliente, auth):
    """Pedir la renovación de un token inexistente no es un error."""
    r = cliente.post("/api/config/refresh-token/protrack/prod", headers=auth)

    assert r.status_code == 200
    assert "message" in r.json()


def test_se_descarta_solo_el_token_del_proveedor_pedido():
    """
    REGRESIÓN: la función buscaba una columna inexistente (auth_config_enc en
    lugar de fetch_config_enc), caía siempre en el camino de emergencia y
    vaciaba el caché completo. Con varios proveedores PULL, renovar el token de
    uno descartaría los de todos.
    """
    from unittest.mock import MagicMock, patch
    import app.worker.pull_engine as pe

    pe._TOKEN_CACHE.clear()
    pe._TOKEN_CACHE["http://api.protrack365.com|prueba.maersk"] = {"token": "A", "expires_at": 9e12}
    pe._TOKEN_CACHE["http://api.otro.com|otro"] = {"token": "B", "expires_at": 9e12}

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = MagicMock()

    with patch("app.database.get_session", return_value=db), \
         patch.object(pe, "_load_fetch_config", return_value={
             "url": "http://api.protrack365.com/api/track",
             "auth_user": "prueba.maersk",
         }):
        assert pe.invalidar_token_cacheado("protrack", "test") is True

    assert "http://api.protrack365.com|prueba.maersk" not in pe._TOKEN_CACHE
    assert "http://api.otro.com|otro" in pe._TOKEN_CACHE, (
        "Se descartó el token de otro proveedor"
    )
    pe._TOKEN_CACHE.clear()


def test_la_clave_del_cache_se_arma_igual_que_al_pedir_el_token(config_aislada, monkeypatch):
    """
    Si la clave no coincidiera con la de _get_protrack_token, la invalidación no
    encontraría nada. v1.9.9 (B-20): se EJECUTAN las dos: se pide un token real
    (Protrack simulado) y la invalidación de esa integración lo encuentra.
    """
    import asyncio
    import json
    import app.worker.pull_engine as pe
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    _b20_protrack_simulado(monkeypatch, clave="clave-protrack")
    asyncio.run(pe._get_protrack_token("http://api.protrack365.com", "cuenta", "clave-protrack"))
    assert pe._TOKEN_CACHE, "no se cacheó el token"
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="protrack", env="prod", provider_type="pull", fetch_config_enc=encrypt(
        json.dumps({"url": "http://api.protrack365.com/api/track", "auth_type": "protrack", "auth_user": "cuenta"}))))
    db.commit()
    db.close()
    assert pe.invalidar_token_cacheado("protrack", "prod") is True
    assert pe._TOKEN_CACHE == {}


def test_sin_configuracion_de_extraccion_no_descarta_nada():
    """Un proveedor sin URL o usuario no tiene token asociado."""
    from unittest.mock import MagicMock, patch
    import app.worker.pull_engine as pe

    pe._TOKEN_CACHE.clear()
    pe._TOKEN_CACHE["http://x|y"] = {"token": "Z", "expires_at": 9e12}

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = MagicMock()

    with patch("app.database.get_session", return_value=db), \
         patch.object(pe, "_load_fetch_config", return_value={}):
        assert pe.invalidar_token_cacheado("protrack", "test") is False

    assert "http://x|y" in pe._TOKEN_CACHE
    pe._TOKEN_CACHE.clear()


def test_la_renovacion_no_pide_el_token_en_el_momento(config_aislada, monkeypatch):
    """
    Solo descarta el guardado: pedirlo es tarea del ciclo del worker. v1.9.9
    (B-20): se EJECUTA la invalidación con la red prohibida.
    """
    import json
    import time
    import httpx
    import app.worker.pull_engine as pe
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="protrack", env="prod", provider_type="pull", fetch_config_enc=encrypt(
        json.dumps({"url": "http://api.protrack365.com/api/track", "auth_type": "protrack", "auth_user": "cuenta"}))))
    db.commit()
    db.close()
    pe._TOKEN_CACHE["http://api.protrack365.com|cuenta"] = {"token": "T", "expires_at": time.time() + 600}

    def prohibido(*a, **k):
        raise AssertionError("la invalidación salió a la red")

    monkeypatch.setattr(httpx.AsyncClient, "get", prohibido)
    monkeypatch.setattr(httpx.AsyncClient, "post", prohibido)
    monkeypatch.setattr(httpx, "get", prohibido)
    assert pe.invalidar_token_cacheado("protrack", "prod") is True
    assert "http://api.protrack365.com|cuenta" not in pe._TOKEN_CACHE


# ═══════════════════════════════════════════════════════════════════
# B2 — El motor PULL no debe alcanzar direcciones internas
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/admin",
    "http://localhost/interno",
    "http://192.168.1.1/router",
    "http://10.0.0.5/servicio",
    "http://169.254.169.254/latest/meta-data/",
])
def test_se_rechazan_los_destinos_internos(url, monkeypatch):
    """
    El Inspector ya validaba esto; el motor PULL no. Una URL mal cargada
    convertiría al hub en un puente hacia la red del servidor.

    La bandera se fija en el test: si dependiera del .env de quien corre la
    suite, el resultado cambiaría de máquina en máquina.
    """
    import app.worker.pull_engine as pe

    monkeypatch.setattr(pe, "PERMITIR_DESTINOS_INTERNOS", False)
    with pytest.raises(ValueError, match="interna"):
        pe._verificar_destino_permitido(url)


def test_un_destino_publico_se_permite(monkeypatch):
    import app.worker.pull_engine as pe

    monkeypatch.setattr(pe, "PERMITIR_DESTINOS_INTERNOS", False)
    pe._verificar_destino_permitido("http://api.protrack365.com/api/track")


def test_un_nombre_que_no_resuelve_se_bloquea(monkeypatch):
    """
    v1.9.9 (B-9): el guardián falla CERRADO. Antes este test se llamaba
    test_un_nombre_que_no_resuelve_no_bloquea y aseguraba lo contrario: un
    host que no resolvía pasaba. Ahora no se puede verificar que sea externo,
    así que se bloquea (también con los destinos internos permitidos).
    """
    import app.worker.pull_engine as pe

    for permitir in (False, True):
        monkeypatch.setattr(pe, "PERMITIR_DESTINOS_INTERNOS", permitir)
        with pytest.raises(ValueError, match="no resuelve"):
            pe._verificar_destino_permitido("http://dominio-que-no-existe-12345.invalid/api")


# ═══════════════════════════════════════════════════════════════════
# B3 — El nombre del archivo de caché no debe escapar de db/
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("usuario", [
    "../../etc/passwd",
    "usuario/con/barras",
    "..\\\\windows\\\\system32",
    "a/../../../tmp/x",
])
def test_el_nombre_de_cache_no_escapa_del_directorio(usuario):
    from app.services.rc_soap import _nombre_cache_seguro

    nombre = _nombre_cache_seguro(usuario)
    assert "/" not in nombre
    assert "\\" not in nombre
    assert ".." not in nombre


def test_usuarios_distintos_no_colisionan():
    """
    Sanear puede reducir dos usuarios al mismo texto; el hash los desambigua
    para que no compartan token.
    """
    from app.services.rc_soap import _nombre_cache_seguro

    assert _nombre_cache_seguro("a/b") != _nombre_cache_seguro("a_b")


def test_el_mismo_usuario_da_siempre_el_mismo_nombre():
    """Si cambiara entre llamadas, el token cacheado no se encontraría nunca."""
    from app.services.rc_soap import _nombre_cache_seguro

    assert _nombre_cache_seguro("AC_avl_Protrack") == _nombre_cache_seguro("AC_avl_Protrack")


def test_los_destinos_internos_se_pueden_habilitar(monkeypatch):
    """
    Probar contra un proveedor simulado en la misma máquina es un caso legítimo.
    Bloquearlo sin salida haría el sistema imposible de probar en local.

    Se parchea la bandera en lugar de recargar el módulo: un reload deja
    referencias viejas en los otros tests que ya lo importaron.
    """
    import app.worker.pull_engine as pe

    monkeypatch.setattr(pe, "PERMITIR_DESTINOS_INTERNOS", True)
    pe._verificar_destino_permitido("http://127.0.0.1:8000/Json/Data")
    pe._verificar_destino_permitido("http://192.168.1.50/api")


def test_por_defecto_los_destinos_internos_se_bloquean(monkeypatch):
    """La protección tiene que estar activa salvo que se pida lo contrario."""
    import app.worker.pull_engine as pe

    monkeypatch.setattr(pe, "PERMITIR_DESTINOS_INTERNOS", False)
    with pytest.raises(ValueError):
        pe._verificar_destino_permitido("http://127.0.0.1:8000/x")


def test_el_mensaje_indica_como_habilitarlo(monkeypatch):
    """
    Un bloqueo sin explicación obliga a leer el código para entender qué hacer.
    """
    import app.worker.pull_engine as pe

    monkeypatch.setattr(pe, "PERMITIR_DESTINOS_INTERNOS", False)
    with pytest.raises(ValueError, match="PULL_ALLOW_INTERNAL_URLS"):
        pe._verificar_destino_permitido("http://127.0.0.1:9999/x")


def test_la_bandera_se_lee_del_entorno(tmp_path):
    """
    El valor por defecto es el seguro. v1.9.9 (B-20): se importa el motor en
    un proceso sin la variable y con ella (antes se buscaba el texto).
    """
    codigo = """
        from app.worker import pull_engine
        print(pull_engine.PERMITIR_DESTINOS_INTERNOS)
    """
    assert _b20_en_proceso(tmp_path, codigo) == "False"
    assert _b20_en_proceso(tmp_path, codigo, PULL_ALLOW_INTERNAL_URLS="True") == "True"


# ─── v1.9.9 (B-20): ayuda para correr código del hub en un proceso aparte ──────
def _b20_en_proceso(tmp_path, codigo, **entorno):
    """
    Corre `codigo` con el hub importable, en una carpeta temporal y con el
    entorno dado (por ejemplo TZ, para que la hora local y la UTC difieran).
    Devuelve la última línea de la salida.
    """
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path
    from cryptography.fernet import Fernet
    raiz = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in ("RC_USE_MOCK", "PULL_ALLOW_INTERNAL_URLS", "TZ")}
    env.update(APP_ENV="development", MASTER_ENC_KEY=Fernet.generate_key().decode(),
               LOG_FILE_PATH=str(tmp_path / "proceso.jsonl"), PYTHONIOENCODING="utf-8")
    env.update(entorno)
    guion = tmp_path / "guion_b20.py"
    guion.write_text("import sys\nsys.path.insert(0, %r)\n" % str(raiz) + textwrap.dedent(codigo), encoding="utf-8")
    p = subprocess.run([sys.executable, str(guion)], cwd=str(tmp_path), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-3000:]
    return p.stdout.strip().splitlines()[-1]


def _b20_tz_con_otra_fecha():
    """Un TZ (formato del CRT) en el que la fecha local de HOY no es la UTC."""
    from datetime import datetime, timezone
    return "ABC-13" if datetime.now(timezone.utc).hour >= 11 else "ABC+13"


# ─── v1.9.9 (B-20): Protrack simulado para los tests que ejecutan el PULL ──────
def _b20_protrack_simulado(monkeypatch, clave="clave-de-prueba", pedidos=None):
    """
    execute_fetch / _get_protrack_token contra un Protrack simulado (httpx
    MockTransport) que exige la firma md5(md5(clave)+tiempo). Los nombres se
    resuelven a una IP pública fija para que el guardián de destinos corra igual.
    """
    import hashlib
    import socket
    import httpx
    from app.worker import pull_engine
    pedidos = [] if pedidos is None else pedidos

    def protrack(request):
        q = dict(request.url.params)
        pedidos.append((request.url.path, q))
        if request.url.path == "/api/authorization":
            esperada = hashlib.md5((hashlib.md5(clave.encode()).hexdigest() + q["time"]).encode()).hexdigest()
            if q.get("signature") != esperada:
                return httpx.Response(200, json={"code": 10003, "message": "signature error"})
            return httpx.Response(200, json={"code": 0, "record": {"access_token": "TOKEN-B20", "expires_in": 7200}})
        if q.get("access_token") == "TOKEN-B20":
            return httpx.Response(200, json={"code": 0, "record": [{"imei": "864035052734572"}]})
        return httpx.Response(200, json={"code": 10011, "message": "access_token error"})

    real = httpx.AsyncClient
    monkeypatch.setattr(pull_engine.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(protrack), **k))
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    pull_engine._TOKEN_CACHE.clear()
    return pedidos
