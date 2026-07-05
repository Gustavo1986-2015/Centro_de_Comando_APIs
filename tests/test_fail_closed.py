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
