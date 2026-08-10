"""Tests de comportamiento fail-closed en autenticación.

Valida que:
  - El webhook dinámico rechaza providers inexistentes (401/404)
  - dynamic_webhook_receive tiene lógica de auth hardened (inspección de código fuente)
  - schmitz.py no lee API key desde os.getenv (usa DB cifrada)
  - verify_dashboard_auth retorna las credenciales tras validación exitosa
"""
import pytest
import inspect


def test_dynamic_webhook_nonexistent_provider_returns_401_or_404():
    """Webhook dinámico con provider inexistente debe retornar 401 o 404, nunca 200/202."""
    import base64
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app, raise_server_exceptions=False)
    creds = base64.b64encode(b"test_admin:test_pass_123").decode()

    response = client.post(
        "/webhook/dynamic/proveedor_inexistente_xyz?env=test",
        headers={
            "Authorization": f"Basic {creds}",
            "x-api-key": "cualquier_key_falsa",
        },
        json={"test": "data"},
    )
    assert response.status_code in (401, 404), (
        f"Se esperaba 401 o 404 para provider inexistente, se recibió {response.status_code}"
    )


def test_dynamic_webhook_has_fail_closed_auth_logic():
    """dynamic_webhook_receive debe tener lógica de auth fail-closed en su código fuente."""
    from app.api.routers.dynamic_webhook import dynamic_webhook_receive

    src = inspect.getsource(dynamic_webhook_receive)
    # La función delega auth en _validate_dynamic_auth (Depends), que lanza 401
    # Verificamos que el módulo entero tenga las referencias correctas
    from app.api.routers import dynamic_webhook as dw_module
    module_src = inspect.getsource(dw_module)

    assert "401" in module_src, "El módulo debe tener lógica de rechazo 401"
    assert "webhook_auth_secret_enc" in module_src, (
        "Debe leer la API key cifrada desde DB, no desde env"
    )


def test_schmitz_does_not_read_api_key_from_env():
    """schmitz.py no debe leer SCHMITZ_API_KEY desde os.getenv (usa DB cifrada)."""
    from app.api.routers import schmitz
    src = inspect.getsource(schmitz)

    # No debe haber os.getenv("SCHMITZ_API_KEY") en el router actual
    assert "os.getenv" not in src or "SCHMITZ_API_KEY" not in src, (
        "schmitz.py no debe leer la API key directamente desde variable de entorno"
    )


def test_verify_dashboard_auth_returns_credentials():
    """verify_dashboard_auth debe retornar las credenciales tras validación exitosa."""
    from app.core.auth import verify_dashboard_auth
    src = inspect.getsource(verify_dashboard_auth)

    assert "return credentials" in src, (
        "verify_dashboard_auth debe retornar credentials (bug L2: faltaba este return)"
    )


# ── Aislamiento entre entornos de Schmitz ────────────────────────────────────
# Había un fallback que, si faltaba la configuración del entorno pedido, tomaba
# "cualquier configuración de schmitz". Eso permitía que una petición a prod se
# autenticara con la clave de test: exactamente lo contrario a separarlos.

def test_la_clave_de_un_entorno_no_abre_el_otro(monkeypatch):
    from unittest.mock import MagicMock, patch
    from fastapi import HTTPException
    from app.api.routers.schmitz import _validate_schmitz_auth

    solicitud = MagicMock()
    solicitud.headers = {"x-api-key": "CLAVE_DE_TEST"}

    # La base solo tiene configuración para 'test'
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None

    from app.api.routers.schmitz import invalidar_cache_auth
    invalidar_cache_auth()

    with patch("app.api.routers.schmitz.get_session", return_value=db):
        with pytest.raises(HTTPException) as exc:
            _validate_schmitz_auth(solicitud, env="prod")
    invalidar_cache_auth()

    assert exc.value.status_code == 401
    assert "prod" in str(exc.value.detail)


def test_un_entorno_sin_clave_rechaza_en_lugar_de_tomar_la_de_otro():
    """
    El caso concreto: la fila de prod existe (aparece en el panel) pero con la
    API key vacía. Debe rechazar, no caer en la configuración de test.
    """
    from unittest.mock import MagicMock, patch
    from fastapi import HTTPException
    from app.api.routers.schmitz import _validate_schmitz_auth

    solicitud = MagicMock()
    solicitud.headers = {"x-api-key": "CLAVE_DE_TEST"}

    config_sin_clave = MagicMock()
    config_sin_clave.webhook_auth_secret_enc = None

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = config_sin_clave

    from app.api.routers.schmitz import invalidar_cache_auth
    invalidar_cache_auth()

    with patch("app.api.routers.schmitz.get_session", return_value=db):
        with pytest.raises(HTTPException) as exc:
            _validate_schmitz_auth(solicitud, env="prod")
    invalidar_cache_auth()

    assert exc.value.status_code == 401
    assert "API key" in str(exc.value.detail)


def test_no_queda_ningun_fallback_entre_entornos():
    """
    La consulta debe filtrar siempre por entorno. Un filter_by sin `env` sobre
    provider_config reintroduciría el cruce de credenciales.
    """
    import inspect
    from app.api.routers import schmitz

    # La lectura ahora vive en _clave_esperada, que es lo que consulta la base
    fuente = inspect.getsource(schmitz._clave_esperada)
    assert 'filter_by(provider_name="schmitz")' not in fuente, (
        "Hay una búsqueda de configuración sin filtrar por entorno"
    )
    assert "env=entorno" in fuente


# ── Costo de la validación en el camino con SLA ──────────────────────────────
# La validación abría una sesión a la base de configuración en cada petición.
# Medido: 1,13 ms de media pero hasta 125 ms bajo contención, sobre la misma
# base que recibe las estadísticas diarias. A 40 mensajes por segundo eso
# producía picos de latencia y peticiones que agotaban su tiempo de espera.

def test_la_validacion_no_lee_la_base_en_cada_peticion():
    from unittest.mock import MagicMock, patch
    from app.api.routers import schmitz

    schmitz.invalidar_cache_auth()

    config = MagicMock()
    config.webhook_auth_secret_enc = "cifrado"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = config

    lecturas = {"n": 0}

    def _sesion(*args, **kwargs):
        lecturas["n"] += 1
        return db

    solicitud = MagicMock()
    solicitud.headers = {"x-api-key": "CLAVE"}

    with patch("app.api.routers.schmitz.get_session", side_effect=_sesion), \
         patch("app.api.routers.schmitz.decrypt", return_value="CLAVE"):
        for _ in range(100):
            schmitz._validate_schmitz_auth(solicitud, env="test")

    assert lecturas["n"] == 1, f"Se leyó la base {lecturas['n']} veces en 100 peticiones"
    schmitz.invalidar_cache_auth()


def test_el_cache_es_independiente_por_entorno():
    """Una clave cacheada para un entorno no debe servir para el otro."""
    from app.api.routers import schmitz

    schmitz.invalidar_cache_auth()
    schmitz._auth_cache["test"] = (9e12, "CLAVE_TEST")

    assert schmitz._auth_cache.get("prod") is None
    schmitz.invalidar_cache_auth()


def test_invalidar_el_cache_fuerza_la_relectura():
    """Al cambiar la clave desde el panel, el cambio debe tomar efecto."""
    from app.api.routers import schmitz

    schmitz._auth_cache["test"] = (9e12, "VIEJA")
    schmitz.invalidar_cache_auth("test")

    assert "test" not in schmitz._auth_cache


def test_un_fallo_al_leer_la_configuracion_rechaza(monkeypatch):
    """
    Si la base no responde, no se puede validar a nadie: se rechaza en lugar de
    dejar pasar.
    """
    from unittest.mock import MagicMock, patch
    from fastapi import HTTPException
    from app.api.routers import schmitz

    schmitz.invalidar_cache_auth()
    solicitud = MagicMock()
    solicitud.headers = {"x-api-key": "CUALQUIERA"}

    def _falla(*args, **kwargs):
        raise RuntimeError("base no disponible")

    with patch("app.api.routers.schmitz.get_session", side_effect=_falla):
        with pytest.raises(HTTPException) as exc:
            schmitz._validate_schmitz_auth(solicitud, env="test")

    assert exc.value.status_code == 401
    schmitz.invalidar_cache_auth()
