"""
Tests del rescate de eventos atascados en 'processing' (hallazgo A1).

El problema: un lote se marca 'processing' al tomarlo y cambia de estado al
terminar de despacharse. Si el proceso muere entre esos dos momentos, o si una
excepción corta el camino antes de escribir los resultados, esos eventos quedan
en un limbo: ningún ciclo los vuelve a tomar porque no están pendientes, y la
purga no los toca porque no están enviados. Se acumulan invisibles.

Existía una recuperación equivalente, pero solo al arrancar el proceso.

Los tests usan SQLite real: lo que importa es el comportamiento de la consulta
y de la transición de estado.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.db_models import Base, NormalizedRCEvent


@pytest.fixture
def base(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/cola.db")
    Base.metadata.create_all(engine, tables=[NormalizedRCEvent.__table__])
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _evento(**kwargs):
    campos = dict(
        provider="protrack", status="pending", chassis_number="C1",
        latitude=1.0, longitude=2.0, speed=0, code="1",
        created_at=datetime.now(), date=datetime.now(),
    )
    campos.update(kwargs)
    return NormalizedRCEvent(**campos)


def _rescatar(db, umbral_seg):
    """Réplica de _recuperar_estancados_sync sobre la sesión de prueba."""
    limite = datetime.now() - timedelta(seconds=umbral_seg)
    ids = [
        f[0]
        for f in db.query(NormalizedRCEvent.id)
        .filter(
            NormalizedRCEvent.status == "processing",
            NormalizedRCEvent.updated_at.isnot(None),
            NormalizedRCEvent.updated_at < limite,
        )
        .all()
    ]
    if not ids:
        return 0
    db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id.in_(ids)).update(
        {"status": "pending"}, synchronize_session=False
    )
    db.commit()
    return len(ids)


# ═══════════════════════════════════════════════════════════════════
# Rescate
# ═══════════════════════════════════════════════════════════════════

def test_rescata_los_que_llevan_demasiado_tiempo_procesando(base):
    """
    REGRESIÓN: sin esto, un lote interrumpido esperaba al próximo reinicio del
    servicio. Con 2000 eventos por lote, eso son 2000 eventos que no llegan a RC.
    """
    viejo = datetime.now() - timedelta(minutes=30)
    base.add_all([_evento(status="processing", updated_at=viejo) for _ in range(5)])
    base.commit()

    assert _rescatar(base, 600) == 5
    assert base.query(NormalizedRCEvent).filter_by(status="pending").count() == 5
    assert base.query(NormalizedRCEvent).filter_by(status="processing").count() == 0


def test_no_toca_un_lote_que_sigue_en_vuelo(base):
    """
    Un envío grande puede tardar minutos. Reencolarlo mientras todavía está
    saliendo produciría envíos duplicados a RC.
    """
    reciente = datetime.now() - timedelta(seconds=30)
    base.add_all([_evento(status="processing", updated_at=reciente) for _ in range(5)])
    base.commit()

    assert _rescatar(base, 600) == 0
    assert base.query(NormalizedRCEvent).filter_by(status="processing").count() == 5


def test_no_toca_los_demas_estados(base):
    """Solo los atascados: los pendientes, enviados y fallidos no se tocan."""
    viejo = datetime.now() - timedelta(hours=2)
    base.add_all([
        _evento(status="pending", updated_at=viejo),
        _evento(status="sent", updated_at=viejo),
        _evento(status="failed", updated_at=viejo),
        _evento(status="retry", updated_at=viejo),
    ])
    base.commit()

    assert _rescatar(base, 600) == 0
    assert base.query(NormalizedRCEvent).count() == 4


def test_el_rescatado_vuelve_sin_espera_de_reintento(base):
    """
    Nunca llegó a intentarse contra RC, así que no corresponde imponerle un
    backoff: debe salir en el próximo ciclo.
    """
    viejo = datetime.now() - timedelta(minutes=30)
    base.add(_evento(status="processing", updated_at=viejo, next_retry_at=None))
    base.commit()

    _rescatar(base, 600)

    evento = base.query(NormalizedRCEvent).first()
    assert evento.status == "pending"
    assert evento.next_retry_at is None


def test_conserva_el_contador_de_intentos(base):
    """Un rescate no debe regalar intentos ni consumirlos."""
    viejo = datetime.now() - timedelta(minutes=30)
    base.add(_evento(status="processing", updated_at=viejo, retry_count=2))
    base.commit()

    _rescatar(base, 600)

    assert base.query(NormalizedRCEvent).first().retry_count == 2


def test_sin_marca_temporal_no_se_rescata(base):
    """
    Sin updated_at no se puede saber hace cuánto está en ese estado; rescatarlo
    a ciegas podría interrumpir un envío en curso.
    """
    base.add(_evento(status="processing", updated_at=None))
    base.commit()

    assert _rescatar(base, 600) == 0


def test_el_umbral_determina_que_se_rescata(base):
    viejo = datetime.now() - timedelta(minutes=20)
    base.add(_evento(status="processing", updated_at=viejo))
    base.commit()

    assert _rescatar(base, 3600) == 0    # umbral de 1 hora: todavía no
    assert _rescatar(base, 600) == 1     # umbral de 10 min: sí


# ═══════════════════════════════════════════════════════════════════
# updated_at debe reflejar el momento en que se tomó el lote
# ═══════════════════════════════════════════════════════════════════

def test_marcar_processing_actualiza_la_marca_temporal(base):
    """
    Todo el rescate se apoya en updated_at. Si el UPDATE masivo no disparara
    el onupdate, la marca quedaría vieja y el reaper reencolaría lotes que
    acaban de tomarse.
    """
    import time

    base.add(_evento(status="pending"))
    base.commit()
    evento_id = base.query(NormalizedRCEvent).first().id

    time.sleep(1.1)
    antes = datetime.now()

    base.query(NormalizedRCEvent).filter(NormalizedRCEvent.id == evento_id).update(
        {"status": "processing"}, synchronize_session=False
    )
    base.commit()
    base.expire_all()

    evento = base.query(NormalizedRCEvent).first()
    assert evento.updated_at is not None
    assert evento.updated_at >= antes - timedelta(seconds=2)


# ═══════════════════════════════════════════════════════════════════
# Integración con el worker
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_el_reaper_recorre_todas_las_integraciones():
    """
    Cada integración tiene su propia base: el rescate debe alcanzarlas a todas,
    no solo a la primera.
    """
    from unittest.mock import patch, MagicMock
    from app.worker.processor import _recuperar_eventos_atascados

    consultadas = []

    class ColaFalsa:
        def __init__(self, provider, env):
            self.provider, self.env = provider, env

        async def recuperar_estancados(self, provider, env, umbral):
            consultadas.append((provider, env))
            return 3

    with patch("app.core.queue_factory.QueueFactory.get_queue_service",
               side_effect=lambda p, e: ColaFalsa(p, e)):
        total = await _recuperar_eventos_atascados(
            [("protrack", "prod"), ("protrack", "test"), ("schmitz", "prod")]
        )

    assert total == 9
    assert consultadas == [("protrack", "prod"), ("protrack", "test"), ("schmitz", "prod")]


@pytest.mark.asyncio
async def test_un_fallo_en_una_integracion_no_frena_a_las_demas():
    """Si una base no responde, las otras igual deben rescatarse."""
    from unittest.mock import patch
    from app.worker.processor import _recuperar_eventos_atascados

    class ColaFalsa:
        def __init__(self, provider, env):
            self.provider, self.env = provider, env

        async def recuperar_estancados(self, provider, env, umbral):
            if provider == "protrack":
                raise RuntimeError("base bloqueada")
            return 5

    with patch("app.core.queue_factory.QueueFactory.get_queue_service",
               side_effect=lambda p, e: ColaFalsa(p, e)):
        total = await _recuperar_eventos_atascados([("protrack", "prod"), ("schmitz", "prod")])

    assert total == 5


@pytest.mark.asyncio
async def test_un_backend_sin_soporte_no_rompe_el_watchdog():
    """Una cola que no implemente el rescate no debe tumbar el ciclo."""
    from unittest.mock import patch
    from app.worker.processor import _recuperar_eventos_atascados

    class ColaSinSoporte:
        pass

    with patch("app.core.queue_factory.QueueFactory.get_queue_service",
               return_value=ColaSinSoporte()):
        assert await _recuperar_eventos_atascados([("protrack", "prod")]) == 0


def test_el_umbral_por_defecto_supera_el_despacho_mas_lento():
    """
    Un lote de 2000 eventos son 40 sub-lotes de a 4 en paralelo, con hasta 30 s
    de timeout: unos 5 minutos en el peor caso.
    """
    from app.worker.processor import obtener_parametros_rc

    peor_caso_seg = (2000 / 50 / 4) * 30
    assert obtener_parametros_rc()["recuperacion_umbral_seg"] > peor_caso_seg


def test_el_reaper_no_corre_en_cada_ciclo_del_watchdog():
    """
    El watchdog gira cada 15 s; consultar todas las bases con esa frecuencia
    sería un costo innecesario para algo que ocurre muy de vez en cuando.
    """
    from app.worker.processor import _INTERVALO_RECUPERACION

    assert _INTERVALO_RECUPERACION >= 60
