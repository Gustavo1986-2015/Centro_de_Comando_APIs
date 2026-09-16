"""
Red de seguridad de ingesta: ningún evento aceptado se pierde.

EL PROBLEMA QUE RESUELVE

El hub responde 202 Accepted apenas encola el evento en memoria. El INSERT
ocurre después, en otro hilo. Si ese INSERT falla —típicamente por
`database is locked`, porque SQLite admite un solo escritor por archivo— el
lote se descartaba con un log y nada más.

El proveedor lo contabiliza como entregado. En la base no existe. Nadie se
entera. Durante una certificación de alto caudal se perdieron lotes
enteros por esta vía.

LA GARANTÍA

Un evento al que se respondió 202 tiene exactamente tres destinos, y ninguno es
"desapareció":

  1. Está en la base
  2. Está esperando reintento
  3. Está en cuarentena, con el motivo escrito

EL DISEÑO: dos archivos y un registro de confirmados

  pendiente.jsonl    se anexa en la recepción, antes de intentar la base
  confirmados.log    se anexa el ingest_id cuando el INSERT confirmó

Ambos son de solo-anexado. No se borra por posición —eso exigiría reescribir el
archivo entero mientras otro hilo le escribe, que es la parte frágil de
cualquier otro esquema—. Al arrancar se reproduce el pendiente salteando lo ya
confirmado, y recién ahí se compacta, cuando nadie más está escribiendo.

El costo es un anexado por LOTE confirmado, no por evento.

POR QUÉ UN ANEXADOR DEDICADO

`log_raw_payload` abre y cierra un descriptor por payload, despachado como una
tarea de `to_thread` por payload, sobre el mismo executor que usa la
persistencia y las consultas del panel. A 40/s son 40 tareas por segundo
compitiendo con los INSERT.

Sumar otra escritura por evento en ese mismo pool degradaría la ingesta. Por eso
acá hay un hilo propio, un descriptor abierto y un flush por lote.
"""
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DIRECTORIO_BASE = "./db/red_seguridad"

# Cada cuántos segundos el reintentador revisa si hay pendientes.
INTERVALO_REINTENTO_SEG = 15

# Espera creciente entre rondas fallidas, en segundos. Topea en el último valor:
# el reintento NO se rinde, solo se espacia para no castigar una base ocupada.
ESPERAS = (1, 3, 10, 30, 60, 120)

# Intentos con el MISMO error antes de mandar a cuarentena. Solo aplica a
# errores no transitorios: un lock se reintenta para siempre.
MAX_INTENTOS_MISMO_ERROR = 5

# Señales de que la base está ocupada y conviene volver a intentar. Un error de
# datos no está acá: reintentarlo no lo va a arreglar nunca.
SENALES_TRANSITORIAS = (
    "database is locked",
    "database is busy",
    "disk i/o error",
    # El reintento no insertó nada: puede ser contención, no datos malos.
    "no insertó ninguna fila",
)


def es_transitorio(error: Exception) -> bool:
    """
    Si vale la pena reintentar.

    La distinción es la que sostiene todo el diseño: un lock desaparece solo,
    un campo que no entra en su columna no. Reintentar lo segundo para siempre
    consume ciclos y puede tapar la recuperación de lo que sí se puede salvar.
    """
    texto = str(error).lower()
    return any(senal in texto for senal in SENALES_TRANSITORIAS)


def nuevo_ingest_id() -> str:
    """Identificador único asignado en la recepción, antes de responder 202."""
    return uuid.uuid4().hex


def _ruta(provider: str, env: str, nombre: str) -> str:
    carpeta = os.path.join(DIRECTORIO_BASE, f"{provider.lower()}_{env.lower()}")
    os.makedirs(carpeta, exist_ok=True)
    return os.path.join(carpeta, nombre)


def esperar_vaciado_global(timeout: float = 5.0):
    """Espera a que todos los anexadores hayan volcado lo suyo a disco."""
    with _lock_anexadores:
        anexadores = list(_anexadores.values())
    for a in anexadores:
        a.esperar_vaciado(timeout)


def anexador_por_ruta(ruta: str) -> "_Anexador":
    """
    Anexador para una ruta arbitraria, reutilizando el hilo si ya existe.

    Lo usa la auditoría de crudos: antes abría y cerraba un descriptor POR
    PAYLOAD, despachado como una tarea suelta al mismo executor que usa la
    persistencia. A 40 msg/s eran 40 tareas por segundo compitiendo con los
    INSERT por el mismo pool.
    """
    with _lock_anexadores:
        if ruta not in _anexadores:
            _anexadores[ruta] = _Anexador(ruta)
        return _anexadores[ruta]


class _Anexador:
    """
    Escribe a un archivo desde un hilo propio, con el descriptor abierto.

    Un hilo y un descriptor por archivo, en vez de abrir y cerrar por evento
    sobre el executor compartido. Quien llama no espera: encola y sigue.
    """

    def __init__(self, ruta: str):
        self.ruta = ruta
        self._cola: queue.Queue = queue.Queue()
        self._hilo = threading.Thread(target=self._correr, daemon=True,
                                      name=f"anexador-{os.path.basename(ruta)}")
        self._hilo.start()

    def anexar(self, linea: str):
        self._cola.put(linea)

    def esperar_vaciado(self, timeout: float = 5.0):
        """
        Bloquea hasta que lo encolado esté en disco.

        Existe para los tests y para los cierres ordenados: la escritura es
        asíncrona a propósito, así que quien necesite leer el archivo justo
        después de escribir tiene que poder esperarla.
        """
        limite = time.time() + timeout
        while time.time() < limite:
            if self._cola.empty():
                # La cola vacía no garantiza que la última tanda terminó de
                # escribirse: se le da un instante al hilo escritor.
                time.sleep(0.05)
                if self._cola.empty():
                    return True
            time.sleep(0.01)
        return False

    def _correr(self):
        while True:
            try:
                primera = self._cola.get()
                lineas = [primera]
                # Vaciar lo que haya llegado mientras tanto: un flush por lote,
                # no por línea.
                while True:
                    try:
                        lineas.append(self._cola.get_nowait())
                    except queue.Empty:
                        break
                with open(self.ruta, "a", encoding="utf-8") as f:
                    f.write("".join(l if l.endswith("\n") else l + "\n" for l in lineas))
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                # Que falle el anexador no puede tumbar la ingesta. Se registra
                # y se sigue: el evento ya está en la base o va a reintentarse.
                logger.error(f"Anexador {self.ruta} falló al escribir: {e}")
                time.sleep(1)


_anexadores: dict[str, _Anexador] = {}
_lock_anexadores = threading.Lock()


def _anexador(provider: str, env: str, nombre: str) -> _Anexador:
    clave = f"{provider.lower()}_{env.lower()}_{nombre}"
    with _lock_anexadores:
        if clave not in _anexadores:
            _anexadores[clave] = _Anexador(_ruta(provider, env, nombre))
        return _anexadores[clave]


# ─── Registro de lo recibido y lo confirmado ─────────────────────────────────

def registrar_pendiente(provider: str, env: str, ingest_id: str, payload: dict):
    """
    Deja el evento en disco ANTES de intentar la base.

    Este es el momento en que el evento deja de depender de la memoria. Si el
    proceso muere entre el 202 y el INSERT, al arrancar se recupera de acá.
    """
    _anexador(provider, env, "pendiente.jsonl").anexar(json.dumps({
        "ingest_id": ingest_id,
        "recibido": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }, ensure_ascii=False))


def confirmar(provider: str, env: str, ingest_ids: list[str]):
    """Marca como persistidos. Un anexado por lote, no por evento."""
    if not ingest_ids:
        return
    _anexador(provider, env, "confirmados.log").anexar("\n".join(ingest_ids))


def registrar_cuarentena(provider: str, env: str, ingest_id: str,
                         payload: dict, motivo: str):
    """
    Aparta un evento que no va a entrar nunca, CON su motivo.

    No se borra. Si mañana se corrige un bug del mapper, estos eventos son
    reprocesables. Borrarlos sería perder datos igual, solo que decidiéndolo.
    """
    _anexador(provider, env, "cuarentena.jsonl").anexar(json.dumps({
        "ingest_id": ingest_id,
        "apartado": datetime.now(timezone.utc).isoformat(),
        "motivo": motivo,
        "payload": payload,
    }, ensure_ascii=False))
    logger.warning(
        f"CUARENTENA {provider}/{env} | evento {ingest_id} apartado: {motivo}. "
        f"No se reintenta más. El evento queda guardado para reprocesar."
    )


# ─── Recuperación ────────────────────────────────────────────────────────────

def _leer_confirmados(provider: str, env: str) -> set[str]:
    ruta = _ruta(provider, env, "confirmados.log")
    if not os.path.exists(ruta):
        return set()
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip()}
    except OSError as e:
        logger.error(f"No se pudo leer confirmados de {provider}/{env}: {e}")
        return set()


def _leer_cuarentena(provider: str, env: str) -> set[str]:
    ruta = _ruta(provider, env, "cuarentena.jsonl")
    if not os.path.exists(ruta):
        return set()
    ids = set()
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    ids.add(json.loads(linea)["ingest_id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        pass
    return ids


def pendientes_reales(provider: str, env: str) -> list[dict]:
    """
    Lo que quedó sin persistir: el pendiente menos lo confirmado y lo apartado.

    Se recalcula leyendo los archivos en vez de mantener un estado en memoria,
    justamente para que sobreviva a un reinicio.
    """
    ruta = _ruta(provider, env, "pendiente.jsonl")
    if not os.path.exists(ruta):
        return []

    resueltos = _leer_confirmados(provider, env) | _leer_cuarentena(provider, env)
    pendientes, vistos = [], set()
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    reg = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                iid = reg.get("ingest_id")
                if not iid or iid in resueltos or iid in vistos:
                    continue
                vistos.add(iid)
                pendientes.append(reg)
    except OSError as e:
        logger.error(f"No se pudo leer el pendiente de {provider}/{env}: {e}")
    return pendientes


def compactar(provider: str, env: str):
    """
    Reescribe el pendiente dejando solo lo que sigue sin resolver.

    Se llama al arrancar y cuando el pendiente queda vacío: momentos en que la
    reescritura no compite con la ingesta. Nunca en caliente.
    """
    restantes = pendientes_reales(provider, env)
    ruta = _ruta(provider, env, "pendiente.jsonl")
    tmp = ruta + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for reg in restantes:
                f.write(json.dumps(reg, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ruta)

        # El registro de confirmados ya cumplió su función: lo que confirmaba
        # acaba de salir del pendiente.
        conf = _ruta(provider, env, "confirmados.log")
        if os.path.exists(conf):
            os.remove(conf)

        if restantes:
            logger.info(
                f"Red de seguridad {provider}/{env}: compactada, "
                f"{len(restantes)} evento(s) siguen pendientes de persistir."
            )
    except OSError as e:
        logger.error(f"No se pudo compactar la red de seguridad de {provider}/{env}: {e}")


def integraciones_con_pendientes() -> list[tuple[str, str]]:
    """Qué integraciones tienen archivos de red de seguridad en disco."""
    if not os.path.isdir(DIRECTORIO_BASE):
        return []
    salida = []
    for nombre in os.listdir(DIRECTORIO_BASE):
        if "_" not in nombre:
            continue
        provider, _, env = nombre.rpartition("_")
        if provider and env:
            salida.append((provider, env))
    return salida


# ─── Estado para el panel ────────────────────────────────────────────────────

_recuperados: dict[str, int] = {}
_lock_recuperados = threading.Lock()


def sumar_recuperados(provider: str, env: str, cantidad: int):
    if cantidad <= 0:
        return
    with _lock_recuperados:
        clave = f"{provider.lower()}_{env.lower()}"
        _recuperados[clave] = _recuperados.get(clave, 0) + cantidad


# Caché del estado por integración, con vencimiento corto.
#
# `estado()` relee el archivo de pendientes para calcular qué falta, y el panel
# lo consulta en cada refresco. Con el archivo chico —que es lo normal ahora que
# solo se escribe al fallar— eso es barato. Pero si alguna vez se llena, releerlo
# en cada consulta volvería a ser el problema que esta versión vino a corregir.
#
# Cinco segundos: el panel no necesita más frescura que eso, y acota el costo
# sin importar cuánto crezca el archivo.
_CACHE_ESTADO_SEG = 5
_cache_estado: dict[str, tuple[float, dict]] = {}


def estado(provider: str, env: str, usar_cache: bool = True) -> dict:
    """
    Qué mostrar en el panel.

    Un reintento que nunca progresa es exactamente el problema silencioso que
    esta red viene a eliminar: por eso el panel tiene que poder mostrar la
    antigüedad del más viejo, no solo cuántos hay.
    """
    clave = f"{provider.lower()}_{env.lower()}"

    if usar_cache:
        cacheado = _cache_estado.get(clave)
        if cacheado and (time.time() - cacheado[0]) < _CACHE_ESTADO_SEG:
            return cacheado[1]

    pend = pendientes_reales(provider, env)

    antiguedad = None
    if pend:
        try:
            mas_viejo = datetime.fromisoformat(pend[0]["recibido"])
            antiguedad = int(
                (datetime.now(timezone.utc) - mas_viejo).total_seconds()
            )
        except (KeyError, ValueError):
            pass

    with _lock_recuperados:
        recuperados = _recuperados.get(clave, 0)

    resultado = {
        "pendientes": len(pend),
        "antiguedad_mas_viejo_seg": antiguedad,
        "recuperados": recuperados,
        "en_cuarentena": len(_leer_cuarentena(provider, env)),
    }
    _cache_estado[clave] = (time.time(), resultado)
    return resultado


# ─── Reintentador ────────────────────────────────────────────────────────────

_reintentador_corriendo = False


async def reintentar_pendientes(provider: str, env: str, persistir) -> dict:
    """
    Una ronda de reintento sobre lo que quedó sin persistir.

    `persistir` recibe una lista de (payload, ingest_id) y devuelve cuántos
    entraron. Se le pasa desde afuera para no atar este módulo a un proveedor:
    sirve igual para el camino PUSH, el PUSH genérico y el PULL.

    Nadie queda atrás: un error transitorio se reintenta indefinidamente, con
    espera creciente. Solo lo que nunca va a entrar se aparta.
    """
    pendientes = pendientes_reales(provider, env)
    if not pendientes:
        return {"recuperados": 0, "pendientes": 0}

    logger.info(
        f"Red de seguridad {provider}/{env}: reintentando "
        f"{len(pendientes)} evento(s) que no se habían podido persistir."
    )

    recuperados, fallidos = 0, 0
    for intento, espera in enumerate(ESPERAS, start=1):
        if not pendientes:
            break
        lote = [(p["payload"], p["ingest_id"]) for p in pendientes]
        try:
            entraron = await persistir(provider, env, lote)
            # Solo se confirma si la función informó que el INSERT ocurrió.
            # Confirmar a ciegas borraría del pendiente algo que no entró: sería
            # el mismo agujero que esta red viene a tapar, con otra cara.
            if entraron <= 0:
                raise RuntimeError(
                    "El reintento no insertó ninguna fila. No se confirma: "
                    "el evento sigue pendiente."
                )
            confirmar(provider, env, [p["ingest_id"] for p in pendientes])
            recuperados += entraron
            sumar_recuperados(provider, env, entraron)
            logger.info(
                f"Red de seguridad {provider}/{env}: {entraron} evento(s) "
                f"recuperados en el intento {intento}."
            )
            pendientes = []
            break
        except Exception as e:
            fallidos += 1
            if not es_transitorio(e):
                # No se arregla esperando. Se aparta CON el motivo, sin borrar.
                for p in pendientes:
                    registrar_cuarentena(provider, env, p["ingest_id"],
                                         p["payload"], str(e))
                pendientes = []
                break
            if intento >= MAX_INTENTOS_MISMO_ERROR and intento >= len(ESPERAS):
                # Sigue siendo transitorio: no se rinde, espera a la próxima
                # ronda. El evento queda en disco, no se pierde.
                logger.warning(
                    f"Red de seguridad {provider}/{env}: la base sigue ocupada "
                    f"tras {intento} intentos. Se reintenta en la próxima ronda."
                )
                break
            await _dormir(espera)

    if not pendientes_reales(provider, env):
        compactar(provider, env)

    return {"recuperados": recuperados, "pendientes": len(pendientes_reales(provider, env))}


async def _dormir(segundos: float):
    import asyncio
    await asyncio.sleep(segundos)


async def bucle_reintentador(persistir):
    """
    Corre para siempre, revisando todas las integraciones con pendientes.

    Al arrancar recupera lo que haya quedado de una ejecución anterior: ese es
    el requisito central, que el pendiente sobreviva a un reinicio.
    """
    import asyncio

    global _reintentador_corriendo
    if _reintentador_corriendo:
        return
    _reintentador_corriendo = True

    logger.info("Reintentador de la red de seguridad iniciado.")

    # Primera pasada: lo que quedó colgado de la ejecución anterior.
    for provider, env in integraciones_con_pendientes():
        try:
            compactar(provider, env)
            estado_inicial = estado(provider, env)
            if estado_inicial["pendientes"]:
                logger.warning(
                    f"Red de seguridad {provider}/{env}: {estado_inicial['pendientes']} "
                    f"evento(s) quedaron sin persistir de la ejecución anterior. "
                    f"Se recuperan ahora."
                )
        except Exception as e:
            logger.error(f"No se pudo revisar la red de seguridad de {provider}/{env}: {e}")

    while True:
        try:
            for provider, env in integraciones_con_pendientes():
                try:
                    await reintentar_pendientes(provider, env, persistir)
                except Exception as e:
                    logger.error(
                        f"Ronda de reintento fallida en {provider}/{env}: {e}",
                        exc_info=True,
                    )
        except Exception as e:
            logger.error(f"Error en el bucle del reintentador: {e}", exc_info=True)
        await asyncio.sleep(INTERVALO_REINTENTO_SEG)
