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

def aprender_par(env: str, device_id, device_name) -> None:
    """Registra o actualiza el par. Un cambio de nombre queda en consola."""
    if not device_id or not device_name:
        return
    device_id, device_name = str(device_id).strip(), str(device_name).strip()
    if not device_id or not device_name:
        return
    with _lock:
        con = _conexion(env)
        fila = con.execute(
            "SELECT device_name FROM pares WHERE device_id = ?", (device_id,)
        ).fetchone()
        if fila and fila[0] == device_name:
            return
        con.execute(
            "INSERT INTO pares (device_id, device_name, actualizado) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET device_name = excluded.device_name, "
            "actualizado = excluded.actualizado",
            (device_id, device_name, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    if fila:
        logger.warning(
            f"[TIVE-{env}] El equipo {device_id} cambió de nombre: "
            f"{fila[0]} -> {device_name}. Se usa el nuevo."
        )
    else:
        logger.info(f"[TIVE-{env}] Par aprendido: {device_id} -> {device_name}.")


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
