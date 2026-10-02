"""
OAuth2 `client_credentials` en el motor PULL, transversal.

CONTEXTO

El motor PULL soportaba md5_dynamic, un bearer fijo y el flujo propio de
Protrack. Ninguno sirve para APIs que emiten tokens que vencen por
client_credentials, que es el estándar de la industria. Un bearer pegado a
mano vence y el diccionario deja de actualizarse sin que nadie se entere.

Verificado contra https://developers.tive.com/docs/authentication, Tive tiene
dos particularidades respecto del estándar OAuth2 (RFC 6749):

  · Exige multipart/form-data. El estándar es x-www-form-urlencoded, y un
    cliente genérico recibe error de Tive.
  · Devuelve expires_in como TEXTO ("3600"), no como número.

Ambas se cubren por configuración, sin código específico de Tive.
"""
import asyncio
import json

import httpx
import pytest

from app.worker import pull_engine

TOKEN_URL = "https://api.tive.com/public/v3/authenticate"

# Se guarda ANTES de cualquier parche. `pull_engine.httpx` es el mismo módulo
# que `httpx`: si un test toma `httpx.AsyncClient` después de que una fixture lo
# parcheó, recibe la versión parcheada y su propio manejador nunca corre. Ese
# error hizo que dos tests de cableado fallaran con el código correcto.
_CLIENTE_REAL = httpx.AsyncClient


@pytest.fixture(autouse=True)
def cache_limpio():
    pull_engine._TOKEN_CACHE.clear()
    yield
    pull_engine._TOKEN_CACHE.clear()


@pytest.fixture
def servidor(monkeypatch):
    """
    Servidor OAuth simulado. Registra cada petición para poder inspeccionar
    cómo se armó el cuerpo, que es justamente lo que diferencia a Tive.
    """
    registro = {"peticiones": [], "respuesta": None, "status": 200}

    def manejar(request: httpx.Request):
        registro["peticiones"].append(request)
        return httpx.Response(
            registro["status"],
            json=registro["respuesta"] or {
                "access_token": "TOKEN-1", "token_type": "Bearer", "expires_in": "3600",
            },
        )

    real = _CLIENTE_REAL

    def cliente(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(manejar)
        return real(*args, **kwargs)

    monkeypatch.setattr(pull_engine.httpx, "AsyncClient", cliente)
    # El destino real es público; el control anti-SSRF no es lo que se prueba acá.
    monkeypatch.setattr(pull_engine, "_verificar_destino_permitido", lambda url: None)
    return registro


def _pedir(formato="form", **kw):
    return asyncio.run(pull_engine._get_oauth2_token(
        kw.get("url", TOKEN_URL), kw.get("id", "cliente"), kw.get("secreto", "secreto"),
        formato, kw.get("scope", ""),
    ))


def test_obtiene_el_token(servidor):
    assert _pedir() == "TOKEN-1"


def test_el_formato_multipart_es_el_que_exige_tive(servidor):
    """
    Lo que se verifica es el cuerpo real que sale, no un flag: el
    Content-Type tiene que ser multipart/form-data y llevar los tres campos.
    """
    _pedir("multipart")
    peticion = servidor["peticiones"][0]
    assert peticion.headers["content-type"].startswith("multipart/form-data")
    cuerpo = peticion.content.decode()
    for campo in ('name="grant_type"', "client_credentials",
                  'name="client_id"', 'name="client_secret"'):
        assert campo in cuerpo, f"Falta {campo} en el cuerpo multipart"


def test_el_formato_por_defecto_es_el_estandar_oauth2(servidor):
    _pedir()
    peticion = servidor["peticiones"][0]
    assert peticion.headers["content-type"] == "application/x-www-form-urlencoded"
    assert b"grant_type=client_credentials" in peticion.content


def test_acepta_expires_in_como_texto(servidor):
    """Tive manda "3600" entre comillas. No puede caer al fallback por eso."""
    _pedir()
    entrada = next(iter(pull_engine._TOKEN_CACHE.values()))
    import time
    vigencia = entrada["expires_at"] - time.time()
    assert vigencia > 3000, f"No usó el expires_in informado: vigencia {vigencia:.0f}s"


def test_reutiliza_el_token_mientras_esta_vigente(servidor):
    """Pedir un token por cada consulta agotaría el rate limit del proveedor."""
    for _ in range(5):
        _pedir()
    assert len(servidor["peticiones"]) == 1


def test_credenciales_distintas_no_comparten_token(servidor):
    """Cada par de credenciales es una identidad distinta en el proveedor."""
    _pedir(id="cuenta-a")
    _pedir(id="cuenta-b")
    assert len(servidor["peticiones"]) == 2


def test_un_rechazo_falla_ruidoso(servidor):
    servidor["status"] = 401
    with pytest.raises(pull_engine.ProviderAuthError, match="rechazó la autenticación"):
        _pedir()


def test_el_motivo_menciona_el_formato_del_cuerpo(servidor):
    """
    Si Tive rechaza porque se mandó "form" en vez de "multipart", el mensaje
    tiene que dar la pista. Es el error más probable al configurarlo.
    """
    servidor["status"] = 400
    with pytest.raises(pull_engine.ProviderAuthError, match="formato del cuerpo"):
        _pedir()


def test_una_respuesta_sin_token_falla_ruidoso(servidor):
    servidor["respuesta"] = {"token_type": "Bearer"}
    with pytest.raises(pull_engine.ProviderAuthError, match="access_token"):
        _pedir()


@pytest.mark.parametrize("faltante", ["url", "id", "secreto"])
def test_la_configuracion_incompleta_falla_antes_de_pedir(servidor, faltante):
    with pytest.raises(pull_engine.ProviderAuthError):
        _pedir(**{faltante: ""})
    assert servidor["peticiones"] == [], "Pidió el token con la configuración incompleta"


def test_el_token_se_usa_en_la_consulta_real(servidor, monkeypatch):
    """
    Cableado, no solo lógica: execute_fetch —que usan el diccionario y la
    telemetría— tiene que pedir el token y mandarlo como Bearer, junto con los
    headers adicionales configurados.
    """
    vistas = []
    real = _CLIENTE_REAL

    def manejar(request):
        if request.url.path.endswith("/authenticate"):
            return httpx.Response(200, json={"access_token": "TOKEN-X", "expires_in": "3600"})
        vistas.append(request)
        return httpx.Response(200, json={"data": []})

    def cliente(*a, **k):
        k["transport"] = httpx.MockTransport(manejar)
        return real(*a, **k)

    monkeypatch.setattr(pull_engine.httpx, "AsyncClient", cliente)

    asyncio.run(pull_engine.execute_fetch({
        "url": "https://api.tive.com/public/v3/Devices",
        "method": "GET",
        "auth_type": "oauth2_client_credentials",
        "token_url": TOKEN_URL,
        "token_body_format": "multipart",
        "auth_user": "cliente",
        "auth_pass": "secreto",
        "headers": '{"x-tive-account-id": "2847"}',
    }))

    assert vistas, "No se hizo la consulta real"
    assert vistas[0].headers["authorization"] == "Bearer TOKEN-X"
    assert vistas[0].headers["x-tive-account-id"] == "2847"


def test_un_401_en_la_consulta_descarta_el_token(servidor, monkeypatch):
    """
    Si el token se revoca antes de vencer, el caché lo seguiría usando hasta
    una hora. Ante un 401 se descarta para que el próximo ciclo pida otro.
    """
    real = _CLIENTE_REAL

    def manejar(request):
        if request.url.path.endswith("/authenticate"):
            return httpx.Response(200, json={"access_token": "REVOCADO", "expires_in": "3600"})
        return httpx.Response(401)

    def cliente(*a, **k):
        k["transport"] = httpx.MockTransport(manejar)
        return real(*a, **k)

    monkeypatch.setattr(pull_engine.httpx, "AsyncClient", cliente)

    config = {
        "url": "https://api.tive.com/public/v3/Devices", "method": "GET",
        "auth_type": "oauth2_client_credentials", "token_url": TOKEN_URL,
        "auth_user": "cliente", "auth_pass": "secreto",
    }
    with pytest.raises(pull_engine.ProviderAuthError, match="401"):
        asyncio.run(pull_engine.execute_fetch(config))

    assert not pull_engine._TOKEN_CACHE, "El token revocado sigue en el caché"


def test_protrack_no_se_ve_afectado():
    """El flujo propio de Protrack sigue siendo el que era."""
    import inspect
    fuente = inspect.getsource(pull_engine.execute_fetch)
    assert 'auth_type == "protrack"' in fuente
    assert "_get_protrack_token" in fuente


# ═══════════════════════════════════════════════════════════════════════════
# El camino REAL del diccionario
#
# Los tests de arriba llaman a execute_fetch directo. Eso probaba la lógica de
# OAuth, pero no que el sincronizador del diccionario se la pasara: armaba su
# configuración copiando solo seis claves, y token_url y los headers se perdían
# en el camino. Con los tests de arriba en verde, el diccionario de Tive no
# habría funcionado nunca.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def base_con_diccionario_oauth(tmp_path, monkeypatch):
    from app import database

    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()

    from app.models.config_models import ProviderConfig
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="tive", env="prod", provider_type="push", is_active=True,
        use_mock=True, rc_user="u",
        enrichment_config={
            "enabled": True, "frequency": 24,
            "url": "https://api.tive.com/public/v3/Devices",
            "method": "GET",
            "auth_type": "oauth2_client_credentials",
            "token_url": TOKEN_URL,
            "token_body_format": "multipart",
            "auth_user": "cliente", "auth_pass": "secreto",
            "headers": '{"x-tive-account-id": "2847"}',
            "key_path": "data.id", "value_path": "data.name",
        },
    ))
    db.commit()
    db.close()
    yield
    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)


def test_el_diccionario_le_pasa_oauth_y_headers_a_la_consulta(base_con_diccionario_oauth, monkeypatch):
    capturado = {}

    # BaseException y no Exception: el bucle del diccionario atrapa Exception
    # para sobrevivir a errores del proveedor y seguir girando, así que una
    # Exception común no lo cortaría nunca.
    class _Cortar(BaseException):
        pass

    async def espiar(fetch_cfg):
        capturado.update(fetch_cfg)
        raise _Cortar()          # sale del bucle infinito después de la primera consulta

    monkeypatch.setattr(pull_engine, "execute_fetch", espiar)

    with pytest.raises(_Cortar):
        asyncio.run(pull_engine.dictionary_sync_loop("tive", "prod"))

    assert capturado.get("auth_type") == "oauth2_client_credentials"
    assert capturado.get("token_url") == TOKEN_URL, (
        "El diccionario no pasó token_url: el token nunca se pediría"
    )
    assert capturado.get("token_body_format") == "multipart"
    assert "x-tive-account-id" in (capturado.get("headers") or ""), (
        "El diccionario no pasó los headers: Tive recibiría la consulta sin la cuenta"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Regresión: Protrack en producción
#
# El cambio en el sincronizador del diccionario toca un camino que Protrack
# usa en producción todos los días. Se verifica con su configuración REAL
# (la de recuperar_protrack.yaml) que la consulta que arma sea la misma de
# siempre: mismo auth_type, mismas credenciales, sin claves de OAuth colándose.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def base_con_protrack(tmp_path, monkeypatch):
    from app import database
    from app.core.crypto import encrypt

    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()

    from app.models.config_models import ProviderConfig
    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="protrack", env="test", provider_type="pull", is_active=True,
        use_mock=True, rc_user="AC_avl_Protrack",
        fetch_config_enc=encrypt(json.dumps({
            "url": "http://api.protrack365.com/api/track", "method": "GET",
            "auth_type": "protrack", "auth_user": "prueba.maersk", "auth_pass": "clave",
            "headers": "{}", "body": "{}",
        })),
        enrichment_config={
            "enabled": True, "timezone_offset": 0, "frequency": 23,
            "url": "http://api.protrack365.com/api/device/list?access_token=ACCESS_TOKEN",
            "method": "GET", "key_path": "record.0.imei", "value_path": "record.0.platenumber",
            "auth_type": "protrack", "auth_user": "prueba.maersk",
        },
    ))
    db.commit()
    db.close()
    yield
    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)


def test_el_diccionario_de_protrack_arma_la_misma_consulta_de_siempre(base_con_protrack, monkeypatch):
    capturado = {}

    class _Cortar(BaseException):
        pass

    async def espiar(fetch_cfg):
        capturado.update(fetch_cfg)
        raise _Cortar()

    monkeypatch.setattr(pull_engine, "execute_fetch", espiar)
    with pytest.raises(_Cortar):
        asyncio.run(pull_engine.dictionary_sync_loop("protrack", "test"))

    assert capturado["auth_type"] == "protrack"
    assert capturado["auth_user"] == "prueba.maersk"
    assert capturado["auth_pass"] == "clave", "Perdió la contraseña heredada del PULL"
    assert "access_token=ACCESS_TOKEN" in capturado["url"]
    for clave_oauth in ("token_url", "token_body_format", "scope"):
        assert clave_oauth not in capturado, f"Se coló {clave_oauth} en la consulta de Protrack"
    # Los headers de Protrack son "{}": si viajan, no agregan nada.
    assert json.loads(capturado.get("headers") or "{}") == {}
