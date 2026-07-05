"""Tests anti-regresión: documentan y previenen bugs ya fixeados.

Cada test lleva el ID del bug que previene:
  - M5b: .event_code no existe, el campo canónico es .code
  - dce0758: log_raw_payload faltaba en auditor.py
  - L2: verify_dashboard_auth no retornaba credentials
  - M3: QueueFactory instanciaba RedisQueue stub sin validar (debía lanzar NotImplementedError)
  - C3: RC_USE_MOCK default seguro — debe ser False en producción
"""
import pytest
import inspect


def test_bug_m5b_canonical_field_is_code_not_event_code(sample_schmitz_payload):
    """Bug M5b: El campo canónico es .code, no .event_code (que no existe)."""
    from app.providers.schmitz.mapper import map_schmitz_payload
    from app.schemas.canonical import RCCanonicalModel

    result = map_schmitz_payload(sample_schmitz_payload)

    assert isinstance(result, list), "map_schmitz_payload debe retornar una lista"
    assert len(result) > 0, "Debe retornar al menos un evento"

    first = result[0]
    assert isinstance(first, RCCanonicalModel), "Cada item debe ser RCCanonicalModel"
    assert hasattr(first, "code"), "El modelo canónico debe tener el campo 'code'"
    assert not hasattr(first, "event_code"), (
        "Bug M5b: el campo 'event_code' no debe existir — es un bug conocido"
    )


def test_bug_dce0758_log_raw_payload_exists_and_callable():
    """Bug dce0758: log_raw_payload faltaba en auditor.py y causaba ImportError."""
    from app.core.auditor import log_raw_payload

    assert callable(log_raw_payload), "log_raw_payload debe ser una función callable"


def test_bug_l2_verify_dashboard_auth_returns_credentials():
    """Bug L2: verify_dashboard_auth no retornaba credentials — el router recibía None."""
    from app.core.auth import verify_dashboard_auth
    src = inspect.getsource(verify_dashboard_auth)

    assert "return credentials" in src, (
        "Bug L2: verify_dashboard_auth debe retornar credentials explícitamente"
    )


def test_bug_m3_queue_factory_redis_raises_not_implemented():
    """Bug M3: QueueFactory no debe instanciar RedisQueue silenciosamente — debe lanzar NotImplementedError."""
    from app.core.queue_factory import QueueFactory
    src = inspect.getsource(QueueFactory)

    assert "NotImplementedError" in src, (
        "Bug M3: QueueFactory debe lanzar NotImplementedError si backend=redis (no instanciar stub)"
    )


def test_bug_c3_rc_use_mock_default_is_false():
    """Bug C3: RC_USE_MOCK debe tener default 'False' en rc_soap.py (no True — inseguro en producción)."""
    import inspect
    import app.services.rc_soap as rc_mod
    src = inspect.getsource(rc_mod)

    # Verificar que el default del os.getenv es "False" y no "True"
    assert '"False"' in src or "'False'" in src, (
        "Bug C3: RC_USE_MOCK debe tener default 'False' en rc_soap.py "
        "para evitar que producción use mock inadvertidamente"
    )
