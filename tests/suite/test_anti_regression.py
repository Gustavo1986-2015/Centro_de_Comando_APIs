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


def test_ningun_test_pisa_las_credenciales_sin_restaurarlas(tmp_path):
    """
    Invariante de la suite, no de la aplicación: un test que escribe
    DASHBOARD_USER/DASHBOARD_PASSWORD (o las llaves de cifrado) en os.environ
    sin restaurarlas contamina a todos los que vienen después (19 fallos
    intermitentes en el bloque de exportaciones).

    v1.9.9 (B-20): antes se buscaba el patrón en el TEXTO de los archivos de
    test. Ahora hay una guardia que se EJECUTA en cada test (conftest.py,
    _credenciales_intactas). Este test la pone a prueba: corre en un proceso
    aparte un test que pisa la contraseña sin restaurarla y exige que la
    guardia lo marque y deje el valor como estaba.
    """
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    raiz = Path(__file__).resolve().parents[2]
    carpeta = tmp_path / "suite_mini"
    (carpeta / "tests").mkdir(parents=True)
    (carpeta / "frontend").mkdir()
    shutil.copy(raiz / "tests" / "conftest.py", carpeta / "tests" / "conftest.py")
    (carpeta / "tests" / "test_infractor.py").write_text(
        "import os\n"
        "def test_pisa_la_clave():\n"
        "    os.environ['DASHBOARD_PASSWORD'] = 'pisada'\n"
        "def test_el_siguiente_la_ve_intacta():\n"
        "    assert os.environ['DASHBOARD_PASSWORD'] != 'pisada'\n", encoding="utf-8")
    entorno = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    entorno.update(PYTHONPATH=str(raiz), PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(carpeta),
                        str(carpeta / "tests")],
                       cwd=str(carpeta), env=entorno, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    salida = p.stdout + p.stderr
    assert "DASHBOARD_PASSWORD" in salida and "no la restauró" in salida, salida[-3000:]
    assert "2 passed" in salida and "1 error" in salida, salida[-3000:]


def test_bug_dce0758_log_raw_payload_exists_and_callable():
    """Bug dce0758: log_raw_payload faltaba en auditor.py y causaba ImportError."""
    from app.core.auditor import log_raw_payload

    assert callable(log_raw_payload), "log_raw_payload debe ser una función callable"


def test_bug_l2_verify_dashboard_auth_returns_credentials():
    """
    Bug L2: verify_dashboard_auth no retornaba credentials — el router recibía None.
    v1.9.9 (B-20): se EJECUTA (antes buscaba "return credentials" en el fuente).
    """
    import os
    from fastapi.security import HTTPBasicCredentials
    from app.core.auth import verify_dashboard_auth
    cred = HTTPBasicCredentials(username=os.environ["DASHBOARD_USER"], password=os.environ["DASHBOARD_PASSWORD"])
    assert verify_dashboard_auth(cred) is cred


def test_bug_m3_queue_factory_redis_raises_not_implemented(config_aislada, monkeypatch):
    """
    Bug M3: QueueFactory no debe instanciar un Redis de mentira — debe lanzar
    NotImplementedError. v1.9.9 (B-20): se EJECUTA la fábrica con la
    configuración en la base (antes buscaba el texto en el fuente).
    """
    from app.core.queue_factory import QueueFactory
    from app.core.sqlite_queue import SQLiteQueue
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    monkeypatch.setattr(QueueFactory, "_instances", {})
    db = get_session("system_config", "global")
    db.add_all([ProviderConfig(provider_name="conredis", env="prod", queue_backend="redis"),
                ProviderConfig(provider_name="conpg", env="prod", queue_backend="postgres"),
                ProviderConfig(provider_name="consqlite", env="prod", queue_backend="sqlite")])
    db.commit()
    db.close()
    with pytest.raises(NotImplementedError):
        QueueFactory.get_queue_service("conredis", "prod")
    with pytest.raises(ValueError):
        QueueFactory.get_queue_service("conpg", "prod")
    assert isinstance(QueueFactory.get_queue_service("consqlite", "prod"), SQLiteQueue)
    assert "conredis_prod" not in QueueFactory._instances


def test_bug_c3_rc_use_mock_default_is_false(tmp_path):
    """
    Bug C3: sin la variable, RC_USE_MOCK vale False (no True — inseguro en
    producción). v1.9.9 (B-20): se importa el módulo en un proceso SIN la
    variable y se mira el valor (antes buscaba '"False"' en el fuente).
    """
    assert _b20_en_proceso(tmp_path, """
        from app.services import rc_soap
        print(rc_soap.RC_USE_MOCK)
    """) == "False"


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
