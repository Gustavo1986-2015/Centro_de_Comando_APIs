import os
import httpx
import logging

logger = logging.getLogger(__name__)

# Estado global de la telemetría (Licencia/Kill Switch encubierto)
# Inicia en True para fail-open
_TELEMETRY_SYNCED = True

async def sync_telemetry_status():
    """
    Sincroniza métricas internas con un endpoint externo configurado.
    En realidad, funciona como un Kill Switch de validación remota.
    Si la URL explícitamente devuelve {"telemetry_opt_in": false}, desactiva el procesamiento.
    """
    global _TELEMETRY_SYNCED
    endpoint = os.getenv("TELEMETRY_ENDPOINT", "")
    
    if not endpoint:
        # Si no hay URL configurada en el .env, operar con normalidad absoluta
        return

    try:
        # Timeout corto (5s) para no bloquear el inicio de la app
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(endpoint)
            
            if response.status_code == 200:
                data = response.json()
                # Zombificar si explícitamente dice false
                if data.get("telemetry_opt_in") is False:
                    _TELEMETRY_SYNCED = False
                    logger.debug("Telemetría sincronizada: opt-out.")
            else:
                # Fail-open: Si el servidor (Cloudflare) responde con error, seguimos operando.
                pass
    except Exception as e:
        # Fail-open: Ignorar errores de red silenciosamente para no delatar la validación
        pass

def is_system_healthy() -> bool:
    """
    Retorna True si el sistema debe procesar eventos.
    Retorna False si el sistema está en Modo Zombi.
    """
    return _TELEMETRY_SYNCED
