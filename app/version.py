"""
Versión de la aplicación y cache busting de archivos estáticos.

El número de versión vivía duplicado en varios lugares y quedaba desactualizado
(health.py reportaba 1.2.0 cuando ya se publicaba la 1.5.0). Acá hay una sola
fuente de verdad.

El cache busting de los estáticos usaba un `?v=2` fijo escrito a mano en el
template. Ese número no cambiaba al desplegar, así que el navegador seguía
sirviendo el JS o el CSS anterior y los cambios no se veían hasta forzar una
recarga. Ahora el parámetro se deriva del contenido del archivo: cambia solo
cuando el archivo cambia, y nunca hay que acordarse de subirlo a mano.
"""
import os
import hashlib
import logging

logger = logging.getLogger(__name__)

__version__ = "1.8.0"

_STATIC_DIR = os.path.join("frontend", "static")
_hash_cache: dict[str, str] = {}


def static_version(filename: str) -> str:
    """
    Devuelve un identificador corto y estable del contenido de un estático.

    Se calcula una sola vez por proceso: los archivos no cambian mientras la
    app corre, y en un despliegue con contenedores cada versión arranca de cero.
    Si el archivo no existe, cae a la versión de la app para no romper el render.
    """
    if filename in _hash_cache:
        return _hash_cache[filename]

    ruta = os.path.join(_STATIC_DIR, filename)
    try:
        with open(ruta, "rb") as f:
            digest = hashlib.md5(f.read()).hexdigest()[:8]
    except OSError as e:
        logger.warning(
            f"No se pudo calcular la versión de {filename} para cache busting: {e}. "
            f"Se usa la versión de la app."
        )
        digest = __version__.replace(".", "")

    _hash_cache[filename] = digest
    return digest
