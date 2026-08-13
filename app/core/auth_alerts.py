"""
Registro agrupado de rechazos de autenticación en los webhooks.

Un 401 sostenido es el síntoma exacto de una integración mal configurada: el
proveedor cree que está enviando y nosotros no recibimos nada. Hasta ahora eso
era invisible salvo en los access logs de uvicorn, que muestran el código pero
no la causa.

Registrar una línea por rechazo tampoco sirve: a decenas de mensajes por segundo
la avalancha esconde el problema tanto como el silencio. Por eso se agrupa:

  · La PRIMERA ocurrencia se emite de inmediato, para que salte apenas empieza.
  · Después, un resumen periódico con el acumulado mientras sigan llegando.
  · Al cesar los rechazos, una línea de cierre indicando cuántos hubo en total.

Con ese esquema, 5.320 rechazos en tres minutos producen tres o cuatro líneas
en lugar de 5.320 o ninguna.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

# Cada cuánto emitir el resumen mientras el problema persiste.
INTERVALO_RESUMEN_SEG = 60

# Tras este tiempo sin rechazos, se considera resuelto y se cierra el episodio.
SILENCIO_PARA_CERRAR_SEG = 120


class _Episodio:
    """Racha de rechazos consecutivos para una misma integración y motivo."""

    __slots__ = ("total", "desde", "ultimo", "ultimo_resumen", "reportado_en_resumen")

    def __init__(self, ahora: float):
        self.total = 0
        self.desde = ahora
        self.ultimo = ahora
        self.ultimo_resumen = ahora
        self.reportado_en_resumen = 0


_episodios: dict[tuple[str, str, str], _Episodio] = {}
_lock = threading.Lock()


def registrar_rechazo(provider: str, env: str, motivo: str, detalle: str = "") -> None:
    """
    Contabiliza un rechazo de autenticación y emite log solo cuando aporta.

    `motivo` agrupa el episodio: rechazos por clave inválida y por clave
    ausente son problemas distintos y merecen su propio recuento.
    """
    clave = (provider.lower(), env.lower(), motivo)
    ahora = time.time()

    emitir_inicio = False
    emitir_resumen = False
    total = 0
    nuevos = 0
    duracion = 0.0

    with _lock:
        episodio = _episodios.get(clave)

        # Un hueco largo cierra el episodio anterior y abre uno nuevo, para que
        # un problema resuelto y otro posterior no se cuenten juntos.
        if episodio is None or (ahora - episodio.ultimo) > SILENCIO_PARA_CERRAR_SEG:
            episodio = _Episodio(ahora)
            _episodios[clave] = episodio
            emitir_inicio = True

        episodio.total += 1
        episodio.ultimo = ahora
        total = episodio.total

        if not emitir_inicio and (ahora - episodio.ultimo_resumen) >= INTERVALO_RESUMEN_SEG:
            emitir_resumen = True
            nuevos = episodio.total - episodio.reportado_en_resumen
            duracion = ahora - episodio.desde
            episodio.ultimo_resumen = ahora
            episodio.reportado_en_resumen = episodio.total

    if emitir_inicio:
        logger.warning(
            f"[{provider.upper()}-{env}] Petición rechazada: {motivo}. "
            f"{detalle} "
            f"Si el proveedor está enviando, no se está recibiendo nada."
        )
    elif emitir_resumen:
        logger.warning(
            f"[{provider.upper()}-{env}] {nuevos} rechazo(s) más por {motivo} "
            f"en los últimos {INTERVALO_RESUMEN_SEG}s "
            f"({total} en total desde hace {duracion / 60:.0f} min). {detalle}"
        )


def cerrar_episodios_resueltos() -> int:
    """
    Cierra los episodios sin actividad reciente, dejando constancia del total.

    Se llama desde el watchdog: sin esto, un problema que se resuelve no deja
    registro de cuánto duró ni cuántas peticiones se perdieron.
    """
    ahora = time.time()
    cerrados = []

    with _lock:
        for clave, episodio in list(_episodios.items()):
            if (ahora - episodio.ultimo) > SILENCIO_PARA_CERRAR_SEG:
                cerrados.append((clave, episodio.total, episodio.ultimo - episodio.desde))
                del _episodios[clave]

    for (provider, env, motivo), total, duracion in cerrados:
        logger.info(
            f"[{provider.upper()}-{env}] Cesaron los rechazos por {motivo}. "
            f"Total del episodio: {total} petición(es) en {duracion / 60:.1f} min."
        )

    return len(cerrados)


def resumen_actual() -> list[dict]:
    """Episodios en curso, para exponerlos en el panel de salud."""
    ahora = time.time()
    with _lock:
        return [
            {
                "provider": provider,
                "env": env,
                "motivo": motivo,
                "total": ep.total,
                "desde_hace_seg": round(ahora - ep.desde, 1),
            }
            for (provider, env, motivo), ep in _episodios.items()
        ]


def reset():
    """Solo para tests."""
    with _lock:
        _episodios.clear()
