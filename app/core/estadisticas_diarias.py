"""
Escritura y lectura de `daily_stats`, el acumulado diario por integración
(v1.9.11).

Hasta v1.9.10 había tres escritores (sumar enviados/fallidos al terminar un
lote, fijar las latencias cada 5 segundos y sumar los recuperados del
reintento manual) y los tres hacían "busco la fila de hoy; si no existe, la
creo" sin restricción única. El primer lote del día lanza en paralelo los dos
primeros: cada uno veía que no había fila y creaba la suya. Desde ahí todos
tomaban `.first()` y la otra fila quedaba en 0 (caso real: SCHMITZ PROD 5439 y
SCHMITZ PROD 0 el mismo día en el historial).

Ahora:
  - la tabla tiene un índice único (date, provider, env), creado al arrancar
    después de fusionar los duplicados que ya existan (`fusionar_duplicados`);
  - toda escritura es un solo `INSERT ... ON CONFLICT DO UPDATE`: crear o
    sumar en una sentencia, sin ventana entre mirar y escribir;
  - provider y env se normalizan (minúsculas, sin espacios) al escribir y al
    leer;
  - quien lee agrupa por (date, provider, env) con la misma regla de fusión,
    así un duplicado que quedara no se ve repetido.
"""
import logging
from datetime import date as _date, datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)

NOMBRE_INDICE_UNICO = "ux_daily_stats_dia"

LATENCIAS = (
    "avg_transmission_latency_sec",
    "avg_hub_latency_sec",
    "avg_rc_latency_sec",
    "avg_push_latency_ms",
)
CONTADORES = ("sent_count", "failed_count", "recovered_count")


def normalizar(valor) -> str:
    """Minúsculas y sin espacios: 'Schmitz ' y 'schmitz' son la misma integración."""
    return "".join(str(valor or "").split()).lower()


def _hoy() -> str:
    # Fecha LOCAL, como siempre (ver dashboard._totales_del_dia_sync).
    return datetime.now().date().isoformat()


def _sesion():
    from app.database import get_session
    return get_session("system_config", "global")


_SQL_SUMAR = text(
    "INSERT INTO daily_stats (date, provider, env, sent_count, failed_count, recovered_count) "
    "VALUES (:d, :p, :e, :s, :f, :r) "
    "ON CONFLICT (date, provider, env) DO UPDATE SET "
    "sent_count = COALESCE(sent_count, 0) + excluded.sent_count, "
    "failed_count = COALESCE(failed_count, 0) + excluded.failed_count, "
    "recovered_count = COALESCE(recovered_count, 0) + excluded.recovered_count"
)

_SQL_LATENCIAS = text(
    "INSERT INTO daily_stats (date, provider, env, sent_count, failed_count, recovered_count, "
    + ", ".join(LATENCIAS) + ") "
    "VALUES (:d, :p, :e, 0, 0, 0, :avg_transmission_latency_sec, :avg_hub_latency_sec, "
    ":avg_rc_latency_sec, :avg_push_latency_ms) "
    "ON CONFLICT (date, provider, env) DO UPDATE SET "
    + ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in LATENCIAS)
)


def _ejecutar(sentencia, params: dict) -> None:
    db = _sesion()
    try:
        try:
            db.execute(sentencia, params)
        except Exception as e:
            # Sin el índice único (una base que no pudo migrarse al arrancar),
            # SQLite rechaza el ON CONFLICT. Se fusiona y se crea el índice en
            # el momento, y se reintenta una vez.
            if "ON CONFLICT" not in str(e).upper():
                raise
            db.rollback()
            migrar_base()
            # Una lectura recarga el esquema en esta conexión: sin ella, SQLite
            # sigue sin ver el índice recién creado.
            db.execute(text("SELECT COUNT(*) FROM sqlite_master")).fetchall()
            db.execute(sentencia, params)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sumar(provider: str, env: str, *, enviados: int = 0, fallidos: int = 0,
          recuperados: int = 0) -> None:
    """Suma al día de hoy, creando la fila si es la primera escritura."""
    if not (enviados or fallidos or recuperados):
        return
    _ejecutar(_SQL_SUMAR, {
        "d": _hoy(), "p": normalizar(provider), "e": normalizar(env),
        "s": int(enviados), "f": int(fallidos), "r": int(recuperados),
    })


def fijar_latencias(provider: str, env: str, **latencias) -> None:
    """Fija las medias de hoy. Una media None deja la que había (como antes)."""
    params = {c: latencias.get(c) for c in LATENCIAS}
    params.update({"d": _hoy(), "p": normalizar(provider), "e": normalizar(env)})
    _ejecutar(_SQL_LATENCIAS, params)


# ── Fusión de duplicados ─────────────────────────────────────────────────────

def fusionar_filas(filas: list[dict]) -> dict:
    """
    Una fila a partir de varias del mismo (date, provider, env).

    Contadores: se suman. Latencias: la no nula; si hay más de una, la de la
    fila con más enviados (a igualdad, la de menor id). La misma regla usan la
    migración y las lecturas.
    """
    ordenadas = sorted(filas, key=lambda f: (-(f.get("sent_count") or 0), f.get("id") or 0))
    base = min(filas, key=lambda f: f.get("id") or 0)
    fusion = {
        "id": base.get("id"),
        "date": base.get("date"),
        "provider": normalizar(base.get("provider")),
        "env": normalizar(base.get("env")),
    }
    for c in CONTADORES:
        fusion[c] = sum(int(f.get(c) or 0) for f in filas)
    for c in LATENCIAS:
        fusion[c] = next((f[c] for f in ordenadas if f.get(c) is not None), None)
    return fusion


def agrupar(filas: list[dict]) -> list[dict]:
    """Agrupa por (date, provider, env) normalizados conservando el orden de llegada."""
    grupos: dict[tuple, list[dict]] = {}
    for f in filas:
        clave = (str(f.get("date")), normalizar(f.get("provider")), normalizar(f.get("env")))
        grupos.setdefault(clave, []).append(f)
    return [fusionar_filas(g) for g in grupos.values()]


def _filas(conexion) -> list[dict]:
    cursor = conexion.execute(
        "SELECT id, date, provider, env, " + ", ".join(CONTADORES + LATENCIAS) + " FROM daily_stats")
    columnas = [d[0] for d in cursor.description]
    return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def fusionar_duplicados(conexion) -> int:
    """
    Migración al arrancar (idempotente), sobre una conexión sqlite3.

    Deja una fila por (date, provider, env) normalizados y recién después crea
    el índice único. Devuelve cuántas filas se fusionaron (borradas por
    sumarse a otra). Todo en una transacción: si algo falla no queda a medias.
    """
    try:
        columnas = {fila[1] for fila in conexion.execute("PRAGMA table_info(daily_stats)")}
        if not {"id", "date", "provider", "env"} <= columnas:
            return 0
        if "recovered_count" not in columnas:
            conexion.execute("ALTER TABLE daily_stats ADD COLUMN recovered_count INTEGER DEFAULT 0")
        filas = _filas(conexion)
        grupos: dict[tuple, list[dict]] = {}
        for f in filas:
            clave = (str(f["date"]), normalizar(f["provider"]), normalizar(f["env"]))
            grupos.setdefault(clave, []).append(f)

        fusionadas = renombradas = 0
        for grupo in grupos.values():
            fusion = fusionar_filas(grupo)
            original = next(f for f in grupo if f["id"] == fusion["id"])
            if len(grupo) == 1 and original["provider"] == fusion["provider"] and original["env"] == fusion["env"]:
                continue
            otros = [f["id"] for f in grupo if f["id"] != fusion["id"]]
            if otros:
                conexion.execute(
                    f"DELETE FROM daily_stats WHERE id IN ({','.join('?' * len(otros))})", otros)
                fusionadas += len(otros)
            if len(grupo) == 1:
                renombradas += 1
            campos = ("provider", "env") + CONTADORES + LATENCIAS
            conexion.execute(
                "UPDATE daily_stats SET " + ", ".join(f"{c} = ?" for c in campos) + " WHERE id = ?",
                [fusion[c] for c in campos] + [fusion["id"]])

        conexion.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {NOMBRE_INDICE_UNICO} ON daily_stats (date, provider, env)")
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise

    if fusionadas:
        logger.info(
            f"Migración daily_stats: {fusionadas} fila(s) duplicada(s) fusionada(s); queda una "
            f"por día, integración y entorno (los totales no cambian).")
    if renombradas:
        logger.info(f"Migración daily_stats: {renombradas} fila(s) con proveedor o entorno "
                    f"normalizado a minúsculas.")
    return fusionadas


def migrar_base() -> int:
    """`fusionar_duplicados` sobre la base de configuración, con conexión propia."""
    import sqlite3
    from app.database import get_db_url
    ruta = get_db_url("system_config", "global").replace("sqlite:///", "", 1)
    conexion = sqlite3.connect(ruta, timeout=30)
    try:
        return fusionar_duplicados(conexion)
    finally:
        conexion.close()


def leer_historial(limite: int = 200) -> list[dict]:
    """Filas del historial, una por (date, provider, env), de la más nueva a la más vieja."""
    db = _sesion()
    try:
        filas = [dict(r._mapping) for r in db.execute(text(
            "SELECT id, date, provider, env, " + ", ".join(CONTADORES + LATENCIAS)
            + " FROM daily_stats ORDER BY date DESC, id ASC"))]
    finally:
        db.close()
    return agrupar(filas)[:limite]


def fecha(valor) -> str:
    if isinstance(valor, (_date, datetime)):
        return valor.strftime("%Y-%m-%d")
    return str(valor or "")[:10]
