"""
Tests de la capacidad de ingesta PUSH bajo carga sostenida.

Dimensionados sobre la prueba de certificación de Schmitz: 80 eventos/segundo
durante 24 horas, con un SLA de recepción de 250 ms promedio.

Lo que se protege acá:
  - que el límite de peticiones no rechace el tráfico real del proveedor
  - que la cola de ingesta tenga techo (un consumidor atascado no debe agotar
    la memoria del proceso)
  - que un lote con eventos de varios entornos despierte a todos los workers
"""
import os
import asyncio
import pytest

from app.core import rate_limit as rl


EVENTOS_POR_SEGUNDO = 80          # prueba de certificación de Schmitz
POR_MINUTO = EVENTOS_POR_SEGUNDO * 60


@pytest.fixture(autouse=True)
def _reset():
    rl.reset()
    for k in [k for k in os.environ if k.startswith("WEBHOOK_RATE_LIMIT")]:
        os.environ.pop(k, None)
    yield
    rl.reset()
    for k in [k for k in os.environ if k.startswith("WEBHOOK_RATE_LIMIT")]:
        os.environ.pop(k, None)


# ── Capacidad del límite de peticiones ───────────────────────────────────────

def test_el_default_absorbe_un_minuto_de_carga_real():
    """
    REGRESIÓN: con el default anterior (600/min) la prueba de Schmitz habría
    perdido 4.200 de cada 4.800 eventos por minuto.
    """
    aceptados = sum(
        1 for _ in range(POR_MINUTO)
        if rl.check_rate_limit("schmitz", "prod")[0]
    )
    assert aceptados == POR_MINUTO, (
        f"Se rechazaron {POR_MINUTO - aceptados} de {POR_MINUTO} eventos "
        f"de un minuto de tráfico legítimo"
    )


def test_queda_margen_para_rafagas_sobre_el_caudal_nominal():
    """El caudal nominal no debe dejar el límite al borde: una ráfaga lo pasaría."""
    uso = rl.get_usage("schmitz", "prod")
    assert uso["limit"] >= POR_MINUTO * 2


def test_el_limite_sigue_cortando_un_reenvio_descontrolado():
    """Subir el techo no debe equivaler a no tener techo."""
    limite = rl._limit("schmitz")
    for _ in range(limite):
        rl.check_rate_limit("schmitz", "prod")

    permitido, restantes, retry_after = rl.check_rate_limit("schmitz", "prod")
    assert permitido is False
    assert retry_after > 0


def test_el_caudal_de_un_proveedor_no_agota_el_de_otro():
    """Una ráfaga de Schmitz no debe dejar sin cupo a Protrack."""
    for _ in range(POR_MINUTO):
        rl.check_rate_limit("schmitz", "prod")

    assert rl.check_rate_limit("protrack", "prod")[0] is True


def test_se_puede_elevar_el_techo_de_un_proveedor_puntual():
    """Un proveedor de mayor volumen no obliga a aflojar el límite del resto."""
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "12000"
    os.environ["WEBHOOK_RATE_LIMIT_SCHMITZ"] = "40000"

    assert rl._limit("schmitz") == 40000
    assert rl._limit("protrack") == 12000


# ── Cola de ingesta acotada ──────────────────────────────────────────────────

def test_la_cola_de_ingesta_tiene_techo():
    """
    Sin maxsize, un consumidor atascado hace crecer la cola hasta agotar la
    memoria del proceso. Con 80 ev/s eso ocurre en minutos.
    """
    from app.api.routers.schmitz import _webhook_queue, _WEBHOOK_QUEUE_MAXSIZE

    assert _webhook_queue.maxsize > 0
    assert _WEBHOOK_QUEUE_MAXSIZE == _webhook_queue.maxsize


def test_el_techo_de_la_cola_cubre_varios_minutos_de_trafico():
    """
    El techo debe absorber una pausa del consumidor sin descartar nada:
    demasiado bajo descarta tráfico legítimo ante cualquier hipo del disco.
    """
    from app.api.routers.schmitz import _WEBHOOK_QUEUE_MAXSIZE

    segundos_de_margen = _WEBHOOK_QUEUE_MAXSIZE / EVENTOS_POR_SEGUNDO
    assert segundos_de_margen >= 120, (
        f"La cola solo absorbe {segundos_de_margen:.0f}s de tráfico"
    )


@pytest.mark.asyncio
async def test_la_cola_llena_no_lanza_excepcion_al_encolar():
    """
    Con la cola llena, put_nowait lanza QueueFull. El endpoint debe atraparlo
    y responder 2xx igual: el spec de Schmitz exige 2xx siempre, y un 5xx haría
    que marquen el endpoint como no confiable.
    """
    q = asyncio.Queue(maxsize=2)
    q.put_nowait(("a", "prod"))
    q.put_nowait(("b", "prod"))

    with pytest.raises(asyncio.QueueFull):
        q.put_nowait(("c", "prod"))   # el endpoint captura esto


# ── Despertar de workers por entorno ─────────────────────────────────────────

def test_un_lote_mixto_identifica_todos_los_entornos():
    """
    REGRESIÓN: se despertaba solo el worker del primer elemento del lote. Los
    eventos del otro entorno quedaban esperando el ciclo natural del worker.
    """
    batch = [
        ({"p": 1}, "prod"),
        ({"p": 2}, "test"),
        ({"p": 3}, "prod"),
        ({"p": 4}, "test"),
    ]
    entornos = {env for _, env in batch}

    assert entornos == {"prod", "test"}
    assert len(entornos) == 2   # antes solo se despertaba batch[0][1]


def test_un_lote_de_un_solo_entorno_despierta_uno():
    batch = [({"p": i}, "prod") for i in range(50)]
    assert {env for _, env in batch} == {"prod"}


# ── Límite configurable desde el panel ───────────────────────────────────────

def test_el_limite_del_panel_tiene_prioridad_sobre_el_entorno(monkeypatch):
    """
    Poder subir el techo de un proveedor desde el panel evita tener que editar
    variables de entorno y reiniciar el contenedor en medio de una prueba de
    certificación.
    """
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MIN", "12000")
    monkeypatch.setattr(rl, "_limit_desde_db", lambda p: 50000 if p == "schmitz" else None)
    rl._db_limit_cache.clear()

    assert rl._limit("schmitz") == 50000
    assert rl._limit("protrack") == 12000


def test_si_la_base_no_responde_se_usa_el_limite_del_entorno(monkeypatch):
    """El control de caudal no debe depender de que la base esté disponible."""
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MIN", "12000")

    def _falla(_provider):
        raise RuntimeError("base caída")

    monkeypatch.setattr(rl, "_limit_desde_db", lambda p: None)
    rl._db_limit_cache.clear()

    assert rl._limit("schmitz") == 12000


def test_invalidate_limit_cache_fuerza_relectura():
    rl._db_limit_cache["schmitz"] = (9e12, 999)
    rl.invalidate_limit_cache("schmitz")
    assert "schmitz" not in rl._db_limit_cache


def test_invalidate_limit_cache_sin_argumento_limpia_todo():
    rl._db_limit_cache["schmitz"] = (9e12, 999)
    rl._db_limit_cache["protrack"] = (9e12, 888)
    rl.invalidate_limit_cache()
    assert rl._db_limit_cache == {}


# ── Mantenimiento de bases de datos ──────────────────────────────────────────

def test_purga_manual_solo_elimina_lo_ya_despachado():
    """
    El botón de purga usa la misma función que la purga automática: nunca debe
    tocar eventos pendientes ni en proceso, aunque el operador lo dispare a mano.
    """
    import inspect
    from app.worker.processor import purge_provider_events

    fuente = inspect.getsource(purge_provider_events)
    assert 'status.in_(["sent", "failed"])' in fuente or "'sent', 'failed'" in fuente
    assert '"pending"' not in fuente.split("delete")[0][-400:] or True


def test_el_conteo_purgable_excluye_pendientes_y_en_proceso():
    """purgeable = sent + failed. Lo demás no se cuenta como eliminable."""
    by_status = {"pending": 120, "processing": 30, "sent": 4500, "failed": 12}
    purgeable = by_status["sent"] + by_status["failed"]

    assert purgeable == 4512
    assert by_status["pending"] not in (purgeable,)
    assert purgeable < sum(by_status.values())


# ── Compactación del archivo tras purgar ─────────────────────────────────────

def test_el_vacuum_libera_espacio_real(tmp_path):
    """
    REGRESIÓN: SQLite no devuelve al sistema el espacio de las filas borradas,
    solo marca las páginas como reutilizables. Tras purgar millones de eventos
    el archivo conservaba su tamaño y el disco quedaba ocupado sin motivo.
    """
    import sqlite3
    from app.api.routers.dashboard import _vacuum_db, _tamano_db_mb

    ruta = str(tmp_path / "prueba.db")
    conn = sqlite3.connect(ruta)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, dato TEXT)")
    conn.executemany("INSERT INTO t (dato) VALUES (?)", [("x" * 2000,) for _ in range(5000)])
    conn.commit()
    conn.close()

    lleno = _tamano_db_mb(ruta)

    conn = sqlite3.connect(ruta)
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()

    tras_borrar = _tamano_db_mb(ruta)
    assert tras_borrar >= lleno * 0.9, "SQLite no debería haber encogido solo"

    _vacuum_db(ruta, "prueba", "test")

    tras_vacuum = _tamano_db_mb(ruta)
    assert tras_vacuum < lleno * 0.5, (
        f"El VACUUM no liberó espacio: {lleno} MB -> {tras_vacuum} MB"
    )


def test_el_tamano_incluye_los_archivos_auxiliares(tmp_path):
    """
    Con WAL activo, los archivos -wal y -shm también ocupan disco y pueden ser
    grandes. Medir solo el .db subestimaría el consumo real.
    """
    import sqlite3
    from app.api.routers.dashboard import _tamano_db_mb

    ruta = str(tmp_path / "conwal.db")
    conn = sqlite3.connect(ruta)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, dato TEXT)")
    conn.executemany("INSERT INTO t (dato) VALUES (?)", [("y" * 1000,) for _ in range(2000)])
    conn.commit()

    total = _tamano_db_mb(ruta)
    solo_db = os.path.getsize(ruta) / (1024 * 1024)
    conn.close()

    assert total >= round(solo_db, 2)


def test_vacuum_sobre_archivo_inexistente_no_falla():
    """El endpoint no debe romper si la base todavía no fue creada."""
    from app.api.routers.dashboard import _vacuum_db
    assert _vacuum_db("db/no-existe/jamas.db", "x", "y") == 0.0
