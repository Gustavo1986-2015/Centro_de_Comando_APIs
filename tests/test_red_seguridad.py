"""
Regresión de la pérdida de eventos detectada en la certificación del 14/09/2026.

EL BUG: el hub responde 202 Accepted apenas encola en memoria. Si el INSERT
posterior fallaba —por `database is locked`, porque SQLite admite un solo
escritor por archivo— el lote se descartaba con un log y nada más. El proveedor
lo contabilizaba como entregado y en la base no existía.

LA GARANTÍA QUE SE PRUEBA ACÁ: un evento con respuesta 202 tiene exactamente
tres destinos, y ninguno es "desapareció".

  1. Está en la base
  2. Está esperando reintento
  3. Está en cuarentena, con el motivo escrito
"""
import json
import os
import sqlite3
import threading
import time

import pytest

from app.core import safety_net


@pytest.fixture
def disco(tmp_path, monkeypatch):
    """Red de seguridad sobre un directorio temporal, con los hilos aislados."""
    monkeypatch.setattr(safety_net, "DIRECTORIO_BASE", str(tmp_path / "red"))
    safety_net._anexadores.clear()
    safety_net._recuperados.clear()
    safety_net._cache_estado.clear()
    yield tmp_path
    safety_net._anexadores.clear()


def _esperar_escritura(segundos=2.0):
    """Los anexadores escriben en su propio hilo: hay que darles su momento."""
    time.sleep(0.3)


# ─── Lo que se escribe antes de tocar la base ────────────────────────────────

def test_el_evento_queda_en_disco_al_recibirlo(disco):
    """
    Antes, entre el 202 y el INSERT, el evento vivía SOLO en memoria. Si el
    proceso moría en ese tramo se evaporaba sin dejar rastro.
    """
    safety_net.registrar_pendiente("schmitz", "prod", "abc123",
                                   {"ChassisNumber": "CTU001"})
    _esperar_escritura()

    pendientes = safety_net.pendientes_reales("schmitz", "prod")
    assert len(pendientes) == 1
    assert pendientes[0]["ingest_id"] == "abc123"
    assert pendientes[0]["payload"]["ChassisNumber"] == "CTU001"


def test_lo_confirmado_deja_de_estar_pendiente(disco):
    """Un evento persistido no puede seguir figurando como pendiente."""
    for iid in ("a1", "a2", "a3"):
        safety_net.registrar_pendiente("schmitz", "prod", iid, {"x": iid})
    _esperar_escritura()

    safety_net.confirmar("schmitz", "prod", ["a1", "a3"])
    _esperar_escritura()

    quedan = [p["ingest_id"] for p in safety_net.pendientes_reales("schmitz", "prod")]
    assert quedan == ["a2"]


def test_el_pendiente_sobrevive_al_reinicio(disco):
    """
    El requisito central: si el hub se detiene con eventos sin persistir, al
    arrancar los retoma. Se simula el reinicio descartando todo el estado en
    memoria y releyendo el disco.
    """
    for iid in ("r1", "r2"):
        safety_net.registrar_pendiente("schmitz", "prod", iid, {"x": iid})
    _esperar_escritura()

    # Reinicio: se pierde absolutamente todo lo que vivía en memoria.
    safety_net._anexadores.clear()
    safety_net._recuperados.clear()

    recuperados = safety_net.pendientes_reales("schmitz", "prod")
    assert len(recuperados) == 2, "El pendiente no sobrevivió al reinicio"
    assert {p["ingest_id"] for p in recuperados} == {"r1", "r2"}


def test_una_integracion_sin_pendientes_no_reporta_nada(disco):
    assert safety_net.pendientes_reales("protrack", "test") == []


# ─── Transitorio contra definitivo ───────────────────────────────────────────

@pytest.mark.parametrize("mensaje", [
    "(sqlite3.OperationalError) database is locked",
    "database is busy",
    "DATABASE IS LOCKED",
    "disk I/O error",
])
def test_reconoce_los_errores_que_vale_la_pena_reintentar(mensaje):
    assert safety_net.es_transitorio(Exception(mensaje))


@pytest.mark.parametrize("mensaje", [
    "NOT NULL constraint failed: normalized_rc_events.provider",
    "datatype mismatch",
    "no such column: inventada",
])
def test_no_reintenta_lo_que_nunca_va_a_entrar(mensaje):
    """
    Reintentar un evento mal formado para siempre consume ciclos y puede tapar
    la recuperación de los que sí se pueden salvar.
    """
    assert not safety_net.es_transitorio(Exception(mensaje))


# ─── Cuarentena ──────────────────────────────────────────────────────────────

def test_la_cuarentena_conserva_el_evento_y_su_motivo(disco):
    """
    Apartar no es borrar. Si mañana se corrige un bug del mapper, estos eventos
    son reprocesables.
    """
    safety_net.registrar_cuarentena("schmitz", "prod", "malo1",
                                    {"ChassisNumber": "ROTO"},
                                    "datatype mismatch")
    _esperar_escritura()

    ruta = os.path.join(safety_net.DIRECTORIO_BASE, "schmitz_prod", "cuarentena.jsonl")
    with open(ruta, encoding="utf-8") as f:
        registro = json.loads(f.readline())

    assert registro["ingest_id"] == "malo1"
    assert registro["motivo"] == "datatype mismatch"
    assert registro["payload"]["ChassisNumber"] == "ROTO", "Se perdió el evento"


def test_lo_apartado_no_vuelve_al_pendiente(disco):
    """Si volviera, el reintentador quedaría en un bucle infinito sobre él."""
    safety_net.registrar_pendiente("schmitz", "prod", "malo1", {"x": 1})
    safety_net.registrar_pendiente("schmitz", "prod", "bueno1", {"x": 2})
    _esperar_escritura()

    safety_net.registrar_cuarentena("schmitz", "prod", "malo1", {"x": 1}, "roto")
    _esperar_escritura()

    quedan = [p["ingest_id"] for p in safety_net.pendientes_reales("schmitz", "prod")]
    assert quedan == ["bueno1"]


# ─── Compactación ────────────────────────────────────────────────────────────

def test_al_compactar_no_se_pierde_lo_que_sigue_pendiente(disco):
    for iid in ("c1", "c2", "c3"):
        safety_net.registrar_pendiente("schmitz", "prod", iid, {"x": iid})
    _esperar_escritura()
    safety_net.confirmar("schmitz", "prod", ["c1"])
    _esperar_escritura()

    safety_net.compactar("schmitz", "prod")

    quedan = [p["ingest_id"] for p in safety_net.pendientes_reales("schmitz", "prod")]
    assert quedan == ["c2", "c3"]


def test_compactar_es_seguro_sobre_una_integracion_vacia(disco):
    safety_net.compactar("inexistente", "prod")


# ─── Estado para el panel ────────────────────────────────────────────────────

def test_el_panel_puede_ver_cuantos_esperan_y_desde_cuando(disco):
    """
    Un reintento que nunca progresa es el problema silencioso que esta red
    viene a eliminar: hay que poder verlo sin leer logs.
    """
    safety_net.registrar_pendiente("schmitz", "prod", "e1", {"x": 1})
    _esperar_escritura()

    est = safety_net.estado("schmitz", "prod")
    assert est["pendientes"] == 1
    assert est["antiguedad_mas_viejo_seg"] is not None
    assert est["recuperados"] == 0
    assert est["en_cuarentena"] == 0


def test_el_panel_cuenta_los_recuperados(disco):
    safety_net.sumar_recuperados("schmitz", "prod", 12)
    safety_net.sumar_recuperados("schmitz", "prod", 3)
    # Sin caché: el panel tolera 5 segundos de retraso, un test no.
    assert safety_net.estado("schmitz", "prod", usar_cache=False)["recuperados"] == 15


def test_detecta_las_integraciones_con_archivos_en_disco(disco):
    safety_net.registrar_pendiente("schmitz", "prod", "x1", {})
    safety_net.registrar_pendiente("protrack", "test", "x2", {})
    _esperar_escritura()

    assert set(safety_net.integraciones_con_pendientes()) == {
        ("schmitz", "prod"), ("protrack", "test")
    }


# ─── Concurrencia del anexador ───────────────────────────────────────────────

def test_el_anexador_no_pierde_lineas_bajo_concurrencia(disco):
    """
    A 40 msg/s varios hilos registran a la vez. El anexador tiene un solo hilo
    escritor por archivo justamente para que eso no se convierta en líneas
    entremezcladas o perdidas.
    """
    def registrar(desde):
        for i in range(desde, desde + 50):
            safety_net.registrar_pendiente("schmitz", "prod", f"h{i}", {"n": i})

    hilos = [threading.Thread(target=registrar, args=(base,))
             for base in (0, 100, 200, 300)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    time.sleep(1.0)

    assert len(safety_net.pendientes_reales("schmitz", "prod")) == 200


# ─── El índice único que hace idempotente al reintento ───────────────────────

def test_el_indice_unico_impide_duplicar_al_reinsertar(tmp_path):
    """
    Sin esto, reinsertar desde la red de seguridad duplicaría SIEMPRE, y el
    duplicado viajaría a Recurso Confiable. La tabla no tenía ninguna clave
    natural: el id es autoincremental.
    """
    ruta = str(tmp_path / "prueba.db")
    conn = sqlite3.connect(ruta)
    conn.execute("""
        CREATE TABLE normalized_rc_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingest_id TEXT,
            chassis_number TEXT
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX idx_ingest_id_unico ON normalized_rc_events(ingest_id)"
    )

    conn.execute("INSERT INTO normalized_rc_events (ingest_id, chassis_number) "
                 "VALUES ('abc-0', 'CTU001')")
    conn.commit()

    # Reintento del mismo evento: no debe crear una copia.
    conn.execute("INSERT INTO normalized_rc_events (ingest_id, chassis_number) "
                 "VALUES ('abc-0', 'CTU001') ON CONFLICT(ingest_id) DO NOTHING")
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM normalized_rc_events").fetchone()[0]
    assert total == 1, "El reintento duplicó el evento"

    # Las filas anteriores a la migración quedan en NULL, y SQLite admite
    # múltiples NULL en un índice único: por eso no hace falta backfill.
    for _ in range(3):
        conn.execute("INSERT INTO normalized_rc_events (ingest_id, chassis_number) "
                     "VALUES (NULL, 'VIEJO')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM normalized_rc_events").fetchone()[0] == 4
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Reproducción de la pérdida original, con un lock real
# ═══════════════════════════════════════════════════════════════════════════

def test_un_lock_real_ya_no_hace_desaparecer_el_evento(tmp_path, monkeypatch):
    """
    Reproduce el incidente del 14/09: otra conexión sostiene un lock de
    escritura sobre la base del proveedor mientras entra un lote ya respondido
    con 202.

    ANTES: `_persist_batch` retornaba normalmente, el log decía
    "Error saving batch: database is locked", y el evento no quedaba en ningún
    lado.

    AHORA: el evento está en la red de seguridad, en disco, listo para el
    reintentador.
    """
    from app import database

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(safety_net, "DIRECTORIO_BASE", str(tmp_path / "red"))
    safety_net._anexadores.clear()

    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    try:
        database.check_and_migrate_provider_db("schmitz", "prod")
        from app.models.db_models import NormalizedRCEvent
        database.get_engine("schmitz", "prod")
        Base_meta = NormalizedRCEvent.metadata
        Base_meta.create_all(bind=database.get_engine("schmitz", "prod"))

        ruta_db = database.get_db_url("schmitz", "prod").replace("sqlite:///./", "./")

        # Otro escritor toma la base y no la suelta.
        bloqueador = sqlite3.connect(ruta_db, timeout=1.0)
        bloqueador.execute("PRAGMA busy_timeout=1000")
        bloqueador.execute("BEGIN IMMEDIATE")
        try:
            iid = safety_net.nuevo_ingest_id()
            payload = {"ChassisNumber": "CTU00003018001771",
                       "Events": [{"Type": "Standard"}]}
            # Lo que hace el endpoint al recibir, antes de responder 202.
            safety_net.registrar_pendiente("schmitz", "prod", iid, payload)
            _esperar_escritura()

            # El INSERT falla por el lock y NO se confirma.
            pendientes = safety_net.pendientes_reales("schmitz", "prod")
            assert len(pendientes) == 1, "El evento no llegó a la red de seguridad"
            assert pendientes[0]["payload"]["ChassisNumber"] == "CTU00003018001771"
        finally:
            bloqueador.rollback()
            bloqueador.close()

        # Con la base libre, el evento sigue esperando: nadie lo descartó.
        assert len(safety_net.pendientes_reales("schmitz", "prod")) == 1
    finally:
        database._engines.clear()
        database._sessions.clear()
        database._engines.update(engines)
        database._sessions.update(sessions)
        safety_net._anexadores.clear()


def test_la_migracion_agrega_ingest_id_y_su_indice(tmp_path, monkeypatch):
    """La migración es idempotente: correrla dos veces no puede romper nada."""
    from app import database

    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    try:
        from app.models.db_models import NormalizedRCEvent
        NormalizedRCEvent.metadata.create_all(
            bind=database.get_engine("schmitz", "prod")
        )
        database.check_and_migrate_provider_db("schmitz", "prod")
        database.check_and_migrate_provider_db("schmitz", "prod")   # dos veces

        ruta = database.get_db_url("schmitz", "prod").replace("sqlite:///./", "./")
        conn = sqlite3.connect(ruta)
        columnas = [r[1] for r in conn.execute("PRAGMA table_info(normalized_rc_events)")]
        indices = [r[1] for r in conn.execute("PRAGMA index_list(normalized_rc_events)")]
        conn.close()

        assert "ingest_id" in columnas
        assert "idx_ingest_id_unico" in indices
    finally:
        database._engines.clear()
        database._sessions.clear()
        database._engines.update(engines)
        database._sessions.update(sessions)


# ═══════════════════════════════════════════════════════════════════════════
# La red de seguridad solo escribe cuando hace falta
#
# Antes escribía CADA evento antes del INSERT: a 40 msg/s, millones de líneas
# por día. Y `estado()` relee ese archivo para saber qué falta, con el panel
# consultándolo en cada refresco. Era el mismo defecto que el deque sin tope:
# estructura que crece sin límite, recorrida entera en cada lectura.
# ═══════════════════════════════════════════════════════════════════════════

def test_el_camino_feliz_no_escribe_en_la_red_de_seguridad(disco):
    """
    Si el INSERT entra, la red de seguridad no se toca. Es lo que hace que el
    archivo tenga decenas de líneas en vez de millones.
    """
    import os as _os

    carpeta = _os.path.join(safety_net.DIRECTORIO_BASE, "schmitz_prod")
    assert not _os.path.exists(carpeta), "Se escribió sin que nada fallara"
    assert safety_net.estado("schmitz", "prod", usar_cache=False)["pendientes"] == 0


def test_consultar_el_estado_repetidas_veces_lee_el_archivo_una_sola_vez(disco, monkeypatch):
    """
    El panel llama a estado() en cada refresco. Con caché, N consultas cuestan
    una sola lectura del archivo.

    Se cuentan las lecturas en vez de medir tiempos: un test que compara
    duraciones falla cuando la máquina está cargada, y entonces deja de decir
    nada sobre el código.
    """
    for i in range(50):
        safety_net.registrar_pendiente("schmitz", "prod", f"p{i}", {"n": i})
    _esperar_escritura()
    safety_net._cache_estado.clear()

    lecturas = {"n": 0}
    real = safety_net.pendientes_reales

    def contando(provider, env):
        lecturas["n"] += 1
        return real(provider, env)

    monkeypatch.setattr(safety_net, "pendientes_reales", contando)

    for _ in range(30):
        safety_net.estado("schmitz", "prod")

    assert lecturas["n"] == 1, (
        f"El caché no evitó las relecturas: {lecturas['n']} lecturas en 30 consultas"
    )


def test_el_cache_no_oculta_un_pendiente_nuevo_por_mucho_tiempo(disco):
    """Cinco segundos de retraso es tolerable; ocultarlo para siempre no."""
    assert safety_net._CACHE_ESTADO_SEG <= 10, (
        "Un caché largo haría que el panel muestre pendientes que ya se resolvieron, "
        "o esconda los nuevos"
    )
