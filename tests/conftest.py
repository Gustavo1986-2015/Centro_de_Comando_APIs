"""Fixtures compartidas. CRÍTICO: setear env vars ANTES de importar app.*
El orden de imports aquí importa: os.environ se setea primero para que
cualquier import de app.* que suceda en los módulos de test ya vea los valores correctos.
"""
import os
import sys
import pytest
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet

# ── Fernet key válida generada una única vez para toda la sesión de tests ────
# Fernet exige exactamente 32 bytes url-safe base64. Se genera en runtime.
_TEST_FERNET_KEY = Fernet.generate_key().decode()

# ── Env vars de aislamiento — DEBEN ir antes de cualquier import de app.* ──────
os.environ["APP_ENV"] = "development"
os.environ["RC_USE_MOCK"] = "True"
os.environ["DASHBOARD_USER"] = "test_admin"
os.environ["DASHBOARD_PASSWORD"] = "test_pass_123"
os.environ["MASTER_ENC_KEY"] = _TEST_FERNET_KEY
os.environ["RC_TOKEN_ENC_KEY"] = _TEST_FERNET_KEY

# Log aislado: la suite provoca errores a propósito (descifrados que fallan,
# respuestas de error de proveedores, tokens inválidos). Sin esto se escriben
# en el mismo archivo que la aplicación y aparecen en la consola del panel
# mezclados con los errores reales, que es lo que hay que poder distinguir.
_TEST_LOG_DIR = Path(tempfile.gettempdir()) / "cca_test_logs"
_TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
os.environ["LOG_FILE_PATH"] = str(_TEST_LOG_DIR / "tests.jsonl")

# Limpiar caché interno de crypto.py para que use la key de test (no la de .env)
try:
    import app.core.crypto as _crypto
    _crypto._MASTER_KEY_CACHE = None
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(scope="session", autouse=True)
def limpiar_log_de_tests():
    """Arranca cada sesión con el log de tests vacío."""
    destino = Path(os.environ["LOG_FILE_PATH"])
    try:
        if destino.exists():
            destino.unlink()
    except OSError:
        pass
    yield destino


@pytest.fixture
def sample_schmitz_payload():
    """Payload mínimo válido de Schmitz v3 para tests del mapper.
    El mapper busca 'ChassisNumber' o 'Plate' en la RAÍZ del payload.
    """
    return {
        "ChassisNumber": "TEST123456",
        "Header": {
            "Customer": {"Id": "test_customer", "Name": "Test"},
            "SerialNumber": "TEST123456",
            "Timestamp": "2026-06-29T10:00:00Z"
        },
        "Events": [],
        "StatusData": [{
            "Position": {
                "Latitude": -34.6037,
                "Longitude": -58.3816,
                "GPSSpeed": {"exists": True, "Value": 45.5},
                "GPSHeading": 180.0
            }
        }],
        "Reason": {"ItemElementName": "Standard", "Value": "Status"}
    }


@pytest.fixture
def sample_protrack_payload():
    """Payload mínimo válido de Protrack para tests del mapper dinámico."""
    return {
        "code": 0,
        "msg": "success",
        "data": [{
            "imei": "868166053130217",
            "name": "Test Vehicle",
            "lat": "-34.6037",
            "lng": "-58.3816",
            "speed": "45.5",
            "course": "180",
            "time": "2026-06-29 10:00:00"
        }]
    }
