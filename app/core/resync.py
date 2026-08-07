"""
Señales de resincronización manual de integraciones.

Problema que resuelve: cuando el sincronizador de diccionario de un proveedor
falla, duerme un intervalo (5 min ante error, N horas si tuvo éxito) antes de
reintentar. Si el operador corrige la configuración en el dashboard, hoy debe
esperar ese intervalo o reiniciar el servicio completo.

Este módulo permite despertar al sincronizador de una integración puntual sin
tocar el resto del sistema. Es transversal: funciona igual para cualquier
proveedor, presente o futuro, sin lógica específica por AVL.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

# { "protrack|prod": asyncio.Event }
_SIGNALS: dict[str, asyncio.Event] = {}


def _key(provider: str, env: str) -> str:
    return f"{provider.lower()}|{env.lower()}"


def get_signal(provider: str, env: str) -> asyncio.Event:
    """Devuelve (creando si hace falta) el evento de resync de una integración."""
    k = _key(provider, env)
    if k not in _SIGNALS:
        _SIGNALS[k] = asyncio.Event()
    return _SIGNALS[k]


def request_resync(provider: str, env: str) -> bool:
    """
    Solicita una resincronización inmediata.

    Retorna True si había un worker registrado escuchando. False significa que
    la integración todavía no arrancó su loop (o no existe), y el pedido no
    tiene efecto.
    """
    k = _key(provider, env)
    if k not in _SIGNALS:
        logger.warning(
            f"Resync solicitado para {provider}/{env} pero no hay worker registrado."
        )
        return False
    _SIGNALS[k].set()
    logger.info(f"Resync manual solicitado para {provider}/{env}.")
    return True


async def sleep_or_resync(provider: str, env: str, seconds: float) -> bool:
    """
    Duerme el intervalo indicado, pero se despierta antes si alguien pide un resync.

    Reemplaza a `await asyncio.sleep(N)` en los loops de sincronización.
    Retorna True si despertó por un pedido manual, False si cumplió el tiempo.
    """
    signal = get_signal(provider, env)
    try:
        await asyncio.wait_for(signal.wait(), timeout=seconds)
        signal.clear()
        return True
    except asyncio.TimeoutError:
        return False


def reset():
    """Solo para tests."""
    _SIGNALS.clear()
