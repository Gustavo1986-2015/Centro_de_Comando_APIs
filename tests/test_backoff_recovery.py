"""
Tests de la liberación del backoff cuando Recurso Confiable se recupera.

Escenario que se resuelve: RC se cae diez minutos. A 40 msg/s se acumulan
decenas de miles de eventos, cada uno con su espera de reintento (hasta siete
minutos en el cuarto intento). Cuando RC vuelve, esos eventos siguen esperando
aunque el destino ya responda.

La liberación es progresiva a propósito: soltar todo de golpe volvería a
saturar a RC apenas se recupera.

Estos tests usan una base SQLite real, no mocks: lo que se verifica es el
comportamiento de la consulta y de la transición de estado.
"""
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.db_models import Base, NormalizedRCEvent
from app.worker.processor import CircuitBreaker


# ═══════════════════════════════════════════════════════════════════
# Marca de recuperación en el circuit breaker
# ═══════════════════════════════════════════════════════════════════

def test_sin_caidas_no_hay_marca_de_recuperacion():
    """Un breaker que nunca se degradó no debe disparar liberaciones."""
    cb = CircuitBreaker()
    cb.record_success()

    assert cb.recovery_timestamp() == 0.0


def test_la_recuperacion_deja_marca_temporal():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1)

    cb.record_failure()
    cb.record_failure()                      # circuito abierto
    assert cb.recovery_timestamp() == 0.0

    antes = time.time()
    cb.record_success()                      # RC vuelve
    marca = cb.recovery_timestamp()

    assert marca >= antes


def test_exitos_consecutivos_no_mueven_la_marca():
    """
    Solo la transición desde un estado degradado cuenta como recuperación: si
    cada éxito moviera la marca, se liberaría el backoff continuamente.
    """
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()

    primera = cb.recovery_timestamp()
    time.sleep(0.01)
    cb.record_success()
    cb.record_success()

    assert cb.recovery_timestamp() == primera


def test_una_nueva_caida_genera_una_nueva_marca():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1)

    cb.record_failure(); cb.record_failure(); cb.record_success()
    primera = cb.recovery_timestamp()

    time.sleep(0.01)
    cb.record_failure(); cb.record_failure(); cb.record_success()

    assert cb.recovery_timestamp() > primera


# ═══════════════════════════════════════════════════════════════════
# Liberación sobre base real
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def base(tmp_path):
    """Base SQLite real con la tabla de eventos."""
    engine = create_engine(f"sqlite:///{tmp_path}/cola.db")
    Base.metadata.create_all(engine, tables=[NormalizedRCEvent.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()


def _liberar(db, limite):
    """
    Réplica de _liberar_backoff_sync sobre la sesión de prueba.

    La función real abre su propia sesión mediante session_context, atada a la
    estructura de directorios del proyecto; acá interesa validar la consulta.
    """
    ahora = datetime.now()
    ids = [
        f[0]
        for f in db.query(NormalizedRCEvent.id)
        .filter(
            NormalizedRCEvent.status == "pending",
            NormalizedRCEvent.next_retry_at.isnot(None),
            NormalizedRCEvent.next_retry_at > ahora,
        )
        .order_by(NormalizedRCEvent.id.asc())
        .limit(limite)
        .all()
    ]
    if not ids:
        return 0
    db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id.in_(ids)).update(
        {"next_retry_at": None}, synchronize_session=False
    )
    db.commit()
    return len(ids)


def _evento(**kwargs):
    base_kwargs = dict(
        provider="protrack", status="pending", chassis_number="C1",
        latitude=1.0, longitude=2.0, speed=0, code="1",
        created_at=datetime.now(), date=datetime.now(),
    )
    base_kwargs.update(kwargs)
    return NormalizedRCEvent(**base_kwargs)


def test_libera_los_que_esperan_reintento(base):
    """El caso central: eventos con espera futura salen en el próximo ciclo."""
    futuro = datetime.now() + timedelta(minutes=7)
    base.add_all([_evento(next_retry_at=futuro, retry_count=3) for _ in range(10)])
    base.commit()

    liberados = _liberar(base, 500)

    assert liberados == 10
    pendientes_con_espera = base.query(NormalizedRCEvent).filter(
        NormalizedRCEvent.next_retry_at.isnot(None)
    ).count()
    assert pendientes_con_espera == 0


def test_no_toca_los_que_ya_vencieron(base):
    """Los que ya podían salir no necesitan intervención."""
    pasado = datetime.now() - timedelta(minutes=5)
    base.add_all([_evento(next_retry_at=pasado, retry_count=1) for _ in range(5)])
    base.commit()

    assert _liberar(base, 500) == 0


def test_no_toca_eventos_nuevos(base):
    """Un evento recién encolado no tiene espera y no debe contarse."""
    base.add_all([_evento(next_retry_at=None) for _ in range(5)])
    base.commit()

    assert _liberar(base, 500) == 0


def test_no_toca_los_fallidos_ni_los_enviados(base):
    """
    La liberación es para reintentar, no para revivir eventos ya cerrados:
    resucitar un failed es otra decisión, con otras consecuencias.
    """
    futuro = datetime.now() + timedelta(minutes=5)
    base.add_all([
        _evento(status="failed", next_retry_at=futuro),
        _evento(status="sent", next_retry_at=futuro),
        _evento(status="processing", next_retry_at=futuro),
    ])
    base.commit()

    assert _liberar(base, 500) == 0


def test_conserva_el_contador_de_intentos(base):
    """
    Adelantar la salida no debe regalar intentos: un evento que RC rechaza
    sistemáticamente tiene que llegar igual a su tope y dejar de insistir.
    """
    futuro = datetime.now() + timedelta(minutes=5)
    base.add(_evento(next_retry_at=futuro, retry_count=3))
    base.commit()

    _liberar(base, 500)

    evento = base.query(NormalizedRCEvent).first()
    assert evento.retry_count == 3
    assert evento.next_retry_at is None


# ═══════════════════════════════════════════════════════════════════
# Liberación progresiva
# ═══════════════════════════════════════════════════════════════════

def test_respeta_el_tamano_de_tanda(base):
    """
    REGRESIÓN del diseño: soltar decenas de miles de eventos de una vez
    volvería a saturar a RC justo cuando se está recuperando.
    """
    futuro = datetime.now() + timedelta(minutes=5)
    base.add_all([_evento(next_retry_at=futuro) for _ in range(1200)])
    base.commit()

    assert _liberar(base, 500) == 500

    restantes = base.query(NormalizedRCEvent).filter(
        NormalizedRCEvent.next_retry_at.isnot(None)
    ).count()
    assert restantes == 700


def test_los_ciclos_sucesivos_agotan_la_cola(base):
    """Cada ciclo del worker libera una tanda hasta que no queda ninguno."""
    futuro = datetime.now() + timedelta(minutes=5)
    base.add_all([_evento(next_retry_at=futuro) for _ in range(1200)])
    base.commit()

    tandas = []
    while True:
        n = _liberar(base, 500)
        if not n:
            break
        tandas.append(n)

    assert tandas == [500, 500, 200]
    assert base.query(NormalizedRCEvent).filter(
        NormalizedRCEvent.next_retry_at.isnot(None)
    ).count() == 0


def test_libera_primero_los_mas_antiguos(base):
    """Los eventos más viejos son los que más tiempo llevan esperando."""
    futuro = datetime.now() + timedelta(minutes=5)
    base.add_all([_evento(next_retry_at=futuro, chassis_number=f"C{i}") for i in range(10)])
    base.commit()

    _liberar(base, 4)

    liberados = base.query(NormalizedRCEvent).filter(
        NormalizedRCEvent.next_retry_at.is_(None)
    ).order_by(NormalizedRCEvent.id).all()

    assert [e.chassis_number for e in liberados] == ["C0", "C1", "C2", "C3"]


# ═══════════════════════════════════════════════════════════════════
# Control de repetición
# ═══════════════════════════════════════════════════════════════════

def test_una_recuperacion_ya_procesada_no_vuelve_a_disparar():
    """Sin este control, cada ciclo del worker liberaría de nuevo."""
    from app.worker.processor import _recuperacion_procesada

    _recuperacion_procesada.clear()
    marca = time.time()
    _recuperacion_procesada["protrack_prod"] = marca

    assert _recuperacion_procesada.get("protrack_prod", 0.0) >= marca
    _recuperacion_procesada.clear()


def test_cada_integracion_procesa_la_recuperacion_por_separado():
    """
    El breaker es único porque RC es destino único, pero cada integración tiene
    su propia base y debe liberar la suya.
    """
    from app.worker.processor import _recuperacion_procesada

    _recuperacion_procesada.clear()
    marca = time.time()
    _recuperacion_procesada["protrack_prod"] = marca

    assert _recuperacion_procesada.get("schmitz_prod", 0.0) < marca
    _recuperacion_procesada.clear()


def test_el_tamano_de_tanda_es_razonable():
    """
    Suficiente para desagotar rápido, moderado para no tumbar a RC recién
    recuperado.
    """
    from app.worker.processor import obtener_parametros_rc

    assert 100 <= obtener_parametros_rc()["liberacion_tanda"] <= 5000


def test_los_parametros_caen_a_valores_por_defecto_si_falla_la_base(monkeypatch):
    """
    La política de reintentos no debe depender de que la configuración
    responda: si la base no está disponible, se sigue operando.
    """
    from app.worker import processor

    processor.invalidar_parametros_rc()

    def _falla(*args, **kwargs):
        raise RuntimeError("base no disponible")

    monkeypatch.setattr(processor, "get_session", _falla)
    valores = processor.obtener_parametros_rc()

    assert valores["liberacion_tanda"] > 0
    assert valores["max_reintentos"] > 0
    assert valores["fallos_circuito"] > 0
    processor.invalidar_parametros_rc()


def test_los_parametros_se_cachean():
    """Se leen en cada ciclo del worker: consultar la base cada vez sobra."""
    from app.worker.processor import obtener_parametros_rc, _params_cache, invalidar_parametros_rc

    invalidar_parametros_rc()
    primera = obtener_parametros_rc()
    marca = _params_cache["ts"]

    segunda = obtener_parametros_rc()

    assert segunda == primera
    assert _params_cache["ts"] == marca   # no volvió a leer


# ═══════════════════════════════════════════════════════════════════
# Formato de fecha hacia RC
# ═══════════════════════════════════════════════════════════════════

def test_el_comentario_del_formato_de_fecha_esta_al_dia():
    """
    El comentario decía que se agregaba la Z cuando el código ya no lo hace.
    Un comentario que contradice al código induce al error siguiente.
    """
    import inspect
    from app.services import rc_soap

    fuente = inspect.getsource(rc_soap)
    assert "añadimos la Z" not in fuente
    assert "SIN sufijo Z" in fuente


# ═══════════════════════════════════════════════════════════════════
# Configuración desde el panel
# ═══════════════════════════════════════════════════════════════════

def test_los_rangos_impiden_configuraciones_que_romperian_la_operacion():
    """
    Los límites no son arbitrarios: una tanda enorme satura a RC al recuperarse,
    cero reintentos descarta eventos ante el primer tropiezo de red, y un umbral
    de circuito muy alto hace que el hub insista sobre un destino caído.
    """
    import base64
    import os
    from fastapi.testclient import TestClient

    os.environ["DASHBOARD_USER"] = "t"
    os.environ["DASHBOARD_PASSWORD"] = "clave_de_prueba_larga"
    from main import app

    auth = {"Authorization": "Basic " + base64.b64encode(b"t:clave_de_prueba_larga").decode()}
    cliente = TestClient(app)

    validos = {"rc_liberacion_tanda": 500, "rc_max_reintentos": 4, "rc_fallos_circuito": 5}

    invalidos = [
        ({**validos, "rc_liberacion_tanda": 99999}, "tanda desmedida"),
        ({**validos, "rc_liberacion_tanda": 1}, "tanda demasiado chica"),
        ({**validos, "rc_max_reintentos": 0}, "sin reintentos"),
        ({**validos, "rc_max_reintentos": 99}, "reintentos excesivos"),
        ({**validos, "rc_fallos_circuito": 1}, "circuito hipersensible"),
        ({**validos, "rc_fallos_circuito": 500}, "circuito que nunca abre"),
    ]

    for payload, descripcion in invalidos:
        r = cliente.put("/api/config/rc-behavior", headers=auth, json=payload)
        assert r.status_code == 400, f"Se aceptó una configuración inválida: {descripcion}"

    # Restaurar valores operativos
    cliente.put("/api/config/rc-behavior", headers=auth, json=validos)


def test_la_configuracion_requiere_autenticacion():
    from fastapi.testclient import TestClient
    from main import app

    cliente = TestClient(app)
    assert cliente.get("/api/config/rc-behavior").status_code == 401
    assert cliente.put("/api/config/rc-behavior", json={}).status_code in (401, 422)


# ═══════════════════════════════════════════════════════════════════
# Consolidación de categorías en el log del lote
# ═══════════════════════════════════════════════════════════════════
# Este bloque existe porque la consolidación trataba cada elemento como si
# fuera un diccionario, cuando en realidad es la tupla que devuelve el
# sub-lote. La condición nunca se cumplía y el log mostraba "sin_respuestas"
# incluso con envíos exitosos: el dato estaba, pero no llegaba al operador.

def _consolidar(all_metrics):
    """Réplica de la consolidación que arma la línea `| RC: ...` del log."""
    from collections import defaultdict

    categorias = defaultdict(int)
    for m in all_metrics:
        if isinstance(m, Exception):
            continue
        for clave, valor in (m[0].get("categorias") or {}).items():
            categorias[clave] += valor
    return dict(categorias)


def test_las_categorias_se_consolidan_desde_la_tupla_del_sublote():
    """
    REGRESIÓN: `all_metrics` contiene tuplas (metrics, retry, fail, sent), no
    diccionarios. Tratarlas como dict hacía que el contador quedara siempre
    vacío.
    """
    all_metrics = [
        ({"sent": 29, "categorias": {"SUCCESS": 29}}, [], [], []),
        ({"sent": 21, "categorias": {"SUCCESS": 21}}, [], [], []),
    ]

    assert _consolidar(all_metrics) == {"SUCCESS": 50}


def test_la_consolidacion_suma_categorias_distintas():
    all_metrics = [
        ({"categorias": {"SUCCESS": 25, "TRANSPORT_retry": 3}}, [], [], []),
        ({"categorias": {"SUCCESS": 20, "BUSINESS_failed": 2}}, [], [], []),
    ]

    assert _consolidar(all_metrics) == {
        "SUCCESS": 45, "TRANSPORT_retry": 3, "BUSINESS_failed": 2,
    }


def test_un_sublote_fallido_no_rompe_la_consolidacion():
    """Si un sub-lote lanza excepción, los demás igual deben reportarse."""
    all_metrics = [
        ({"categorias": {"SUCCESS": 29}}, [], [], []),
        RuntimeError("sub-lote fallido"),
        ({"categorias": {"SUCCESS": 21}}, [], [], []),
    ]

    assert _consolidar(all_metrics) == {"SUCCESS": 50}


def test_sin_categorias_no_falla():
    """Un sub-lote que retornó temprano no trae el campo."""
    assert _consolidar([({"sent": 5}, [], [], [])]) == {}


def test_la_consolidacion_del_codigo_real_desempaqueta_la_tupla():
    """
    Verifica sobre el código fuente que no se volvió a la comparación con dict,
    que es la forma en que el bug pasó desapercibido con la suite en verde.
    """
    import inspect
    from app.worker import processor

    fuente = inspect.getsource(processor.process_provider_events)
    bloque = fuente[fuente.find("categorias_lote = defaultdict"):]
    bloque = bloque[:bloque.find("detalle_rc")]

    assert "isinstance(m, dict)" not in bloque, (
        "all_metrics contiene tuplas, no diccionarios"
    )
    assert "m[0]" in bloque or "metrics_sublote" in bloque
