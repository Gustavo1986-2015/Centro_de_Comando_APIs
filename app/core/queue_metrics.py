"""
Métrica de espera en la cola de ingesta.

El tiempo de respuesta del endpoint PUSH no sirve para saber si el sistema
aguanta: el handler solo encola y responde, así que mide sub-milisegundo aunque
el consumidor esté atrasado. Bajo carga sostenida, esa métrica seguiría en verde
mientras el pipeline real se hunde.

Lo que sí lo revela es la ESPERA EN COLA: cuánto pasa entre que un payload se
recibe y se persiste. Si el consumidor sigue el ritmo, son milisegundos; si se
atrasa, crece sin techo aunque el endpoint siga respondiendo al instante.

Se mide con una ventana móvil en memoria, sin tocar la base: el propósito es
detectar el atraso, y consultar disco para medirlo lo agravaría.
"""
import logging
import statistics
import threading
import time

logger = logging.getLogger(__name__)

# Ventana de muestras. Con caudales altos, 5.000 cubren varios minutos y
# alcanzan para un percentil representativo sin que la memoria crezca.
_MAX_MUESTRAS = 5000

# Umbral a partir del cual la espera deja de ser normal y pasa a indicar que el
# consumidor no sigue el ritmo de entrada. Un lote se drena cada 0,5 s, así que
# esperas por encima de unos pocos segundos significan acumulación real.
UMBRAL_ALERTA_MS = 5000

# Espaciado mínimo entre alertas, para no repetir la misma advertencia en cada
# lote mientras dura la congestión.
_INTERVALO_ALERTA_SEG = 60

_muestras: dict[str, list[float]] = {}
_ocupacion: dict[str, tuple[int, int]] = {}
_ultima_alerta: dict[str, float] = {}
_lock = threading.Lock()


def registrar_espera_cola(provider: str, esperas_ms: list, profundidad: int = 0,
                          capacidad: int = 0) -> None:
    """
    Registra la espera de un lote recién drenado.

    `profundidad` y `capacidad` describen el estado de la cola al momento de
    consumir: sirven para distinguir una espera alta puntual de una cola que se
    está llenando de verdad.
    """
    if not esperas_ms:
        return

    clave = provider.lower()
    ahora = time.time()
    alertar = False
    peor = max(esperas_ms)

    with _lock:
        acumuladas = _muestras.setdefault(clave, [])
        acumuladas.extend(esperas_ms)
        if len(acumuladas) > _MAX_MUESTRAS:
            del acumuladas[: len(acumuladas) - _MAX_MUESTRAS]

        _ocupacion[clave] = (profundidad, capacidad)

        if peor > UMBRAL_ALERTA_MS:
            if ahora - _ultima_alerta.get(clave, 0.0) >= _INTERVALO_ALERTA_SEG:
                _ultima_alerta[clave] = ahora
                alertar = True

    if alertar:
        ocupacion_pct = (profundidad / capacidad * 100) if capacidad else 0.0
        logger.warning(
            f"[{provider.upper()}] La cola de ingesta se está atrasando: "
            f"un evento esperó {peor / 1000:.1f}s entre recibirse y guardarse. "
            f"Cola al {ocupacion_pct:.1f}% ({profundidad}/{capacidad}). "
            f"Si persiste, el consumidor no está siguiendo el ritmo de entrada."
        )


def obtener_estadisticas(provider: str) -> dict:
    """Resumen de la espera en cola, para exponer en el panel."""
    clave = provider.lower()
    with _lock:
        muestras = list(_muestras.get(clave, []))
        profundidad, capacidad = _ocupacion.get(clave, (0, 0))

    if not muestras:
        return {
            "muestras": 0, "media_ms": None, "p95_ms": None, "max_ms": None,
            "profundidad": profundidad, "capacidad": capacidad, "ocupacion_pct": 0.0,
        }

    muestras.sort()
    return {
        "muestras": len(muestras),
        "media_ms": round(statistics.mean(muestras), 2),
        "p95_ms": round(muestras[int(len(muestras) * 0.95)], 2),
        "max_ms": round(muestras[-1], 2),
        "profundidad": profundidad,
        "capacidad": capacidad,
        "ocupacion_pct": round(profundidad / capacidad * 100, 2) if capacidad else 0.0,
    }


def reset():
    """Solo para tests."""
    with _lock:
        _muestras.clear()
        _ocupacion.clear()
        _ultima_alerta.clear()
