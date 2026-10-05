"""
Registro persistente de descartes: qué no se envió a RC, de quién y por qué.

POR QUÉ EXISTE

Hasta la v1.9.3 cada descarte quedaba solo en la consola: el filtro de
admisión, la validación del contrato y el módulo de Tive avisaban, pero un
reinicio se llevaba todo, y no había forma de responder "¿cuántas posiciones
de contenedor se descartaron ayer, y de qué equipo?" sin leer logs.

Ahora cada descarte, además del aviso en consola, queda en
db/descartes.db, y el panel (Diagnóstico y Salud) muestra el conteo por
integración y motivo y los últimos descartes con hora, equipo, envío, motivo
y AlertId.

SIN COSTO PARA LA INGESTA

registrar() no escribe: encola y vuelve. Un hilo dedicado escribe en lotes,
como el anexador de crudos. Si la cola se llena (la base trabada), los
descartes que no entran se cuentan y se avisa en consola: el aviso original
del descarte ya salió, así que no se pierde el rastro, solo su copia en la
base.

TOPE Y RETENCIÓN

Se guardan DIAS_RETENCION días y como máximo MAX_FILAS filas; lo más viejo se
borra primero. Es un registro de diagnóstico, no un archivo histórico.
"""
import logging
import os
import queue
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

DIAS_RETENCION = 7
MAX_FILAS = 50_000
_TAM_COLA = 10_000
_TAM_LOTE = 500
_SEGUNDOS_ENTRE_PURGAS = 600

# Los tests lo apuntan a una carpeta temporal.
DIRECTORIO = os.path.join(".", "db")
ARCHIVO = "descartes.db"

ORIGENES = ("admision", "contrato", "tive")

_cola: "queue.Queue[tuple]" = queue.Queue(maxsize=_TAM_COLA)
_lock_hilo = threading.Lock()
_hilo: threading.Thread | None = None
_perdidos = 0
_ultimo_aviso_perdidos = 0.0
_ultima_purga = 0.0


def _ruta() -> str:
    return os.path.abspath(os.path.join(DIRECTORIO, ARCHIVO))


def _conectar() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_ruta()), exist_ok=True)
    con = sqlite3.connect(_ruta(), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS descartes ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
        " proveedor TEXT NOT NULL, env TEXT, origen TEXT NOT NULL, motivo TEXT NOT NULL,"
        " detalle TEXT, equipo TEXT, envio TEXT, alert_id TEXT)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_descartes_ts ON descartes (ts)")
    return con


def _texto(valor, largo: int = 300) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto[:largo] if texto else None


def registrar(proveedor: str, env: str | None, origen: str, motivo: str,
              equipo=None, envio=None, alert_id=None, detalle=None) -> None:
    """Encola un descarte para guardarlo. Nunca bloquea ni lanza."""
    global _perdidos, _ultimo_aviso_perdidos
    fila = (time.time(), (proveedor or "?").lower(), (env or "?").lower(), origen,
            _texto(motivo, 200) or "sin motivo", _texto(detalle, 500),
            _texto(equipo), _texto(envio), _texto(alert_id, 80))
    _asegurar_hilo()
    try:
        _cola.put_nowait(fila)
    except queue.Full:
        _perdidos += 1
        ahora = time.time()
        if ahora - _ultimo_aviso_perdidos > 60:
            _ultimo_aviso_perdidos = ahora
            logger.warning(
                f"Registro de descartes saturado: {_perdidos} descarte(s) no se guardaron en "
                f"la base (sí quedaron en consola). Revisar si db/{ARCHIVO} está bloqueada."
            )


def _asegurar_hilo() -> None:
    global _hilo
    if _hilo is not None and _hilo.is_alive():
        return
    with _lock_hilo:
        if _hilo is None or not _hilo.is_alive():
            _hilo = threading.Thread(target=_escritor, name="registro-descartes", daemon=True)
            _hilo.start()


def _escritor() -> None:
    con, ruta_con = None, None
    while True:
        lote = [_cola.get()]
        while len(lote) < _TAM_LOTE:
            try:
                lote.append(_cola.get_nowait())
            except queue.Empty:
                break
        try:
            if con is None or ruta_con != _ruta():
                if con is not None:
                    con.close()
                con, ruta_con = _conectar(), _ruta()
            con.executemany(
                "INSERT INTO descartes (ts, proveedor, env, origen, motivo, detalle, equipo, envio, alert_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", lote)
            con.commit()
            _purgar_si_corresponde(con)
        except Exception as e:
            logger.error(f"No se pudieron guardar {len(lote)} descarte(s) en db/{ARCHIVO}: {e}")
            try:
                if con is not None:
                    con.close()
            except Exception:
                pass
            con = None
        finally:
            for _ in lote:
                _cola.task_done()


def _purgar_si_corresponde(con: sqlite3.Connection, forzar: bool = False) -> None:
    global _ultima_purga
    ahora = time.time()
    if not forzar and ahora - _ultima_purga < _SEGUNDOS_ENTRE_PURGAS:
        return
    _ultima_purga = ahora
    con.execute("DELETE FROM descartes WHERE ts < ?", (ahora - DIAS_RETENCION * 86400,))
    total = con.execute("SELECT COUNT(*) FROM descartes").fetchone()[0]
    if total > MAX_FILAS:
        con.execute(
            "DELETE FROM descartes WHERE id IN (SELECT id FROM descartes ORDER BY id LIMIT ?)",
            (total - MAX_FILAS,))
    con.commit()


def esperar_escritura(timeout: float = 5.0) -> None:
    """Para tests y para el endpoint: espera a que lo encolado esté en la base."""
    limite = time.time() + timeout
    while _cola.unfinished_tasks and time.time() < limite:
        time.sleep(0.02)


def consultar(limite: int = 100) -> dict:
    """Conteo por integración, origen y motivo, y los últimos descartes."""
    esperar_escritura(1.0)
    limite = max(1, min(int(limite), 1000))
    if not os.path.exists(_ruta()):
        return {"resumen": [], "ultimos": [], "perdidos": _perdidos,
                "retencion_dias": DIAS_RETENCION, "max_filas": MAX_FILAS}
    con = _conectar()
    try:
        con.row_factory = sqlite3.Row
        resumen = [dict(r) for r in con.execute(
            "SELECT proveedor, env, origen, motivo, COUNT(*) AS total, MAX(ts) AS ultimo"
            " FROM descartes GROUP BY proveedor, env, origen, motivo"
            " ORDER BY total DESC, ultimo DESC")]
        ultimos = [dict(r) for r in con.execute(
            "SELECT ts, proveedor, env, origen, motivo, detalle, equipo, envio, alert_id"
            " FROM descartes ORDER BY id DESC LIMIT ?", (limite,))]
    finally:
        con.close()
    return {"resumen": resumen, "ultimos": ultimos, "perdidos": _perdidos,
            "retencion_dias": DIAS_RETENCION, "max_filas": MAX_FILAS}


def reset() -> None:
    """Para los tests: vacía la cola y el contador de perdidos."""
    global _perdidos, _ultima_purga
    esperar_escritura(2.0)
    _perdidos = 0
    _ultima_purga = 0.0
