"""
Lectura y streaming de los logs de la aplicación hacia el dashboard.

Motivación: diagnosticar un problema en producción obliga hoy a entrar por SSH al
servidor y hacer `docker logs`. Esta consola expone los mismos logs en el panel,
con filtros por nivel y por texto.

SEGURIDAD: los logs contienen URLs con credenciales de los proveedores
(access_token, signature, password en query string). Antes de enviar cualquier
línea al navegador se enmascaran. El enmascarado es genérico por nombre de
parámetro, así que cubre proveedores actuales y futuros sin cambios.
"""
import os
import json
import asyncio
import logging
from collections import deque

logger = logging.getLogger(__name__)

# Misma ruta que usa el logger. Configurable por entorno para poder aislar
# el log de la suite de tests del de la aplicación.
LOG_FILE = os.getenv("LOG_FILE_PATH", os.path.join("logs", "app.jsonl"))

# El enmascarado vive en app/core/secretos.py (v1.9.9): uno solo para la
# consola del panel, el archivo de log, la píldora de salud y los errores.


def mask_secrets(text: str) -> str:
    """
    Reemplaza el valor de cualquier parámetro sensible por su versión
    enmascarada: los 2 primeros y los 2 últimos caracteres ("A1••••02").

    v1.9.9 (B-12): un solo enmascarado para la consola del panel, el archivo de
    log, la píldora de salud y los errores (app/core/secretos.py). Antes la
    consola dejaba 4 caracteres y los demás lugares no enmascaraban nada.
    """
    from app.core.secretos import enmascarar
    return enmascarar(text)


def _parse_line(raw: str) -> dict | None:
    """
    Convierte una línea del archivo en un registro para el frontend.

    El logger escribe JSON por línea, pero puede haber líneas sueltas (tracebacks,
    salida de librerías). Esas se devuelven como nivel INFO sin perder contenido.
    """
    raw = raw.strip()
    if not raw:
        return None

    try:
        data = json.loads(raw)
        return {
            "time": data.get("asctime", ""),
            "level": (data.get("levelname") or "INFO").upper(),
            "logger": data.get("name", ""),
            "message": mask_secrets(data.get("message", "")),
        }
    except json.JSONDecodeError:
        return {"time": "", "level": "INFO", "logger": "", "message": mask_secrets(raw)}


# Orden de severidad, para poder pedir "warning o superior"
LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def read_recent(limit: int = 200, min_level: str | None = None) -> list[dict]:
    """
    Últimas N líneas del log, para poblar la consola al abrirla.

    El filtro por nivel se aplica ACÁ y no en el navegador. Filtrar del lado del
    cliente sobre las últimas N líneas hacía que buscar errores no encontrara
    nada: con tráfico alto, 300 líneas son unos segundos de log, y un error de
    hace dos minutos ya no está en ese buffer. Filtrando en el servidor se
    recorre el archivo entero y se devuelven las N coincidencias más recientes.

    Lee con un deque acotado en lugar de cargar el archivo en memoria: rota a
    diario pero puede pesar cientos de MB bajo carga sostenida.
    """
    if not os.path.exists(LOG_FILE):
        return []

    limit = max(1, min(limit, 2000))
    umbral = LEVEL_RANK.get((min_level or "").upper()) if min_level else None

    resultado: deque = deque(maxlen=limit)
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for linea in f:
                registro = _parse_line(linea)
                if not registro:
                    continue
                if umbral is not None:
                    if LEVEL_RANK.get(registro["level"], 1) < umbral:
                        continue
                resultado.append(registro)
    except OSError as e:
        logger.warning(f"No se pudo leer el archivo de logs: {e}")
        return []

    return list(resultado)


async def tail_log(poll_interval: float = 1.0):
    """
    Generador asíncrono que emite las líneas nuevas del log a medida que aparecen.

    Se posiciona al final del archivo y va leyendo lo que se agrega. Detecta la
    rotación diaria comparando el tamaño: si el archivo encogió, reabre desde
    el principio.
    """
    posicion = None
    inode = None

    while True:
        try:
            if not os.path.exists(LOG_FILE):
                await asyncio.sleep(poll_interval)
                continue

            stat = os.stat(LOG_FILE)

            # Primera vuelta: arrancar desde el final para no reenviar el historial
            if posicion is None:
                posicion = stat.st_size
                inode = stat.st_ino
                await asyncio.sleep(poll_interval)
                continue

            # Rotación diaria o truncado: el archivo es otro o encogió
            if stat.st_ino != inode or stat.st_size < posicion:
                posicion = 0
                inode = stat.st_ino

            if stat.st_size > posicion:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(posicion)
                    nuevas = f.readlines()
                    posicion = f.tell()

                for linea in nuevas:
                    registro = _parse_line(linea)
                    if registro:
                        yield registro

        except OSError as e:
            logger.warning(f"Error leyendo el log para streaming: {e}")

        await asyncio.sleep(poll_interval)
