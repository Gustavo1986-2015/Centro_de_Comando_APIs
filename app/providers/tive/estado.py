"""
Estado persistente del módulo de Tive: pares aprendidos y eventos ya vistos.

Vive en un SQLite propio, db/tive/{env}_estado.db, separado de la cola de
eventos. Tiene que sobrevivir a un reinicio por dos razones medidas:

  - PARES: un tramo de contenedor no trae nombre de equipo. La patente sale
    del par DeviceId -> DeviceName aprendido de eventos anteriores del mismo
    equipo, que pueden haber llegado días antes (Tive informó que durante el
    tránsito marítimo los sensores quedan en memoria y se descargan al llegar
    a puerto).
  - VISTOS: Tive entrega "al menos una vez". La actualización de una alerta
    abierta de J392825 se reenvió 9 veces a lo largo de 5 horas el 01/10. Una
    deduplicación en memoria la volvería a enviar después de cada reinicio.

DEDUPLICACIÓN E IDEMPOTENCIA

Cada clave guarda el ingest_id de la recepción que la registró. Si el mismo
payload vuelve a procesarse con el MISMO ingest_id (la red de seguridad
reintentando un INSERT que falló), no es un duplicado: es el mismo evento, y
la base ya lo frena con ON CONFLICT sobre ingest_id. Sin esto, un evento que
cayó a la red de seguridad se descartaría como duplicado de sí mismo.

Las claves que se registran a partir de un duplicado se marcan con el prefijo
"dup:" para que ni siquiera un reintento de esa misma recepción pase.

Una clave se olvida si nadie la vuelve a ver en DIAS_RETENCION días. Cada vez
que llega un duplicado se refresca, así que una alerta que Tive sigue
reenviando no se olvida mientras siga llegando.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DIAS_RETENCION = 30
_SEGUNDOS_ENTRE_PURGAS = 3600

# Directorio de las bases de estado. Los tests lo apuntan a una carpeta
# temporal para no tocar db/ real.
DIRECTORIO = os.path.join(".", "db", "tive")

_lock = threading.RLock()
_conexiones: dict[str, sqlite3.Connection] = {}
_ultima_purga: dict[str, float] = {}


def _ruta(env: str) -> str:
    # Absoluta: es también la clave del caché de conexiones, y una ruta
    # relativa apuntaría a otro archivo si cambia el directorio de trabajo.
    return os.path.abspath(os.path.join(DIRECTORIO, f"{env}_estado.db"))


def _conexion(env: str) -> sqlite3.Connection:
    ruta = _ruta(env)
    con = _conexiones.get(ruta)
    if con is None:
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        con = sqlite3.connect(ruta, check_same_thread=False, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute(
            "CREATE TABLE IF NOT EXISTS pares ("
            " device_id TEXT PRIMARY KEY, device_name TEXT NOT NULL,"
            " actualizado TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS vistos ("
            " clave TEXT PRIMARY KEY, ingest_id TEXT NOT NULL,"
            " primera REAL NOT NULL, ultima REAL NOT NULL)"
        )
        # v1.9.4: de dónde salió el par. 'webhook' = el tracker reportó con su
        # nombre; 'api' = lo resolvió el resolutor consultando a Tive.
        columnas = {r[1] for r in con.execute("PRAGMA table_info(pares)")}
        if "origen" not in columnas:
            con.execute("ALTER TABLE pares ADD COLUMN origen TEXT NOT NULL DEFAULT 'webhook'")
        # v1.9.4: tramos de contenedor retenidos hasta conocer el nombre del
        # tracker, y la cola de consultas a la API de Tive.
        con.execute(
            "CREATE TABLE IF NOT EXISTS tramos_pendientes ("
            " ingest_id TEXT PRIMARY KEY, payload TEXT NOT NULL, account_id TEXT,"
            " series TEXT NOT NULL, recibido REAL NOT NULL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS consultas ("
            " serie TEXT PRIMARY KEY, account_id TEXT, estado TEXT NOT NULL,"
            " intentos INTEGER NOT NULL DEFAULT 0, proximo REAL NOT NULL,"
            " ultimo_error TEXT, actualizado REAL NOT NULL)"
        )
        con.commit()
        _conexiones[ruta] = con
    return con


def cerrar_todo():
    """Para los tests: suelta los archivos."""
    with _lock:
        for con in _conexiones.values():
            try:
                con.close()
            except Exception:
                pass
        _conexiones.clear()
        _ultima_purga.clear()


# ── Pares DeviceId -> DeviceName ─────────────────────────────────────────────

ORIGENES_PAR = ("webhook", "api")


def aprender_par(env: str, device_id, device_name, origen: str = "webhook") -> None:
    """
    Registra o actualiza el par. Un cambio de nombre queda en consola.

    El origen distingue un par informado por el propio tracker ('webhook')
    de uno resuelto por la API de Tive ('api'). Si el tracker confirma por
    webhook un par que vino de la API, pasa a 'webhook'.
    """
    if origen not in ORIGENES_PAR:
        raise ValueError(f"origen de par desconocido: {origen!r}")
    if not device_id or not device_name:
        return
    device_id, device_name = str(device_id).strip(), str(device_name).strip()
    if not device_id or not device_name:
        return
    with _lock:
        con = _conexion(env)
        fila = con.execute(
            "SELECT device_name, origen FROM pares WHERE device_id = ?", (device_id,)
        ).fetchone()
        if fila and fila[0] == device_name and (fila[1] == origen or origen == "api"):
            return
        con.execute(
            "INSERT INTO pares (device_id, device_name, actualizado, origen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET device_name = excluded.device_name, "
            "actualizado = excluded.actualizado, origen = excluded.origen",
            (device_id, device_name, datetime.now(timezone.utc).isoformat(), origen),
        )
        con.commit()
    if fila and fila[0] != device_name:
        logger.warning(
            f"[TIVE-{env}] El equipo {device_id} cambió de nombre: "
            f"{fila[0]} -> {device_name} (origen {origen}). Se usa el nuevo."
        )
    elif not fila:
        logger.info(f"[TIVE-{env}] Par aprendido ({origen}): {device_id} -> {device_name}.")


def origen_de(env: str, device_id) -> str | None:
    if not device_id:
        return None
    with _lock:
        fila = _conexion(env).execute(
            "SELECT origen FROM pares WHERE device_id = ?", (str(device_id).strip(),)
        ).fetchone()
    return fila[0] if fila else None


# ── Tramos retenidos y consultas a la API (v1.9.4) ──────────────────────────

def retener_tramo(env: str, ingest_id: str, payload: dict, account_id, series: list) -> bool:
    """
    Guarda un tramo de contenedor sin patente hasta que se aprenda el nombre.
    INSERT OR IGNORE: reprocesarlo no le reinicia la antigüedad. True si es nuevo.
    """
    with _lock:
        con = _conexion(env)
        cur = con.execute(
            "INSERT OR IGNORE INTO tramos_pendientes (ingest_id, payload, account_id, series, recibido)"
            " VALUES (?, ?, ?, ?, ?)",
            (ingest_id, json.dumps(payload, ensure_ascii=False),
             str(account_id) if account_id is not None else None, json.dumps(series), time.time()),
        )
        con.commit()
        return cur.rowcount == 1


def tramos_pendientes(env: str) -> list:
    with _lock:
        filas = _conexion(env).execute(
            "SELECT ingest_id, payload, account_id, series, recibido FROM tramos_pendientes"
            " ORDER BY recibido"
        ).fetchall()
    return [{"ingest_id": f[0], "payload": json.loads(f[1]), "account_id": f[2],
             "series": json.loads(f[3]), "recibido": f[4]} for f in filas]


def quitar_tramo(env: str, ingest_id: str) -> None:
    with _lock:
        con = _conexion(env)
        con.execute("DELETE FROM tramos_pendientes WHERE ingest_id = ?", (ingest_id,))
        con.commit()


def encolar_consulta(env: str, serie: str, account_id) -> bool:
    """Pide resolver una serie. No duplica ni adelanta una que ya está en espera."""
    ahora = time.time()
    with _lock:
        con = _conexion(env)
        cur = con.execute(
            "INSERT OR IGNORE INTO consultas (serie, account_id, estado, intentos, proximo, actualizado)"
            " VALUES (?, ?, 'pendiente', 0, ?, ?)",
            (serie, str(account_id) if account_id is not None else None, ahora, ahora),
        )
        con.commit()
        return cur.rowcount == 1


def consultas_a_realizar(env: str, limite: int) -> list:
    """Series cuya consulta toca ahora: pendientes, a reintentar, o no encontradas
    cuya espera de 24 h ya venció."""
    with _lock:
        filas = _conexion(env).execute(
            "SELECT serie, account_id, intentos FROM consultas"
            " WHERE estado IN ('pendiente', 'reintentar', 'no_encontrada') AND proximo <= ?"
            " ORDER BY proximo LIMIT ?", (time.time(), limite)
        ).fetchall()
    return [{"serie": f[0], "account_id": f[1], "intentos": f[2]} for f in filas]


def marcar_consulta(env: str, serie: str, estado: str, proximo=None, error=None,
                    sumar_intento: bool = False, account_id=None) -> None:
    ahora = time.time()
    with _lock:
        con = _conexion(env)
        con.execute(
            "UPDATE consultas SET estado = ?, proximo = ?, ultimo_error = ?, actualizado = ?,"
            " intentos = intentos + ?, account_id = COALESCE(?, account_id) WHERE serie = ?",
            (estado, proximo if proximo is not None else ahora, error, ahora,
             1 if sumar_intento else 0, str(account_id) if account_id is not None else None, serie),
        )
        con.commit()


def consulta(env: str, serie: str):
    with _lock:
        f = _conexion(env).execute(
            "SELECT serie, account_id, estado, intentos, proximo, ultimo_error FROM consultas"
            " WHERE serie = ?", (serie,)
        ).fetchone()
    if not f:
        return None
    return {"serie": f[0], "account_id": f[1], "estado": f[2], "intentos": f[3],
            "proximo": f[4], "ultimo_error": f[5]}


def nombre_de(env: str, device_id) -> str | None:
    if not device_id:
        return None
    with _lock:
        fila = _conexion(env).execute(
            "SELECT device_name FROM pares WHERE device_id = ?", (str(device_id).strip(),)
        ).fetchone()
    return fila[0] if fila else None


# ── Duplicados ───────────────────────────────────────────────────────────────

def registrar_o_duplicado(env: str, claves: list[str], ingest_id: str) -> bool:
    """
    Devuelve True si el evento es un duplicado de uno ya visto.

    Es duplicado si CUALQUIERA de sus claves ya fue registrada por otra
    recepción. Si no lo es, registra todas con este ingest_id. Si lo es,
    registra las que falten marcadas como duplicado: así una retransmisión
    que coincida solo por esa otra clave también se reconoce.
    """
    claves = [c for c in claves if c]
    if not claves:
        return False
    ahora = time.time()
    with _lock:
        con = _conexion(env)
        _purgar_si_corresponde(con, env, ahora)
        existentes = {}
        for clave in claves:
            fila = con.execute(
                "SELECT ingest_id FROM vistos WHERE clave = ?", (clave,)
            ).fetchone()
            if fila:
                existentes[clave] = fila[0]

        duplicado = any(iid != ingest_id for iid in existentes.values())
        marca = f"dup:{ingest_id}" if duplicado else ingest_id
        for clave in claves:
            if clave in existentes:
                con.execute("UPDATE vistos SET ultima = ? WHERE clave = ?", (ahora, clave))
            else:
                con.execute(
                    "INSERT INTO vistos (clave, ingest_id, primera, ultima) VALUES (?, ?, ?, ?)",
                    (clave, marca, ahora, ahora),
                )
        con.commit()
    return duplicado


def alerta_abierta_registrada(env: str, alert_id: str) -> bool:
    """¿Ya se registró la apertura de este AlertId, para cualquier patente?"""
    if not alert_id:
        return False
    with _lock:
        fila = _conexion(env).execute(
            "SELECT 1 FROM vistos WHERE clave LIKE ? LIMIT 1",
            (f"alerta|{alert_id}|apertura|%",),
        ).fetchone()
    return fila is not None


def _purgar_si_corresponde(con: sqlite3.Connection, env: str, ahora: float) -> None:
    if ahora - _ultima_purga.get(env, 0) < _SEGUNDOS_ENTRE_PURGAS:
        return
    _ultima_purga[env] = ahora
    corte = ahora - DIAS_RETENCION * 86400
    borradas = con.execute("DELETE FROM vistos WHERE ultima < ?", (corte,)).rowcount
    con.commit()
    if borradas:
        logger.info(
            f"[TIVE-{env}] {borradas} clave(s) de deduplicación sin actividad en "
            f"{DIAS_RETENCION} días, olvidadas."
        )
