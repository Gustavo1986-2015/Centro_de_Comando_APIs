"""
Registro persistente de descartes: qué no se envió a RC, de quién y por qué.

POR QUÉ EXISTE

Hasta la v1.9.3 cada descarte quedaba solo en la consola: el filtro de
admisión, la validación del contrato y el módulo de Tive avisaban, pero un
reinicio se llevaba todo, y no había forma de responder "¿cuántas posiciones
de contenedor se descartaron ayer, y de qué equipo?" sin leer logs.

Ahora cada descarte, además del aviso en consola, queda en
db/descartes.db, y el panel lo muestra (Diagnóstico y Salud, y desde la
v1.9.10 su propia sección "Descartes", con filtros y exportación).

CATEGORÍAS (v1.9.10)

El motivo es texto libre y no se podía filtrar por tipo: lo esperado por
diseño (una posición de tracker que va por RC directo) tapaba lo que hay que
revisar (un evento sin patente). Cada descarte lleva ahora una CATEGORÍA del
catálogo de abajo, que es la única fuente de verdad: código estable, nombre
para el panel, descripción y tipo ("esperado" o "a_revisar"). La categoría es
obligatoria en registrar(); un test recorre el código y falla si aparece un
llamado sin categoría o con una que no está en el catálogo.

SIN COSTO PARA LA INGESTA

registrar() no escribe: encola y vuelve. Un hilo dedicado escribe en lotes,
como el anexador de crudos. Si la cola se llena (la base trabada), los
descartes que no entran se cuentan y se avisa en consola: el aviso original
del descarte ya salió, así que no se pierde el rastro, solo su copia en la
base.

TOPE Y RETENCIÓN

Se guardan N días y como máximo M filas; lo más viejo se borra primero. Es un
registro de diagnóstico, no un archivo histórico. Desde la v1.9.10 los dos
valores se configuran desde el panel (system_settings), dentro de límites
validados en el servidor; por defecto, los de siempre (7 días, 50.000 filas).
"""
import contextvars
import logging
import os
import queue
import sqlite3
import threading
import time
from collections import namedtuple

logger = logging.getLogger(__name__)

DIAS_RETENCION = 7
MAX_FILAS = 50_000
# Límites de la retención configurable (v1.9.10), validados en el servidor.
LIMITES_DIAS = (1, 90)
LIMITES_FILAS = (10_000, 1_000_000)
_TAM_COLA = 10_000
_TAM_LOTE = 500
_SEGUNDOS_ENTRE_PURGAS = 600

# Los tests lo apuntan a una carpeta temporal.
DIRECTORIO = os.path.join(".", "db")
ARCHIVO = "descartes.db"

# "mapeo" (v1.9.9, B-8): integración del Studio sin patente real.
ORIGENES = ("admision", "contrato", "tive", "mapeo")

# ─── Catálogo de categorías (v1.9.10) ────────────────────────────────────────

ESPERADO = "esperado"
A_REVISAR = "a_revisar"
TIPOS = (ESPERADO, A_REVISAR)

Categoria = namedtuple("Categoria", "nombre descripcion tipo")

CATEGORIAS = {
    # ── Esperado: el sistema descarta por diseño ─────────────────────────────
    "admision_filtro": Categoria(
        "Filtro de admisión",
        "El evento no cumple una condición de admisión configurada en el Integration Studio.",
        ESPERADO),
    "tive_posicion_tracker_rc_directo": Categoria(
        "Tive: posición de equipo (va por RC directo)",
        "Posición de un tracker o beacon con 'posiciones_equipos' apagado: llega a SIMON por el "
        "webhook directo de Tive a RC.",
        ESPERADO),
    "tive_alerta_interruptor_apagado": Categoria(
        "Tive: alerta con su interruptor apagado",
        "Alerta de tracker o de beacon con su interruptor apagado en las opciones de Tive.",
        ESPERADO),
    "tive_tercero_interruptor_apagado": Categoria(
        "Tive: tramo de tercero con su interruptor apagado",
        "Posición de contenedor o aérea con 'posiciones_terceros' apagado.",
        ESPERADO),
    "tive_boton_prueba": Categoria(
        "Tive: botón de prueba",
        "Muestra que manda el botón de prueba de Tive (AccountId -1).",
        ESPERADO),
    "tive_duplicado_alerta": Categoria(
        "Tive: alerta duplicada",
        "Tive reenvió una alerta ya recibida (mismo AlertId, o misma lectura con otro AlertId), "
        "o la actualización de una alerta abierta que ya se envió.",
        ESPERADO),
    "tive_duplicado_posicion": Categoria(
        "Tive: posición duplicada",
        "Misma posición (envío, equipo y lectura) ya recibida.",
        ESPERADO),
    # v1.9.11: decisión de configuración de la integración, no un dato roto.
    "coordenadas_cero": Categoria(
        "Coordenadas 0,0 filtradas",
        "El proveedor informó latitud y longitud exactamente 0 y la integración tiene la opción "
        "\"Coordenadas 0,0 informadas por el proveedor\" en filtrar.",
        ESPERADO),
    # ── A revisar: falta un dato o algo está mal configurado ─────────────────
    "sin_patente": Categoria(
        "Sin patente",
        "El evento no trae patente real: el payload no tiene el campo de patente configurado, o "
        "el diccionario no la tradujo. Avisar al prestador.",
        A_REVISAR),
    "sin_fecha": Categoria(
        "Sin fecha",
        "Le falta la fecha del evento, obligatoria para RC.",
        A_REVISAR),
    "sin_coordenadas": Categoria(
        "Sin coordenadas",
        "Le falta la latitud o la longitud, obligatorias para RC.",
        A_REVISAR),
    "admision_mal_configurada": Categoria(
        "Admisión mal configurada",
        "Una condición de admisión está rota (sin campo u operador desconocido): descarta todo "
        "hasta corregirla.",
        A_REVISAR),
    "tive_alerta_sin_tipo": Categoria(
        "Tive: alerta sin AlertType",
        "Alerta sin AlertType: no se sabe qué código mandar.",
        A_REVISAR),
    "tive_alerta_forma_desconocida": Categoria(
        "Tive: forma de alerta no reconocida",
        "Reasons con una forma que no es apertura, actualización, cierre ni puntual.",
        A_REVISAR),
    "tive_alerta_sin_device_id": Categoria(
        "Tive: alerta sin DeviceId",
        "Alerta sin DeviceId: no se sabe si es de un tracker o de un beacon.",
        A_REVISAR),
    "tive_sin_nombre_equipo": Categoria(
        "Tive: sin nombre de equipo",
        "Alerta o tramo de tercero cuya patente no se puede resolver: ningún equipo del envío "
        "tiene par aprendido.",
        A_REVISAR),
    "tive_retenido_vencido": Categoria(
        "Tive: tramo retenido vencido",
        "Tramo de contenedor retenido 24 h sin conocer el nombre del tracker.",
        A_REVISAR),
    "otro": Categoria(
        "Otro",
        "Descartes anteriores a la v1.9.10 sin correspondencia con una categoría.",
        A_REVISAR),
}

CATEGORIA_POR_DEFECTO = "otro"


def categoria_por_motivo(origen: str | None, motivo: str | None) -> str:
    """
    La categoría de un descarte guardado antes de la v1.9.10, por su origen y
    su motivo (los textos que emitía cada llamado). Lo que no corresponde a
    ninguna queda en "otro". Solo se usa para completar filas viejas: los
    llamados nuevos pasan la categoría explícita.
    """
    origen, m = (origen or "").lower(), (motivo or "")
    if origen == "admision":
        return "admision_mal_configurada" if m.startswith("condición de admisión #") else "admision_filtro"
    if origen == "mapeo":
        return "sin_patente" if m.startswith("sin patente real") else CATEGORIA_POR_DEFECTO
    if origen == "contrato" and m.startswith("falta "):
        if "patente" in m:
            return "sin_patente"
        if "fecha" in m:
            return "sin_fecha"
        if "latitud" in m or "longitud" in m:
            return "sin_coordenadas"
        return CATEGORIA_POR_DEFECTO
    if origen == "tive":
        reglas = (
            (lambda t: t == "posición de tracker, va por RC directo", "tive_posicion_tracker_rc_directo"),
            (lambda t: t.startswith("alerta de ") and t.endswith(", interruptor apagado"),
             "tive_alerta_interruptor_apagado"),
            (lambda t: t == "posición de tramo de tercero, interruptor apagado", "tive_tercero_interruptor_apagado"),
            (lambda t: t.startswith("muestra del botón de prueba de Tive"), "tive_boton_prueba"),
            (lambda t: t.startswith("duplicado de ")
             or t == "actualización de una alerta abierta que ya se envió", "tive_duplicado_alerta"),
            (lambda t: t.startswith("posición duplicada"), "tive_duplicado_posicion"),
            (lambda t: t == "alerta sin AlertType", "tive_alerta_sin_tipo"),
            (lambda t: t.startswith("forma de alerta no reconocida"), "tive_alerta_forma_desconocida"),
            (lambda t: t.startswith("alerta sin DeviceId"), "tive_alerta_sin_device_id"),
            (lambda t: t == "alerta sin patente resoluble"
             or t.startswith("tramo de tercero sin patente resoluble"), "tive_sin_nombre_equipo"),
            (lambda t: t.startswith("evento sin nombre de equipo y sin coordenadas"), "sin_coordenadas"),
            (lambda t: t.startswith("tramo retenido 24 h"), "tive_retenido_vencido"),
        )
        for regla, categoria in reglas:
            if regla(m):
                return categoria
    return CATEGORIA_POR_DEFECTO


def categorias_del_tipo(tipo: str) -> list[str]:
    return [codigo for codigo, c in CATEGORIAS.items() if c.tipo == tipo]


def catalogo() -> list[dict]:
    """El catálogo, para el panel y la API."""
    return [{"codigo": codigo, "nombre": c.nombre, "descripcion": c.descripcion, "tipo": c.tipo}
            for codigo, c in CATEGORIAS.items()]


# ─── Vínculo con el crudo (v1.9.10) ──────────────────────────────────────────
# El ingest_id de la recepción que se está procesando. Lo fija quien recibe
# (el webhook genérico, el módulo de Tive, la red de seguridad) y registrar()
# lo toma solo: así ningún llamado intermedio tiene que pasarlo a mano.
_ingest_actual: contextvars.ContextVar[str | None] = contextvars.ContextVar("descartes_ingest_id", default=None)


def fijar_ingest(ingest_id: str | None):
    """Fija el ingest_id de lo que se procesa; devuelve el token para soltarlo."""
    return _ingest_actual.set(ingest_id)


def soltar_ingest(token) -> None:
    _ingest_actual.reset(token)


def con_ingest(ingest_id: str | None, funcion, *args, **kwargs):
    """Corre `funcion` con el ingest_id fijado (sirve para pasarla a un hilo)."""
    token = _ingest_actual.set(ingest_id)
    try:
        return funcion(*args, **kwargs)
    finally:
        _ingest_actual.reset(token)


# ─── Base ────────────────────────────────────────────────────────────────────

_cola: "queue.Queue[tuple]" = queue.Queue(maxsize=_TAM_COLA)
_lock_hilo = threading.Lock()
_hilo: threading.Thread | None = None
_perdidos = 0
_ultimo_aviso_perdidos = 0.0
_ultima_purga = 0.0
_categorias_desconocidas: set[str] = set()
_migradas: set[str] = set()


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
    # v1.9.8: dónde estaba el equipo cuando se descartó el evento.
    # v1.9.10: la categoría del catálogo y el ingest_id para ver el crudo.
    # Migración idempotente: una base de antes recibe las columnas, vacías.
    columnas = {fila[1] for fila in con.execute("PRAGMA table_info(descartes)")}
    for columna, tipo in (("latitud", "REAL"), ("longitud", "REAL"), ("categoria", "TEXT"), ("ingest_id", "TEXT")):
        if columna not in columnas:
            con.execute(f"ALTER TABLE descartes ADD COLUMN {columna} {tipo}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_descartes_categoria_ts ON descartes (categoria, ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_descartes_proveedor_env_ts ON descartes (proveedor, env, ts)")
    if _ruta() not in _migradas:
        _completar_categorias(con)
        _migradas.add(_ruta())
    con.commit()
    return con


def _completar_categorias(con: sqlite3.Connection) -> None:
    """Filas de antes de la v1.9.10: categoría por correspondencia origen+motivo, o "otro"."""
    pares = con.execute("SELECT DISTINCT origen, motivo FROM descartes WHERE categoria IS NULL").fetchall()
    total = 0
    for origen, motivo in pares:
        total += con.execute(
            "UPDATE descartes SET categoria = ? WHERE categoria IS NULL AND origen = ? AND motivo = ?",
            (categoria_por_motivo(origen, motivo), origen, motivo)).rowcount
    if total:
        logger.info(f"Descartes: {total} fila(s) de antes de la v1.9.10 recibieron su categoría.")


def _texto(valor, largo: int = 300) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto[:largo] if texto else None


def coordenada(valor) -> float | None:
    """Una coordenada utilizable, o None (vacía, no numérica o NaN)."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return None if numero != numero else numero


def texto_coordenadas(latitud, longitud) -> str:
    """' lat=…, lon=…' para el aviso de consola, o vacío si no vinieron."""
    lat, lon = coordenada(latitud), coordenada(longitud)
    if lat is None and lon is None:
        return ""
    return f" lat={lat if lat is not None else '—'}, lon={lon if lon is not None else '—'}"


def registrar(proveedor: str, env: str | None, origen: str, motivo: str, *, categoria: str,
              equipo=None, envio=None, alert_id=None, detalle=None,
              latitud=None, longitud=None, ingest_id=None) -> None:
    """
    Encola un descarte para guardarlo. Nunca bloquea ni lanza.

    `categoria` es obligatoria y tiene que estar en CATEGORIAS. Una que no
    está se guarda como "otro" y se avisa una vez en consola.
    """
    global _perdidos, _ultimo_aviso_perdidos
    if categoria not in CATEGORIAS:
        if categoria not in _categorias_desconocidas:
            _categorias_desconocidas.add(categoria)
            logger.warning(f"Descarte con una categoría que no está en el catálogo: {categoria!r}. "
                           f"Se guarda como '{CATEGORIA_POR_DEFECTO}'.")
        categoria = CATEGORIA_POR_DEFECTO
    fila = (time.time(), (proveedor or "?").lower(), (env or "?").lower(), origen,
            _texto(motivo, 200) or "sin motivo", _texto(detalle, 500),
            _texto(equipo), _texto(envio), _texto(alert_id, 80),
            coordenada(latitud), coordenada(longitud), categoria,
            _texto(ingest_id or _ingest_actual.get(), 80))
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
                "INSERT INTO descartes (ts, proveedor, env, origen, motivo, detalle, equipo, envio, alert_id,"
                " latitud, longitud, categoria, ingest_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", lote)
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


# ─── Retención configurable (v1.9.10) ────────────────────────────────────────

def _acotar(valor, limites: tuple[int, int], defecto: int) -> int:
    try:
        valor = int(valor)
    except (TypeError, ValueError):
        return defecto
    return min(max(valor, limites[0]), limites[1])


def retencion_vigente() -> tuple[int, int]:
    """(días, tope de filas) configurados en el panel, o los de siempre."""
    try:
        from app.core.config_cache import get_settings
        settings = get_settings()
        dias = getattr(settings, "descartes_retencion_dias", None)
        filas = getattr(settings, "descartes_max_filas", None)
    except Exception:
        return DIAS_RETENCION, MAX_FILAS
    return (_acotar(dias, LIMITES_DIAS, DIAS_RETENCION) if dias else DIAS_RETENCION,
            _acotar(filas, LIMITES_FILAS, MAX_FILAS) if filas else MAX_FILAS)


def _purgar_si_corresponde(con: sqlite3.Connection, forzar: bool = False) -> None:
    global _ultima_purga
    ahora = time.time()
    if not forzar and ahora - _ultima_purga < _SEGUNDOS_ENTRE_PURGAS:
        return
    _ultima_purga = ahora
    dias, max_filas = retencion_vigente()
    con.execute("DELETE FROM descartes WHERE ts < ?", (ahora - dias * 86400,))
    total = con.execute("SELECT COUNT(*) FROM descartes").fetchone()[0]
    if total > max_filas:
        con.execute(
            "DELETE FROM descartes WHERE id IN (SELECT id FROM descartes ORDER BY id LIMIT ?)",
            (total - max_filas,))
    con.commit()


def purgar_ahora() -> None:
    """Aplica la retención en el momento (al cambiarla desde el panel)."""
    esperar_escritura(2.0)
    if not os.path.exists(_ruta()):
        return
    con = _conectar()
    try:
        _purgar_si_corresponde(con, forzar=True)
    finally:
        con.close()


def esperar_escritura(timeout: float = 5.0) -> None:
    """Para tests y para el endpoint: espera a que lo encolado esté en la base."""
    limite = time.time() + timeout
    while _cola.unfinished_tasks and time.time() < limite:
        time.sleep(0.02)


# ─── Consultas ───────────────────────────────────────────────────────────────

_COLUMNAS = ("id, ts, proveedor, env, origen, categoria, motivo, detalle, equipo, envio, alert_id,"
             " latitud, longitud, ingest_id")


def consultar(limite: int = 100) -> dict:
    """Resumen de Diagnóstico y Salud: conteo por integración, origen y motivo, y los últimos."""
    esperar_escritura(1.0)
    limite = max(1, min(int(limite), 1000))
    dias, max_filas = retencion_vigente()
    if not os.path.exists(_ruta()):
        return {"resumen": [], "ultimos": [], "por_equipo": [], "perdidos": _perdidos,
                "retencion_dias": dias, "max_filas": max_filas}
    con = _conectar()
    try:
        con.row_factory = sqlite3.Row
        resumen = [dict(r) for r in con.execute(
            "SELECT proveedor, env, origen, categoria, motivo, COUNT(*) AS total, MAX(ts) AS ultimo"
            " FROM descartes GROUP BY proveedor, env, origen, categoria, motivo"
            " ORDER BY total DESC, ultimo DESC")]
        ultimos = [dict(r) for r in con.execute(
            "SELECT ts, proveedor, env, origen, categoria, motivo, detalle, equipo, envio, alert_id, latitud, longitud"
            " FROM descartes ORDER BY id DESC LIMIT ?", (limite,))]
        # B-8 (v1.9.9): los "sin patente" contados por equipo, para avisarle al
        # prestador cuáles le faltan. En SQLite, con MAX() las columnas sueltas
        # (latitud, longitud) salen de la fila del último descarte.
        por_equipo = [dict(r) for r in con.execute(
            "SELECT proveedor, env, equipo, detalle, COUNT(*) AS total, MAX(ts) AS ultimo,"
            " latitud, longitud FROM descartes WHERE origen = 'mapeo'"
            " GROUP BY proveedor, env, equipo, detalle ORDER BY total DESC, ultimo DESC LIMIT 200")]
    finally:
        con.close()
    return {"resumen": resumen, "ultimos": ultimos, "por_equipo": por_equipo, "perdidos": _perdidos,
            "retencion_dias": dias, "max_filas": max_filas}


Filtros = namedtuple("Filtros", "desde hasta proveedor env categorias tipo texto")


def _donde(f: Filtros, con_categoria: bool = True) -> tuple[str, list]:
    """WHERE parametrizado. Los valores ya vienen validados por la API."""
    partes, params = ["ts >= ?", "ts < ?"], [f.desde, f.hasta]
    if f.proveedor:
        partes.append("proveedor = ?")
        params.append(f.proveedor)
    if f.env:
        partes.append("env = ?")
        params.append(f.env)
    if con_categoria:
        categorias = list(f.categorias or [])
        if f.tipo in TIPOS:
            del_tipo = set(categorias_del_tipo(f.tipo))
            categorias = [c for c in categorias if c in del_tipo] if categorias else sorted(del_tipo)
            if not categorias:
                categorias = ["__ninguna__"]
        if categorias:
            partes.append(f"categoria IN ({', '.join('?' * len(categorias))})")
            params.extend(categorias)
    if f.texto:
        patron = "%" + f.texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        partes.append("(equipo LIKE ? ESCAPE '\\' OR envio LIKE ? ESCAPE '\\')")
        params.extend([patron, patron])
    return " AND ".join(partes), params


def _sin_base() -> bool:
    esperar_escritura(1.0)
    return not os.path.exists(_ruta())


def mas_viejo() -> float | None:
    """Fecha (epoch) del descarte más viejo disponible."""
    if _sin_base():
        return None
    con = _conectar()
    try:
        return con.execute("SELECT MIN(ts) FROM descartes").fetchone()[0]
    finally:
        con.close()


def buscar(f: Filtros, limite: int, desplazamiento: int) -> dict:
    """Filas paginadas, total y conteo por categoría del rango (sin el filtro de categoría ni de tipo)."""
    if _sin_base():
        return {"filas": [], "total": 0, "por_categoria": {}, "mas_viejo": None}
    con = _conectar()
    try:
        con.row_factory = sqlite3.Row
        donde, params = _donde(f)
        total = con.execute(f"SELECT COUNT(*) FROM descartes WHERE {donde}", params).fetchone()[0]
        filas = [dict(r) for r in con.execute(
            f"SELECT {_COLUMNAS} FROM descartes WHERE {donde} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            params + [limite, desplazamiento])]
        donde_rango, params_rango = _donde(f, con_categoria=False)
        por_categoria = {r[0]: r[1] for r in con.execute(
            f"SELECT categoria, COUNT(*) FROM descartes WHERE {donde_rango} GROUP BY categoria", params_rango)}
        viejo = con.execute("SELECT MIN(ts) FROM descartes").fetchone()[0]
    finally:
        con.close()
    return {"filas": filas, "total": total, "por_categoria": por_categoria, "mas_viejo": viejo}


def por_equipo(f: Filtros, limite: int) -> dict:
    """Agrupado por integración, equipo y categoría: cantidad, primero, último y último envío."""
    if _sin_base():
        return {"grupos": [], "total": 0}
    con = _conectar()
    try:
        con.row_factory = sqlite3.Row
        donde, params = _donde(f)
        # En SQLite, con MAX() las columnas sueltas (envio) salen de la fila del último.
        consulta = (f"SELECT proveedor, env, equipo, categoria, COUNT(*) AS cantidad, MIN(ts) AS primero,"
                    f" MAX(ts) AS ultimo, envio AS ultimo_envio FROM descartes WHERE {donde}"
                    f" GROUP BY proveedor, env, equipo, categoria")
        total = con.execute(f"SELECT COUNT(*) FROM ({consulta})", params).fetchone()[0]
        grupos = [dict(r) for r in con.execute(
            f"{consulta} ORDER BY cantidad DESC, ultimo DESC LIMIT ?", params + [limite])]
    finally:
        con.close()
    return {"grupos": grupos, "total": total}


def filas_para_exportar(f: Filtros, tope: int):
    """(filas, recortado): hasta `tope` filas, de la más nueva a la más vieja."""
    if _sin_base():
        return [], False
    con = _conectar()
    try:
        con.row_factory = sqlite3.Row
        donde, params = _donde(f)
        filas = [dict(r) for r in con.execute(
            f"SELECT {_COLUMNAS} FROM descartes WHERE {donde} ORDER BY ts DESC, id DESC LIMIT ?",
            params + [tope + 1])]
    finally:
        con.close()
    return filas[:tope], len(filas) > tope


def descarte(id_: int) -> dict | None:
    if _sin_base():
        return None
    con = _conectar()
    try:
        con.row_factory = sqlite3.Row
        fila = con.execute(f"SELECT {_COLUMNAS} FROM descartes WHERE id = ?", (id_,)).fetchone()
    finally:
        con.close()
    return dict(fila) if fila else None


def reset() -> None:
    """Para los tests: vacía la cola y el contador de perdidos."""
    global _perdidos, _ultima_purga
    esperar_escritura(2.0)
    _perdidos = 0
    _ultima_purga = 0.0
    _categorias_desconocidas.clear()
