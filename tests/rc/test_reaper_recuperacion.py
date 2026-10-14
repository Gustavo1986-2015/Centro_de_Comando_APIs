"""
Tests del reaper que rescata eventos atascados en 'processing'.

Estos tests usan una base SQLite real y ejercen la función real
(_recuperar_estancados_sync), parcheando solo el acceso a sesión. Verifican dos
cosas que antes no estaban cubiertas:

1. Zona horaria. El reaper compara `updated_at` (que se escribe en UTC) contra
   "ahora". Si "ahora" se toma en hora local y el proceso corre en un huso
   distinto de UTC —el contenedor usa America/Argentina/Buenos_Aires, UTC-3—,
   el umbral efectivo se desplaza tres horas y el rescate llega tarde. Para que
   el test detecte esa regresión aun corriendo en un CI en UTC, se fuerza el
   huso del proceso a Argentina: con el reloj viejo (local) el rescate no
   ocurre y el test falla; con el reloj corregido (UTC) ocurre y pasa.

2. Piso del umbral. Por más bajo que se configure, el umbral nunca baja de
   _PISO_UMBRAL_RESCATE_SEG, para no arrebatarle a un worker un lote en vuelo.
"""
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.db_models import Base, NormalizedRCEvent
from app.core.sqlite_queue import SQLiteQueue, _PISO_UMBRAL_RESCATE_SEG


def _utc_naive() -> datetime:
    """Mismo reloj con el que la aplicación escribe updated_at."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def tz_argentina():
    """
    Fuerza el huso del proceso a UTC-3 (como en producción) para que la
    diferencia entre hora local y UTC sea distinta de cero y el test pueda
    detectar el uso de un reloj equivocado, corra donde corra.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset no disponible en esta plataforma")
    previo = os.environ.get("TZ")
    os.environ["TZ"] = "America/Argentina/Buenos_Aires"
    time.tzset()
    # Sanity: el huso quedó efectivamente desfasado de UTC.
    assert datetime.now().hour != datetime.now(timezone.utc).hour or \
        (datetime.now(timezone.utc) - datetime.now().replace(tzinfo=None)) != timedelta(0)
    yield
    if previo is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previo
    time.tzset()


@pytest.fixture
def cola(tmp_path, monkeypatch):
    """Cola real sobre SQLite en disco; session_context apunta a esa base."""
    engine = create_engine(f"sqlite:///{tmp_path}/cola.db")
    Base.metadata.create_all(engine, tables=[NormalizedRCEvent.__table__])
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _fake_ctx(provider, env):
        db = Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr("app.core.sqlite_queue.session_context", _fake_ctx)
    return SQLiteQueue(), Session


def _evento(updated_at, status="processing", **kw):
    base = dict(
        provider="protrack", status=status, chassis_number="C1",
        latitude=1.0, longitude=2.0, speed=0, code="1",
        created_at=_utc_naive(), date=_utc_naive(),
        updated_at=updated_at,
    )
    base.update(kw)
    return NormalizedRCEvent(**base)


def _contar(Session, status):
    s = Session()
    try:
        return s.query(NormalizedRCEvent).filter(NormalizedRCEvent.status == status).count()
    finally:
        s.close()


# ── Zona horaria ────────────────────────────────────────────────────────────

def test_rescata_lo_atascado_y_no_toca_lo_en_vuelo(cola, tz_argentina):
    """
    Replica de la simulación de A1: lote viejo rescatado, lote en vuelo intacto.
    Con el reloj local viejo (UTC-3) el lote viejo NO se rescataría y este test
    falla; con UTC se rescata.
    """
    queue, Session = cola
    viejo = _utc_naive() - timedelta(seconds=600 + 300)   # bien pasado el umbral
    reciente = _utc_naive() - timedelta(seconds=5)         # en vuelo

    s = Session()
    s.add_all([_evento(viejo) for _ in range(2000)])
    s.add_all([_evento(reciente) for _ in range(50)])
    s.commit(); s.close()

    rescatados = queue._recuperar_estancados_sync("protrack", "prod", 600)

    assert rescatados == 2000
    assert _contar(Session, "pending") == 2000
    assert _contar(Session, "processing") == 50


def test_no_rescata_nada_dentro_del_umbral(cola, tz_argentina):
    """Un lote reciente (dentro del umbral) no debe tocarse."""
    queue, Session = cola
    reciente = _utc_naive() - timedelta(seconds=30)

    s = Session()
    s.add_all([_evento(reciente) for _ in range(100)])
    s.commit(); s.close()

    assert queue._recuperar_estancados_sync("protrack", "prod", 600) == 0
    assert _contar(Session, "processing") == 100


# ── Piso del umbral ─────────────────────────────────────────────────────────

def test_el_piso_protege_lo_que_esta_por_debajo_de_300(cola, tz_argentina):
    """
    Con un umbral configurado por debajo del piso (10 s), un evento atascado
    120 s NO debe rescatarse: el piso de 300 s manda. Uno de 400 s sí.
    """
    queue, Session = cola
    dentro_del_piso = _utc_naive() - timedelta(seconds=120)
    fuera_del_piso = _utc_naive() - timedelta(seconds=400)

    s = Session()
    s.add(_evento(dentro_del_piso, chassis_number="DENTRO"))
    s.add(_evento(fuera_del_piso, chassis_number="FUERA"))
    s.commit(); s.close()

    rescatados = queue._recuperar_estancados_sync("protrack", "prod", 10)

    assert rescatados == 1
    s = Session()
    try:
        aun_processing = s.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.status == "processing"
        ).one()
        assert aun_processing.chassis_number == "DENTRO"
    finally:
        s.close()


def test_umbral_configurable_por_encima_del_piso_se_respeta(cola, tz_argentina):
    """Por encima del piso, el valor configurado manda: 600 s no rescata a 400 s."""
    queue, Session = cola
    s = Session()
    s.add(_evento(_utc_naive() - timedelta(seconds=400)))   # < 600, se queda
    s.add(_evento(_utc_naive() - timedelta(seconds=700)))   # > 600, sale
    s.commit(); s.close()

    assert queue._recuperar_estancados_sync("protrack", "prod", 600) == 1


def test_el_piso_es_300():
    assert _PISO_UMBRAL_RESCATE_SEG == 300
