import os
import logging
import logging.config
import asyncio
from dotenv import dotenv_values

# Librerías de terceros que en DEBUG son inutilizablemente verbosas:
# sqlalchemy imprime cada consulta, zeep cada XML SOAP completo, urllib3 cada
# conexión. Con decenas de mensajes por segundo eso genera gigabytes de log y
# entierra la información propia de la aplicación.
#
# Por eso tienen su propio nivel: LOG_LEVEL=DEBUG deja ver el detalle de nuestro
# código sin que las librerías inunden la salida.
_LIBRERIAS_RUIDOSAS = (
    "sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool",
    "zeep", "zeep.transports", "zeep.wsdl",
    "urllib3", "asyncio", "watchfiles",
)

# httpx se trata aparte: sus líneas muestran cada llamada saliente a los
# proveedores, que es información de diagnóstico valiosa. Se deja en INFO.
_LIBRERIAS_UTILES = ("httpx",)


def get_log_config_from_env(env_path=".env"):
    """
    Lee la configuración de logging del .env.

    LOG_LEVEL       nivel de la aplicación (app.*, uvicorn)     default INFO
    LOG_LEVEL_LIBS  nivel de librerías de terceros ruidosas      default WARNING
    LOG_RETENTION_DAYS  días de rotación del archivo             default 7
    """
    if os.path.exists(env_path):
        env_dict = dotenv_values(env_path)
        level_str = env_dict.get("LOG_LEVEL", "INFO").upper()
        libs_level_str = env_dict.get("LOG_LEVEL_LIBS", "WARNING").upper()
        try:
            retention_days = int(env_dict.get("LOG_RETENTION_DAYS", "7"))
        except ValueError:
            retention_days = 7
    else:
        level_str = "INFO"
        libs_level_str = "WARNING"
        retention_days = 7

    numeric_level = getattr(logging, level_str, logging.INFO)
    return numeric_level, level_str, retention_days, libs_level_str

def get_log_file_path() -> str:
    """
    Ruta del archivo de log. Configurable por entorno para poder aislarlo:
    la suite de tests la redirige a un temporal, así los errores que provoca
    a propósito no se mezclan con los de la aplicación en la consola del panel.
    """
    return os.getenv("LOG_FILE_PATH", os.path.join("logs", "app.jsonl"))


def setup_logging(env_path=".env"):
    archivo_log = get_log_file_path()
    os.makedirs(os.path.dirname(archivo_log) or ".", exist_ok=True)
    numeric_level, level_str, retention_days, libs_level_str = get_log_config_from_env(env_path)
    
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            },
            "json": {
                "class": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S%z"
            },
            "colored": {
                "()": "colorlog.ColoredFormatter",
                "format": "%(log_color)s%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "log_colors": {
                    "DEBUG": "cyan",
                    "INFO": "green",
                    "WARNING": "yellow",
                    "ERROR": "red",
                    "CRITICAL": "red,bg_white"
                }
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "colored",
                "stream": "ext://sys.stdout"
            },
            "file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "formatter": "json",
                "filename": archivo_log,
                "when": "midnight",
                "backupCount": retention_days,
                "encoding": "utf-8"
            }
        },
        "loggers": {
            "": {  # root logger
                "handlers": ["console", "file"],
                "level": level_str,
            },
            "uvicorn": {
                "handlers": ["console", "file"],
                "level": level_str,
                "propagate": False
            },
            "uvicorn.access": {
                "handlers": ["console", "file"],
                "level": level_str,
                "propagate": False
            },
            "uvicorn.error": {
                "handlers": ["console", "file"],
                "level": level_str,
                "propagate": False
            },
            "watchfiles.main": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False
            },
            **{
                nombre: {"level": libs_level_str, "propagate": True}
                for nombre in _LIBRERIAS_RUIDOSAS
            },
            **{
                nombre: {"level": "INFO", "propagate": True}
                for nombre in _LIBRERIAS_UTILES
            },
        }
    }
    
    logging.config.dictConfig(log_config)
    logger = logging.getLogger(__name__)
    logger.info(
        f"Logging inicializado: level={level_str}, libs={libs_level_str}, "
        f"file={archivo_log}, retention={retention_days}d"
    )


async def watch_log_config(env_path=".env"):
    last_mtime = 0
    if os.path.exists(env_path):
        last_mtime = os.path.getmtime(env_path)
        
    logger = logging.getLogger(__name__)
    
    while True:
        try:
            if os.path.exists(env_path):
                current_mtime = os.path.getmtime(env_path)
                if current_mtime > last_mtime:
                    last_mtime = current_mtime
                    
                    numeric_level, new_level_str, _, new_libs_level = get_log_config_from_env(env_path)
                    
                    root_logger = logging.getLogger()
                    old_level = logging.getLevelName(root_logger.level)
                    
                    libs_numeric = getattr(logging, new_libs_level, logging.WARNING)
                    libs_actual = logging.getLogger("sqlalchemy").level

                    if root_logger.level != numeric_level or libs_actual != libs_numeric:
                        root_logger.setLevel(numeric_level)
                        for nombre in ("uvicorn", "uvicorn.access", "uvicorn.error"):
                            logging.getLogger(nombre).setLevel(numeric_level)

                        # Las librerías mantienen su propio nivel: subir el detalle
                        # de la aplicación no debe inundar la salida con SQL y XML.
                        for nombre in _LIBRERIAS_RUIDOSAS:
                            logging.getLogger(nombre).setLevel(libs_numeric)

                        logger.info(
                            f"Logging actualizado en caliente: app {old_level} -> {new_level_str}, "
                            f"libs -> {new_libs_level}"
                        )
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Excepción capturada en logging_config al recargar: {e}")
            
        await asyncio.sleep(5)


def get_current_levels() -> dict:
    """Niveles efectivos actuales, para mostrarlos en el panel."""
    return {
        "app": logging.getLevelName(logging.getLogger().level),
        "libs": logging.getLevelName(logging.getLogger("sqlalchemy").level),
        "available": ["DEBUG", "INFO", "WARNING", "ERROR"],
    }


def set_runtime_level(app_level: str, libs_level: str | None = None) -> dict:
    """
    Cambia el nivel de logging del proceso en caliente, sin reiniciar.

    Pensado para diagnosticar en producción sin acceso al servidor: subir a
    DEBUG desde el panel, observar el problema y volver a INFO.

    El cambio NO se persiste: si el contenedor se reinicia vuelve a lo que diga
    el .env, y si alguien edita ese archivo el observador de configuración lo
    reaplica. Eso es deliberado: un DEBUG olvidado llenaría el disco, y así
    tiene una vuelta atrás garantizada.
    """
    nivel = getattr(logging, app_level.upper(), None)
    if not isinstance(nivel, int):
        raise ValueError(f"Nivel inválido: {app_level}")

    anterior = logging.getLevelName(logging.getLogger().level)

    logging.getLogger().setLevel(nivel)
    for nombre in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(nombre).setLevel(nivel)

    # Las librerías conservan su propio umbral salvo que se pida lo contrario:
    # subir el detalle de la aplicación no debe llenar la salida con SQL y XML.
    libs_aplicado = logging.getLevelName(logging.getLogger("sqlalchemy").level)
    if libs_level:
        nivel_libs = getattr(logging, libs_level.upper(), None)
        if not isinstance(nivel_libs, int):
            raise ValueError(f"Nivel de librerías inválido: {libs_level}")
        for nombre in _LIBRERIAS_RUIDOSAS:
            logging.getLogger(nombre).setLevel(nivel_libs)
        libs_aplicado = libs_level.upper()

    logging.getLogger(__name__).warning(
        f"Nivel de logging cambiado desde el panel: {anterior} -> {app_level.upper()} "
        f"(librerías: {libs_aplicado}). No persiste al reiniciar."
    )
    return {"app": app_level.upper(), "libs": libs_aplicado, "previous": anterior}
