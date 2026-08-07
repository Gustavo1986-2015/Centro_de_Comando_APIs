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
import re
import json
import asyncio
import logging
from collections import deque

logger = logging.getLogger(__name__)

LOG_FILE = os.path.join("logs", "app.jsonl")

# Parámetros cuyo valor nunca debe salir del servidor. Se comparan en minúsculas
# y cubren tanto query strings como pares clave-valor en texto libre.
_SECRET_PARAMS = (
    "access_token", "token", "signature", "password", "pwd", "passwd",
    "api_key", "apikey", "x-api-key", "secret",
    "auth_pass", "webhook_auth_secret",
)

# Cubre las tres formas en que aparecen credenciales en los logs:
#   access_token=XXXX          (query string)
#   "password": "XXXX"         (JSON, con comilla de cierre antes del separador)
#   'signature': 'XXXX'        (repr de dict de Python)
_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(" + "|".join(re.escape(p) for p in _SECRET_PARAMS) + r")"
        r"([\"']?\s*[=:]\s*[\"']?)([^\s&\"',}]+)"
    ),
    # "Authorization: Bearer XXXX" / "Authorization: Basic XXXX".
    # El patrón anterior captura solo el esquema ("Bearer") y dejaría el token
    # a la vista, porque se detiene en el espacio.
    re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9\-\._~\+/=]+)"),
]


def mask_secrets(text: str) -> str:
    """
    Reemplaza el valor de cualquier parámetro sensible por una versión truncada.

    Se conservan los primeros 4 caracteres para poder correlacionar un token con
    el de otra línea sin exponerlo (útil para diagnosticar rotaciones de token).
    """
    if not text:
        return text

    def _replace(m):
        nombre, sep, valor = m.group(1), m.group(2), m.group(3)
        visible = valor[:4] if len(valor) > 8 else ""
        return f"{nombre}{sep}{visible}***"

    for pat in _SECRET_PATTERNS:
        text = pat.sub(_replace, text)
    return text


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


def read_recent(limit: int = 200) -> list[dict]:
    """
    Últimas N líneas del log, para poblar la consola al abrirla.

    Lee el archivo completo con un deque acotado en lugar de cargarlo en memoria:
    el archivo rota a diario pero puede pesar decenas de MB en un día con tráfico.
    """
    if not os.path.exists(LOG_FILE):
        return []

    limit = max(1, min(limit, 2000))
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            ultimas = deque(f, maxlen=limit)
    except OSError as e:
        logger.warning(f"No se pudo leer el archivo de logs: {e}")
        return []

    return [r for r in (_parse_line(l) for l in ultimas) if r]


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
