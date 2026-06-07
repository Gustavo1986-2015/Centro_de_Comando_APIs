import os
import logging
from app.core.queue_interface import MessageQueueInterface
from app.core.sqlite_queue import SQLiteQueue
from app.core.redis_queue import RedisQueue

logger = logging.getLogger(__name__)

class QueueFactory:
    """
    Factoría para instanciar y obtener de forma dinámica y centralizada
    la implementación de colas activa (SQLite o Redis).
    """
    _instances = {}

    @classmethod
    def get_queue_service(cls, provider: str, env: str) -> MessageQueueInterface:
        key = f"{provider}_{env}"
        if key not in cls._instances:
            backend = "sqlite"
            try:
                from app.database import get_session
                from app.models.config_models import ProviderConfig
                db = get_session("system_config", "global")
                config = db.query(ProviderConfig).filter_by(provider_name=provider, env=env).first()
                if config and config.queue_backend:
                    backend = config.queue_backend.lower()
                db.close()
            except Exception as e:
                logger.error(f"[QueueFactory] No se pudo leer ProviderConfig de la BD para {key}: {e}")
                backend = os.getenv("QUEUE_BACKEND", "sqlite").lower()

            if backend == "redis":
                logger.critical(f"[QueueFactory] ¡ALERTA! RedisQueue está instanciado para {key} pero NO ESTÁ IMPLEMENTADO.")
                logger.info(f"[QueueFactory] Inicializando REDIS para {key}")
                cls._instances[key] = RedisQueue()
            else:
                logger.info(f"[QueueFactory] Inicializando SQLITE para {key}")
                cls._instances[key] = SQLiteQueue()
                
        return cls._instances[key]
