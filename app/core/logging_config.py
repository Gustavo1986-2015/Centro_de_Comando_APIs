import os
import logging
import logging.config
import asyncio
from dotenv import dotenv_values

def get_log_config_from_env(env_path=".env"):
    if os.path.exists(env_path):
        env_dict = dotenv_values(env_path)
        level_str = env_dict.get("LOG_LEVEL", "INFO").upper()
        try:
            retention_days = int(env_dict.get("LOG_RETENTION_DAYS", "7"))
        except ValueError:
            retention_days = 7
    else:
        level_str = "INFO"
        retention_days = 7
        
    numeric_level = getattr(logging, level_str, logging.INFO)
    return numeric_level, level_str, retention_days

def setup_logging(env_path=".env"):
    os.makedirs('logs', exist_ok=True)
    numeric_level, level_str, retention_days = get_log_config_from_env(env_path)
    
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
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
                "formatter": "standard",
                "filename": "logs/app.log",
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
            }
        }
    }
    
    logging.config.dictConfig(log_config)
    logger = logging.getLogger(__name__)
    logger.info(f"Logging inicializado: level={level_str}, file=logs/app.log, retention={retention_days}d")


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
                    
                    numeric_level, new_level_str, _ = get_log_config_from_env(env_path)
                    
                    root_logger = logging.getLogger()
                    old_level = logging.getLevelName(root_logger.level)
                    
                    if root_logger.level != numeric_level:
                        root_logger.setLevel(numeric_level)
                        logging.getLogger("uvicorn").setLevel(numeric_level)
                        logging.getLogger("uvicorn.access").setLevel(numeric_level)
                        logging.getLogger("uvicorn.error").setLevel(numeric_level)
                        
                        logger.info(f"LOG_LEVEL actualizado en caliente: {old_level} -> {new_level_str}")
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Excepción capturada en logging_config al recargar: {e}")
            
        await asyncio.sleep(5)
