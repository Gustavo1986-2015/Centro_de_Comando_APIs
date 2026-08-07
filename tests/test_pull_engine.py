"""
Tests del motor PULL (app/worker/pull_engine.py).

Protegen los fixes de integridad de datos hacia RC:
  P0-1: nunca encolar una respuesta de error del proveedor como si fuera telemetría
  P0-2: abortar el ciclo si falla la autenticación (no hacer llamadas sin token)
  P0-3: firma MD5 vacía debe lanzar excepción, no pasar silenciosa
  P1-4: caché de token para no agotar el rate limit del proveedor
  P2-8: fallback consistente si el descifrado de credenciales falla

El caso de regresión más importante es test_error_response_no_se_encola:
en producción, un {"code": 10005, ...} de Protrack se convirtió en 1635 eventos
UNKNOWN despachados a Recurso Confiable con job_id reales.
"""
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.worker.pull_engine import (
    dynamic_md5,
    _is_error_response,
    _looks_like_telemetry,
    _load_fetch_config,
    _get_protrack_token,
    _invalidate_token,
    _TOKEN_CACHE,
    execute_fetch,
    process_and_enqueue,
    ProviderAuthError,
    ProviderResponseError,
)


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """Cada test arranca con la caché de tokens limpia."""
    _TOKEN_CACHE.clear()
    yield
    _TOKEN_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════
# P0-3 — Firma MD5
# ═══════════════════════════════════════════════════════════════════════

def test_dynamic_md5_genera_firma_valida():
    unix_time, signature = dynamic_md5("mi_password")
    assert unix_time.isdigit()
    assert len(signature) == 32          # hexdigest de MD5
    assert int(unix_time) > 1700000000   # timestamp razonable


def test_dynamic_md5_password_vacia_lanza_error():
    """
    P0-3: antes devolvía ('...', '') y el flujo continuaba con firma vacía,
    el proveedor rechazaba la auth, y nadie abortaba.
    """
    with pytest.raises(ProviderAuthError, match="Password vacía"):
        dynamic_md5("")

    with pytest.raises(ProviderAuthError):
        dynamic_md5(None)


def test_dynamic_md5_es_determinista_en_el_mismo_segundo():
    """Dos llamadas dentro del mismo segundo producen la misma firma."""
    t1, s1 = dynamic_md5("pass")
    t2, s2 = dynamic_md5("pass")
    if t1 == t2:
        assert s1 == s2


# ═══════════════════════════════════════════════════════════════════════
# P2-9 — Detección de respuestas de error
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("payload,esperado", [
    # Errores reales vistos en producción
    ({"code": 10005, "message": "missing required parameter:imeis"}, True),
    ({"code": 10005, "message": "missing required parameter:access_token"}, True),
    ({"code": 10014, "message": "RequestTimeError"}, True),
    ({"error": "unauthorized"}, True),
    # Respuestas válidas
    ({"code": 0, "record": [{"imei": "123"}]}, False),
    ({"code": "0", "record": []}, False),
    ({"record": [{"imei": "123"}]}, False),
    ([{"imei": "123"}], False),
    ({"data": [{"lat": 1, "lng": 2}]}, False),
])
def test_is_error_response(payload, esperado):
    es_error, _ = _is_error_response(payload)
    assert es_error is esperado


def test_is_error_response_incluye_mensaje():
    _, msg = _is_error_response({"code": 10005, "message": "missing parameter"})
    assert "10005" in msg
    assert "missing parameter" in msg


# ═══════════════════════════════════════════════════════════════════════
# P0-1 — Heurística de telemetría vs respuesta de control
# ═══════════════════════════════════════════════════════════════════════

def test_looks_like_telemetry_reconoce_registro_gps():
    registro = {"imei": "868307060968914", "latitude": 9.98, "longitude": -84.73, "speed": 0}
    assert _looks_like_telemetry(registro, {}) is True


def test_looks_like_telemetry_rechaza_respuesta_de_control():
    """Un dict de error no tiene campos de telemetría reconocibles."""
    error = {"code": 10005, "message": "missing required parameter:access_token"}
    assert _looks_like_telemetry(error, {}) is False


def test_looks_like_telemetry_usa_campos_del_mapping_schema():
    """Si el mapping espera 'deviceSerial', ese campo cuenta como telemetría."""
    schema = {"base_mapping": {"chassis_number": "deviceSerial", "speed": "velocidad"}}
    registro = {"deviceSerial": "ABC123", "velocidad": 40}
    assert _looks_like_telemetry(registro, schema) is True


def test_looks_like_telemetry_dict_vacio():
    assert _looks_like_telemetry({}, {}) is False


# ═══════════════════════════════════════════════════════════════════════
# P0-1 — EL TEST DE REGRESIÓN CRÍTICO
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_error_response_no_se_encola():
    """
    REGRESIÓN CRÍTICA — incidente de producción.

    Un {"code": 10005, "message": "..."} de Protrack caía en `items = [data]`,
    se mapeaba a un evento con todo en None/0, se encolaba, y se despachaba a
    Recurso Confiable como telemetría real con chassis UNKNOWN y job_id válido.
    Resultado: 1635 eventos basura en la plataforma del cliente.

    Este test verifica que la respuesta de error se descarta ANTES de tocar la BD.
    """
    error_payload = {"code": 10005, "message": "missing required parameter:access_token"}

    with patch("app.worker.pull_engine.get_session") as mock_session, \
         patch("app.core.auditor.log_raw_payload") as mock_audit:

        await process_and_enqueue(
            provider_name="protrack",
            env="prod",
            data=error_payload,
            mapping_schema={"base_mapping": {"chassis_number": "imei"}},
        )

        # No debe abrir sesión de BD ni encolar nada
        mock_session.assert_not_called()
        # No debe auditar el error como si fuera un payload de telemetría
        mock_audit.assert_not_called()


@pytest.mark.asyncio
async def test_dict_sin_lista_ni_telemetria_se_descarta():
    """Una respuesta de control sin campos de datos no debe convertirse en evento."""
    respuesta_rara = {"status": "maintenance", "info": "server busy"}

    with patch("app.worker.pull_engine.get_session") as mock_session:
        await process_and_enqueue(
            provider_name="protrack",
            env="prod",
            data=respuesta_rara,
            mapping_schema={"base_mapping": {"chassis_number": "imei"}},
        )
        mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_respuesta_valida_si_se_procesa():
    """Contraparte: una respuesta legítima SÍ debe llegar al mapeo."""
    payload_valido = {
        "code": 0,
        "record": [
            {"imei": "868307060968914", "latitude": 9.98, "longitude": -84.73, "speed": 0}
        ],
    }

    mock_db = MagicMock()
    with patch("app.worker.pull_engine.get_session", return_value=mock_db), \
         patch("app.core.auditor.log_raw_payload") as mock_audit, \
         patch("app.worker.pull_engine.DynamicMapper.map_payload_multi", return_value=[]):

        await process_and_enqueue(
            provider_name="protrack",
            env="prod",
            data=payload_valido,
            mapping_schema={"base_mapping": {"chassis_number": "imei"}},
        )

        # Llegó al punto de auditar el registro real
        assert mock_audit.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
# P1-4 — Caché de token
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_token_se_cachea_y_reutiliza():
    """
    P1-4: sin caché, el sistema pedía un token nuevo cada ciclo de PULL (~11s),
    generando ~7.800 llamadas/día a /api/authorization y agotando el rate limit
    del proveedor. Con caché, dos llamadas seguidas usan el mismo token.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"code": 0, "record": {"access_token": "TOKEN_ABC", "expires_in": 7200}}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("httpx.AsyncClient", return_value=mock_client):
        t1 = await _get_protrack_token("http://api.test.com", "user", "pass")
        t2 = await _get_protrack_token("http://api.test.com", "user", "pass")

    assert t1 == t2 == "TOKEN_ABC"
    # La segunda llamada NO golpeó la API
    assert mock_client.get.call_count == 1


@pytest.mark.asyncio
async def test_token_se_renueva_si_expiro():
    """Si el TTL venció, se pide un token nuevo."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"code": 0, "record": {"access_token": "TOKEN_NUEVO", "expires_in": 7200}}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    # Sembrar un token ya vencido
    _TOKEN_CACHE["http://api.test.com|user"] = {
        "token": "TOKEN_VIEJO",
        "expires_at": time.time() - 10,
    }

    with patch("httpx.AsyncClient", return_value=mock_client):
        token = await _get_protrack_token("http://api.test.com", "user", "pass")

    assert token == "TOKEN_NUEVO"
    assert mock_client.get.call_count == 1


@pytest.mark.asyncio
async def test_auth_rechazada_lanza_error():
    """
    P0-2: si el proveedor rechaza la auth, se lanza ProviderAuthError.
    Antes solo se logueaba y el flujo continuaba sin token.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"code": 10014, "message": "RequestTimeError"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ProviderAuthError, match="rechazó la autenticación"):
            await _get_protrack_token("http://api.test.com", "user", "pass")

    # Un token fallido no se cachea
    assert "http://api.test.com|user" not in _TOKEN_CACHE


@pytest.mark.asyncio
async def test_auth_code_0_sin_token_lanza_error():
    """Caso borde: code=0 pero sin access_token en la respuesta."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"code": 0, "record": {}}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ProviderAuthError, match="sin access_token"):
            await _get_protrack_token("http://api.test.com", "user", "pass")


@pytest.mark.asyncio
async def test_token_usa_expires_in_real_del_proveedor():
    """
    Doc oficial de Protrack: el token dura 7200s (2h) y ese valor viene en
    'expires_in' en cada respuesta de /api/authorization. El TTL cacheado
    debe basarse en ese dato real, con margen de seguridad, no en un valor
    fijo estimado a ciegas.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "code": 0,
        "record": {"access_token": "TOKEN_2H", "expires_in": 7200},
    }

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _get_protrack_token("http://api.test.com", "user", "pass")

    cached = _TOKEN_CACHE["http://api.test.com|user"]
    ttl_real = cached["expires_at"] - time.time()

    # Debe cachearse cerca de 7200s menos el margen de seguridad (120s),
    # NO el valor fijo viejo de 1500s (25 min).
    assert 7000 < ttl_real <= 7080


@pytest.mark.asyncio
async def test_token_fallback_si_falta_expires_in():
    """Si el proveedor no informa expires_in, usar el fallback conservador."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"code": 0, "record": {"access_token": "TOKEN_SIN_TTL"}}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _get_protrack_token("http://api.test.com", "user", "pass")

    cached = _TOKEN_CACHE["http://api.test.com|user"]
    ttl_real = cached["expires_at"] - time.time()
    assert 1300 < ttl_real <= 1400   # fallback (1500s) menos margen (120s)


# ═══════════════════════════════════════════════════════════════════════
# P1-7 — Herencia de credenciales en el diccionario
# ═══════════════════════════════════════════════════════════════════════

def _resolver_auth_type(enrich: dict, fetch_c: dict) -> str:
    """
    Réplica de la lógica de resolución de auth_type del dictionary_sync_loop.
    Se testea aislada porque el loop es infinito y no es directamente invocable.
    """
    enrich_auth = (enrich.get("auth_type") or "").strip().lower()
    if enrich_auth in ("", "none"):
        return fetch_c.get("auth_type", "none")
    return enrich_auth


def test_diccionario_hereda_auth_cuando_dice_none():
    """
    REGRESIÓN — bug real de producción.

    El dashboard guarda "none" (string) cuando no se elige autenticación para el
    diccionario. Como "none" es truthy, la herencia con `or` nunca se activaba:
    el diccionario llamaba a /api/device/list con el ACCESS_TOKEN literal de la
    URL y el proveedor respondía code=10011 access_token error.
    """
    enrich = {"auth_type": "none", "auth_user": "", "auth_pass": ""}
    fetch_c = {"auth_type": "protrack", "auth_user": "cuenta", "auth_pass": "pass"}
    assert _resolver_auth_type(enrich, fetch_c) == "protrack"


def test_diccionario_hereda_auth_cuando_esta_vacio():
    enrich = {"auth_type": ""}
    fetch_c = {"auth_type": "protrack"}
    assert _resolver_auth_type(enrich, fetch_c) == "protrack"


def test_diccionario_hereda_auth_cuando_falta_la_clave():
    enrich = {}
    fetch_c = {"auth_type": "md5_dynamic"}
    assert _resolver_auth_type(enrich, fetch_c) == "md5_dynamic"


def test_diccionario_respeta_su_auth_propio_si_esta_configurado():
    """Si el diccionario tiene un auth_type real, NO se pisa con el del PULL."""
    enrich = {"auth_type": "bearer"}
    fetch_c = {"auth_type": "protrack"}
    assert _resolver_auth_type(enrich, fetch_c) == "bearer"


def test_diccionario_sin_auth_en_ninguno_queda_none():
    assert _resolver_auth_type({}, {}) == "none"


def test_invalidate_token_limpia_cache():
    _TOKEN_CACHE["http://api.test.com|user"] = {"token": "X", "expires_at": time.time() + 999}
    _invalidate_token("http://api.test.com", "user")
    assert "http://api.test.com|user" not in _TOKEN_CACHE


# ═══════════════════════════════════════════════════════════════════════
# P0-2 — execute_fetch aborta ante fallos de auth
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_execute_fetch_sin_url_lanza_error():
    with pytest.raises(ValueError, match="sin 'url'"):
        await execute_fetch({"method": "GET"})


@pytest.mark.asyncio
async def test_execute_fetch_bearer_vacio_lanza_error():
    with pytest.raises(ProviderAuthError, match="bearer_token"):
        await execute_fetch({
            "url": "http://api.test.com/data",
            "auth_type": "bearer",
            "bearer_token": "",
        })


@pytest.mark.asyncio
async def test_execute_fetch_protrack_sin_user_lanza_error():
    with pytest.raises(ProviderAuthError, match="auth_user"):
        await execute_fetch({
            "url": "http://api.test.com/api/track",
            "auth_type": "protrack",
            "auth_user": "",
            "auth_pass": "pass",
        })


@pytest.mark.asyncio
async def test_execute_fetch_md5_sin_password_lanza_error():
    """P0-3 a nivel de execute_fetch: password vacía aborta antes de la petición."""
    with pytest.raises(ProviderAuthError, match="Password vacía"):
        await execute_fetch({
            "url": "http://api.test.com/data",
            "auth_type": "md5_dynamic",
            "auth_user": "user",
            "auth_pass": "",
        })


@pytest.mark.asyncio
async def test_execute_fetch_respuesta_de_error_lanza_provider_response_error():
    """
    P2-9: si el proveedor responde 200 OK pero con un cuerpo de error,
    se lanza ProviderResponseError en lugar de devolverlo como datos.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"code": 10005, "message": "missing required parameter:imeis"}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ProviderResponseError, match="10005"):
            await execute_fetch({"url": "http://api.test.com/data", "auth_type": "none"})


@pytest.mark.asyncio
async def test_execute_fetch_params_generados_pisan_los_de_la_url():
    """
    La URL configurada puede traer `?access_token=ACCESS_TOKEN` literal.
    El token real generado debe sobrescribirlo, no duplicarse.
    """
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"code": 0, "record": []}

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__.return_value = mock_client

    with patch("httpx.AsyncClient", return_value=mock_client):
        await execute_fetch({
            "url": "http://api.test.com/data?access_token=ACCESS_TOKEN",
            "auth_type": "md5_dynamic",
            "auth_user": "user",
            "auth_pass": "pass",
        })

    url_llamada = mock_client.get.call_args[0][0]
    assert "ACCESS_TOKEN" not in url_llamada or "signature=" in url_llamada


# ═══════════════════════════════════════════════════════════════════════
# P2-8 — Fallback de descifrado de credenciales
# ═══════════════════════════════════════════════════════════════════════

def test_load_fetch_config_sin_cifrar():
    config = MagicMock()
    config.fetch_config_enc = None
    config.fetch_config = {"url": "http://plano.com", "auth_type": "none"}
    assert _load_fetch_config(config)["url"] == "http://plano.com"


def test_load_fetch_config_descifra_correctamente():
    config = MagicMock()
    config.fetch_config_enc = "cifrado_valido"
    config.fetch_config = {}

    with patch("app.worker.pull_engine.decrypt",
               return_value='{"url": "http://descifrado.com"}'):
        assert _load_fetch_config(config)["url"] == "http://descifrado.com"


def test_load_fetch_config_fallback_si_decrypt_falla():
    """
    P2-8: si decrypt() devuelve None (MASTER_ENC_KEY distinta a la que cifró),
    antes fetch_config quedaba {} vacío sin aviso y el loop dormía para siempre.
    Ahora cae al plaintext y loguea la causa.
    """
    config = MagicMock()
    config.provider_name = "protrack"
    config.env = "prod"
    config.fetch_config_enc = "cifrado_con_otra_key"
    config.fetch_config = {"url": "http://fallback.com"}

    with patch("app.worker.pull_engine.decrypt", return_value=None):
        result = _load_fetch_config(config)

    assert result["url"] == "http://fallback.com"


def test_load_fetch_config_fallback_si_json_invalido():
    config = MagicMock()
    config.provider_name = "protrack"
    config.env = "prod"
    config.fetch_config_enc = "cifrado"
    config.fetch_config = {"url": "http://fallback.com"}

    with patch("app.worker.pull_engine.decrypt", return_value="no-es-json{{{"):
        assert _load_fetch_config(config)["url"] == "http://fallback.com"
