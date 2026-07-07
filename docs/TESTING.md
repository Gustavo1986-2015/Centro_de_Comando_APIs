# Testing Automatizado — Centro de Comando APIs

## Correr los tests

```bash
# Instalar dependencias de testing (una sola vez)
pip install pytest pytest-asyncio

# Ejecutar suite completa
pytest tests/

# Output esperado
# 34 passed in ~5s
```

## Estructura

| Archivo | Tests | Qué valida |
|---------|-------|------------|
| `test_crypto.py` | 5 | Envelope Encryption (encrypt/decrypt reversible, Fernet prefix, None/vacío) |
| `test_fail_closed.py` | 4 | Auth PUSH fail-closed (webhook dinámico, schmitz usa DB no env, return credentials) |
| `test_migrations.py` | 4 | Migraciones idempotentes (PRAGMA, ADD COLUMN, seed, legacy SCHMITZ_API_KEY) |
| `test_anti_regression.py` | 5 | Bugs ya fixeados (M5b, dce0758, L2, M3, C3) |
| `test_schmitz_mapper.py` | 4 | Mapper Schmitz v3 (lista, chassis sanitizado, GPS rango, Events vacío) |
| `test_protrack_mapper.py` | 3 | Mapper Protrack (estructura payload, schema dinámico, MD5 auth) |
| `test_state_dedup.py` | 9 | Motor Anti-State Flooding PULL (base_code, transiciones, toggle, TTL cleanup) |

## Aislamiento — NO toca producción

```
DB temporal     → tempfile automático (no toca db/)
MASTER_ENC_KEY  → "test_master_key_for_pytest_only_32b=" (hardcodeada en conftest.py)
RC_USE_MOCK     → True (no llama a Recurso Confiable)
Credenciales    → test_admin / test_pass_123 (no toca .env)
```

## Añadir tests para una API nueva

1. Crear `tests/test_{proveedor}_mapper.py` siguiendo el patrón de `test_schmitz_mapper.py`
2. Usar el fixture `sample_{proveedor}_payload` definido en `conftest.py` (o crear uno nuevo)
3. Correr `pytest tests/` y confirmar que pasan todos los tests anteriores antes de mergear

```python
# Ejemplo mínimo para un provider nuevo
import pytest
from app.providers.mi_proveedor.mapper import map_mi_proveedor_payload
from app.schemas.canonical import RCCanonicalModel

def test_mi_proveedor_mapper_returns_list(sample_mi_proveedor_payload):
    result = map_mi_proveedor_payload(sample_mi_proveedor_payload)
    assert isinstance(result, list)
    assert len(result) > 0
    assert isinstance(result[0], RCCanonicalModel)
```

## Anti-regresión

Los 5 tests en `test_anti_regression.py` son **documentación viva de bugs ya fixeados**.
Si fallan, alguien reintrodujo un bug conocido. Investigar antes de mergear.

| ID Bug | Test | Descripción |
|--------|------|-------------|
| M5b | `test_bug_m5b_canonical_field_is_code_not_event_code` | Campo canónico es `.code`, no `.event_code` |
| dce0758 | `test_bug_dce0758_log_raw_payload_exists_and_callable` | `log_raw_payload` faltaba en `auditor.py` |
| L2 | `test_bug_l2_verify_dashboard_auth_returns_credentials` | `verify_dashboard_auth` no retornaba credentials |
| M3 | `test_bug_m3_queue_factory_redis_raises_not_implemented` | `QueueFactory` no validaba backend Redis |
| C3 | `test_bug_c3_rc_use_mock_default_is_false` | `RC_USE_MOCK` default inseguro en producción |
