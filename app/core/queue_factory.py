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
    _instance: MessageQueueInterface = None

    @classmethod
    def get_queue_service(cls) -> MessageQueueInterface:
        if cls._instance is None:
            backend = os.getenv("QUEUE_BACKEND", "sqlite").lower()
            if backend == "redis":
                logger.info("[QueueFactory] Inicializando motor de cola REDIS")
                cls._instance = RedisQueue()
            else:
                logger.info("[QueueFactory] Inicializando motor de cola SQLITE (Defecto)")
                cls._instance = SQLiteQueue()
        return cls._instance
