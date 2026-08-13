"""
Tests de los hallazgos A5 y A6 de la auditoría: costo de las estadísticas y de
la purga bajo carga sostenida.

Ambos escalaban con el tamaño de la tabla, así que el problema solo aparece
tras horas de tráfico — justo durante una certificación de 24 horas.
"""
import time
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.worker.processor import (
    registrar_latencias,
    obtener_promedios,
    _LATENCY_WINDOW,
    _LATENCY_WINDOW_SEC,
    _PURGE_CHUNK_SIZE,
)


@pytest.fixture(autouse=True)
def _limpiar_ventana():
    _LATENCY_WINDOW.clear()
    yield
    _LATENCY_WINDOW.clear()


# ═══════════════════════════════════════════════════════════════════
# A6 — Las medias ya no recorren la tabla del día
# ═══════════════════════════════════════════════════════════════════

def test_las_medias_salen_de_la_ventana_sin_tocar_la_base():
    registrar_latencias("schmitz", "prod", [
        {"rc": 0.2, "hub": 0.8, "transmission": 2.0},
        {"rc": 0.4, "hub": 1.2, "transmission": 4.0},
    ])
    p = obtener_promedios("schmitz", "prod")

    assert p["avg_rc"] == pytest.approx(0.3)
    assert p["avg_hub"] == pytest.approx(1.0)
    assert p["avg_transmission"] == pytest.approx(3.0)
    assert p["muestras"] == 2


def test_el_costo_no_crece_con_el_volumen_acumulado():
    """
    REGRESIÓN: el cálculo agregaba toda la tabla del día cada 5 segundos.
    Medido en 31 ms con 15.000 filas, proyecta a varios segundos con los
    millones de una jornada — más de lo que dura el propio intervalo.
    """
    muestras = [{"rc": 0.2, "hub": 0.8, "transmission": 2.0}] * 1000
    for _ in range(144):                      # ~1 hora a 40 msg/s
        registrar_latencias("schmitz", "prod", muestras)

    inicio = time.perf_counter()
    obtener_promedios("schmitz", "prod")
    transcurrido = (time.perf_counter() - inicio) * 1000

    assert transcurrido < 100, f"El cálculo tardó {transcurrido:.0f} ms"


def test_la_memoria_esta_acotada():
    """La ventana no debe crecer indefinidamente con el tráfico."""
    muestras = [{"rc": 0.2, "hub": 0.8, "transmission": 2.0}] * 1000
    for _ in range(100):
        registrar_latencias("schmitz", "prod", muestras)

    assert len(_LATENCY_WINDOW["schmitz|prod"]) <= 20000


def test_las_muestras_viejas_salen_de_la_ventana():
    from collections import deque

    clave = "schmitz|prod"
    viejo = time.time() - _LATENCY_WINDOW_SEC - 60
    _LATENCY_WINDOW[clave] = deque([(viejo, {"rc": 9.9, "hub": 9.9, "transmission": 9.9})])

    registrar_latencias("schmitz", "prod", [{"rc": 0.1, "hub": 0.1, "transmission": 0.1}])
    p = obtener_promedios("schmitz", "prod")

    assert p["muestras"] == 1
    assert p["avg_rc"] == pytest.approx(0.1)   # la muestra vieja no pesa


def test_cada_integracion_tiene_su_ventana():
    registrar_latencias("schmitz", "prod", [{"rc": 1.0, "hub": 1.0, "transmission": 1.0}])
    registrar_latencias("protrack", "prod", [{"rc": 5.0, "hub": 5.0, "transmission": 5.0}])

    assert obtener_promedios("schmitz", "prod")["avg_rc"] == pytest.approx(1.0)
    assert obtener_promedios("protrack", "prod")["avg_rc"] == pytest.approx(5.0)


def test_entornos_separados():
    registrar_latencias("schmitz", "prod", [{"rc": 1.0, "hub": 1.0, "transmission": 1.0}])
    assert obtener_promedios("schmitz", "test")["muestras"] == 0


def test_sin_muestras_devuelve_nulos_sin_fallar():
    p = obtener_promedios("inexistente", "prod")
    assert p == {"avg_rc": None, "avg_hub": None, "avg_transmission": None, "muestras": 0}


def test_campos_ausentes_no_rompen_la_media():
    """Un evento sin fecha del dispositivo no debe invalidar el resto."""
    registrar_latencias("schmitz", "prod", [
        {"rc": 0.2, "hub": 0.8, "transmission": None},
        {"rc": 0.4, "hub": 1.2, "transmission": 4.0},
    ])
    p = obtener_promedios("schmitz", "prod")

    assert p["avg_rc"] == pytest.approx(0.3)
    assert p["avg_transmission"] == pytest.approx(4.0)   # promedia solo el disponible


# ═══════════════════════════════════════════════════════════════════
# A5 — Purga por tandas e índice
# ═══════════════════════════════════════════════════════════════════

def test_el_tamano_de_tanda_es_razonable():
    """
    Suficientemente grande para no multiplicar los commits, suficientemente
    chico para que la base quede libre entre tandas mientras entra tráfico.
    """
    assert 1000 <= _PURGE_CHUNK_SIZE <= 20000


def test_el_indice_esta_declarado_en_el_modelo():
    """
    REGRESIÓN: la purga filtra por (status, created_at) y no había índice sobre
    esas columnas. El de status no discrimina porque casi todas las filas
    quedan en 'sent'.
    """
    from app.models.db_models import NormalizedRCEvent

    indices = {i.name for i in NormalizedRCEvent.__table__.indexes}
    assert "idx_status_created" in indices


def test_el_indice_acelera_el_filtro_de_purga(tmp_path):
    """Comparación directa sobre un volumen equivalente a ~1 hora de tráfico."""
    ruta = str(tmp_path / "bench.db")
    conn = sqlite3.connect(ruta)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE normalized_rc_events (
            id INTEGER PRIMARY KEY, status TEXT, created_at TIMESTAMP, raw_data TEXT
        )
    """)
    cur.execute("CREATE INDEX idx_status ON normalized_rc_events(status)")

    ayer = (datetime.now() - timedelta(days=1)).isoformat()
    cur.executemany(
        "INSERT INTO normalized_rc_events (status, created_at, raw_data) VALUES (?,?,?)",
        [("sent", ayer, "x" * 200) for _ in range(50000)],
    )
    conn.commit()

    hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    consulta = (
        "SELECT COUNT(*) FROM normalized_rc_events "
        "WHERE status IN ('sent','failed') AND created_at < ?"
    )

    inicio = time.perf_counter()
    cur.execute(consulta, (hoy,))
    cur.fetchone()
    sin_indice = time.perf_counter() - inicio

    cur.execute("CREATE INDEX idx_status_created ON normalized_rc_events(status, created_at)")
    conn.commit()

    inicio = time.perf_counter()
    cur.execute(consulta, (hoy,))
    cur.fetchone()
    con_indice = time.perf_counter() - inicio
    conn.close()

    assert con_indice < sin_indice, (
        f"El índice no mejoró: {sin_indice*1000:.0f} ms -> {con_indice*1000:.0f} ms"
    )


def test_la_purga_borra_en_tandas_con_commits_intermedios(tmp_path):
    """
    REGRESIÓN: un DELETE único de millones de filas mantiene tomado el lock de
    escritura de SQLite durante todo el recorrido, frenando la ingesta que
    sigue entrando.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.db_models import NormalizedRCEvent, Base
    from app.worker.processor import _delete_purged_sync

    engine = create_engine(f"sqlite:///{tmp_path}/purga.db")
    Base.metadata.create_all(engine, tables=[NormalizedRCEvent.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()

    ayer = datetime.now() - timedelta(days=1)
    hoy = datetime.now()

    # Más de una tanda de purgables, más pendientes y de hoy que no se tocan
    db.add_all([
        NormalizedRCEvent(provider="p", status="sent", created_at=ayer, chassis_number=f"A{i}")
        for i in range(_PURGE_CHUNK_SIZE + 500)
    ])
    db.add_all([
        NormalizedRCEvent(provider="p", status="pending", created_at=ayer, chassis_number="PEND"),
        NormalizedRCEvent(provider="p", status="processing", created_at=ayer, chassis_number="PROC"),
        NormalizedRCEvent(provider="p", status="sent", created_at=hoy, chassis_number="HOY"),
    ])
    db.commit()

    inicio_dia = hoy.replace(hour=0, minute=0, second=0, microsecond=0)
    borrados = _delete_purged_sync(db, inicio_dia)

    assert borrados == _PURGE_CHUNK_SIZE + 500

    restantes = {e.status for e in db.query(NormalizedRCEvent).all()}
    assert restantes == {"pending", "processing", "sent"}   # el 'sent' de hoy sobrevive
    assert db.query(NormalizedRCEvent).count() == 3
    db.close()


def test_la_purga_nunca_toca_pendientes_ni_en_proceso(tmp_path):
    """Aunque sean de días anteriores: no fueron despachados todavía."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.db_models import NormalizedRCEvent, Base
    from app.worker.processor import _delete_purged_sync

    engine = create_engine(f"sqlite:///{tmp_path}/purga2.db")
    Base.metadata.create_all(engine, tables=[NormalizedRCEvent.__table__])
    db = sessionmaker(bind=engine)()

    ayer = datetime.now() - timedelta(days=1)
    db.add_all([
        NormalizedRCEvent(provider="p", status="pending", created_at=ayer, chassis_number="A"),
        NormalizedRCEvent(provider="p", status="processing", created_at=ayer, chassis_number="B"),
        NormalizedRCEvent(provider="p", status="retry", created_at=ayer, chassis_number="C"),
    ])
    db.commit()

    borrados = _delete_purged_sync(
        db, datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    )

    assert borrados == 0
    assert db.query(NormalizedRCEvent).count() == 3
    db.close()


# ═══════════════════════════════════════════════════════════════════
# Retención por antigüedad
# ═══════════════════════════════════════════════════════════════════
# La base es un espacio de tránsito: recibe, despacha y libera. Antes se
# purgaba lo anterior al inicio del día calendario, así que durante una jornada
# completa de tráfico no se eliminaba nada y la tabla crecía sin freno hasta la
# medianoche siguiente. A 40 msg/s eso son varios gigabytes.

def test_la_purga_no_depende_del_dia_calendario():
    """
    REGRESIÓN: con el corte en el inicio del día, una corrida de 24 horas no
    purgaba un solo evento.
    """
    import inspect
    from app.worker import processor

    fuente = inspect.getsource(processor.purge_provider_events)
    assert "today_start" not in fuente, (
        "La purga sigue usando el inicio del día en lugar de la antigüedad"
    )
    assert "retencion_horas" in fuente


def test_la_retencion_es_configurable():
    from app.worker.processor import obtener_parametros_rc

    valor = obtener_parametros_rc()["retencion_horas"]
    assert 1 <= valor <= 72


def test_solo_se_purga_lo_ya_despachado(tmp_path):
    """
    Un evento pendiente o en proceso no salió todavía: purgarlo sería perderlo.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.db_models import Base, NormalizedRCEvent
    from app.worker.processor import _delete_purged_sync

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine, tables=[NormalizedRCEvent.__table__])
    db = sessionmaker(bind=engine)()

    viejo = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
    for estado in ("sent", "failed", "pending", "processing", "retry"):
        db.add(NormalizedRCEvent(
            provider="p", status=estado, chassis_number=estado,
            latitude=1.0, longitude=2.0, speed=0, code="1",
            created_at=viejo, date=viejo,
        ))
    db.commit()

    corte = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    borrados = _delete_purged_sync(db, corte)

    assert borrados == 2      # solo sent y failed
    restantes = {e.status for e in db.query(NormalizedRCEvent).all()}
    assert restantes == {"pending", "processing", "retry"}
    db.close()


def test_un_evento_reciente_no_se_purga(tmp_path):
    """La ventana debe respetarse: lo despachado hace un rato sigue visible."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.db_models import Base, NormalizedRCEvent
    from app.worker.processor import _delete_purged_sync

    engine = create_engine(f"sqlite:///{tmp_path}/t2.db")
    Base.metadata.create_all(engine, tables=[NormalizedRCEvent.__table__])
    db = sessionmaker(bind=engine)()

    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(NormalizedRCEvent(
        provider="p", status="sent", chassis_number="RECIENTE",
        latitude=1.0, longitude=2.0, speed=0, code="1",
        created_at=ahora - timedelta(minutes=30), date=ahora,
    ))
    db.commit()

    corte = ahora - timedelta(hours=2)
    assert _delete_purged_sync(db, corte) == 0
    db.close()


def test_la_compactacion_tolera_la_base_en_uso(tmp_path):
    """
    REGRESIÓN: la conexión del VACUUM se abría sin timeout y fallaba de
    inmediato con "database is locked". Con tráfico continuo, el momento de
    exclusividad no llega nunca y la purga manual nunca compactaba.
    """
    import inspect
    from app.api.routers import dashboard

    fuente = inspect.getsource(dashboard._vacuum_db)
    assert "timeout=30" in fuente, "La conexión del VACUUM necesita timeout"
    assert "busy_timeout" in fuente
    assert "incremental_vacuum" in fuente, (
        "Sin alternativa incremental, una base ocupada nunca se compacta"
    )
