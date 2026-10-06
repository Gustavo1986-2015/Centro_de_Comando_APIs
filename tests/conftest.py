"""Fixtures compartidas. CRÍTICO: setear env vars ANTES de importar app.*
El orden de imports aquí importa: os.environ se setea primero para que
cualquier import de app.* que suceda en los módulos de test ya vea los valores correctos.
"""
import os
import shutil
import sqlite3
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

RAIZ_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ_REPO))

# ── Directorio de trabajo aislado (v1.9.5) ───────────────────────────────────
# Todas las bases del hub son relativas al directorio de trabajo (./db/...,
# ./audit/...). Lanzada desde la carpeta del hub en vivo, la suite escribía en
# SU configuración: el 05/10/2026 test_mock_mode, que usa la primera
# integración de la base, dejó schmitz/prod en REAL sin dejar rastro en el log.
# La suite corre en una carpeta temporal con una copia de frontend/ (plantillas
# y estáticos que lee la app). Se lance desde donde se lance, no toca el
# directorio desde el que se la corre.
DIRECTORIO_DE_LA_SUITE = Path(tempfile.mkdtemp(prefix="cca_suite_"))
shutil.copytree(RAIZ_REPO / "frontend", DIRECTORIO_DE_LA_SUITE / "frontend")
_CWD_DEL_LANZAMIENTO = os.getcwd()
os.chdir(DIRECTORIO_DE_LA_SUITE)


def _config_del_lanzamiento():
    """Filas de provider_config de la carpeta desde la que se lanzó la suite,
    leídas en solo lectura. None si allí no hay base."""
    ruta = Path(_CWD_DEL_LANZAMIENTO) / "db" / "system_config_global.db"
    if not ruta.exists():
        return None
    con = sqlite3.connect(f"{ruta.as_uri()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT * FROM provider_config ORDER BY id").fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()


_CONFIG_ANTES = _config_del_lanzamiento()

# Limpiar caché interno de crypto.py para que use la key de test (no la de .env)
try:
    import app.core.crypto as _crypto
    _crypto._MASTER_KEY_CACHE = None
except Exception:
    pass


@pytest.fixture(scope="session", autouse=True)
def guardia_de_la_config_real():
    """La configuración de la carpeta desde la que se lanzó la suite tiene que
    terminar idéntica. Si cambió, la suite se marca con error."""
    yield
    os.chdir(_CWD_DEL_LANZAMIENTO)
    despues = _config_del_lanzamiento()
    shutil.rmtree(DIRECTORIO_DE_LA_SUITE, ignore_errors=True)
    assert despues == _CONFIG_ANTES, (
        f"La suite modificó la configuración real de {_CWD_DEL_LANZAMIENTO}: "
        f"ningún test puede escribir en ./db del directorio de lanzamiento.")


@pytest.fixture
def config_aislada(tmp_path, monkeypatch):
    """Base de configuración propia del test, vacía, en tmp_path. Lleva su
    copia de frontend/ porque la app lee plantillas y estáticos relativos."""
    from app import database
    from app.core import config_cache, rate_limit

    shutil.copytree(DIRECTORIO_DE_LA_SUITE / "frontend", tmp_path / "frontend")
    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()
    config_cache.invalidate()
    yield tmp_path
    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()
    config_cache.invalidate()


@pytest.fixture(scope="session", autouse=True)
def descartes_aislados(tmp_path_factory):
    """El registro de descartes de la suite va a una carpeta temporal, no a db/."""
    from app.core import descartes
    original = descartes.DIRECTORIO
    descartes.DIRECTORIO = str(tmp_path_factory.mktemp("descartes"))
    yield
    descartes.esperar_escritura(2.0)
    descartes.DIRECTORIO = original


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
        # Presente en los 6975 crudos reales de Schmitz medidos. Desde la v1.9.2
        # un evento sin fecha no entra (app/core/contrato.py).
        "DeviceTime": "2026-06-29T10:00:00Z",
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
