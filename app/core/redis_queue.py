import os
import logging
from typing import List, Any
from datetime import datetime

from app.core.queue_interface import MessageQueueInterface

logger = logging.getLogger(__name__)

class RedisQueue(MessageQueueInterface):
    """
    Implementación base para Cola de Mensajes usando Redis Streams.
    
    ESTRUCTURA PROPUESTA (Para futura migración completa):
    1. Stream Principal: "gps:{provider}:{env}:events"
       - Campos: event_id, chassis, status, raw_json, etc.
       - Comandos: XADD (encolar), XREADGROUP (consumir en bloque), XACK (confirmar)
       - Consumer Group: "workers" para distribuir la carga dinámicamente entre múltiples hilos/servidores.
    
    2. Cola de Reintentos (Sorted Set): "gps:{provider}:{env}:retry_queue"
       - Score: timestamp unix de 'next_retry_at'
       - Member: event_id
       - Comandos: ZADD (programar), ZRANGEBYSCORE (obtener listos para reintento)
       
    3. Hash Auxiliar: "gps:{provider}:{env}:event:{event_id}"
       - Opcional: Para evitar sobrecargar el Stream con JSONs masivos.
    """

    def __init__(self):
        # Cargar variables de entorno configurables
        self.host = os.getenv("REDIS_HOST", "localhost")
        self.port = int(os.getenv("REDIS_PORT", "6379"))
        self.password = os.getenv("REDIS_PASSWORD", None)
        self.db = int(os.getenv("REDIS_DB", "0"))
        
        logger.info(
            f"[RedisQueue] Inicializando con host={self.host}, port={self.port}, db={self.db} "
            f"(Password: {'Configurada' if self.password else 'Ninguna'})"
        )
        self.client = None
        
        # Validaciones críticas (Fallo Rápido)
        try:
            import redis
            self.client = redis.Redis(
                host=self.host,
                port=self.port,
                password=self.password,
                db=self.db,
                decode_responses=True
            )
            # Ping para evitar fallos silenciosos
            if not self.client.ping():
                raise ConnectionError(f"Redis en {self.host}:{self.port} no responde al ping.")
        except ImportError:
            logger.critical("[RedisQueue] Librería 'redis' no instalada.")
            raise RuntimeError("RedisQueue requiere: pip install redis")
        except Exception as e:
            logger.warning(f"Excepción silenciada en ejecución: {e}")
            logger.critical(f"[RedisQueue] Falla crítica de conexión Redis: {e}")
            raise ConnectionError(f"No se pudo conectar a Redis: {e}")
            
    def health_check(self) -> dict:
        """Chequeo de salud nativo del motor Redis."""
        try:
            is_up = self.client.ping()
            return {"status": "ok" if is_up else "down", "connected": is_up}
        except Exception as e:
            logger.warning(f"Excepción silenciada en ejecución: {e}")
            return {"status": "error", "error": str(e), "connected": False}

    async def get_pending_count(self, provider: str, env: str) -> int:
        """
        PSEUDOCÓDIGO STREAMS:
        INFO STREAM gps:{provider}:{env}:events 
        Retorna la longitud aproximada del stream.
        """
        return 0

    async def get_pending_batch(self, provider: str, env: str, limit: int = 150) -> List[Any]:
        raise NotImplementedError(
            "[RedisQueue] get_pending_batch no está implementado. "
            "Usar queue_backend='sqlite' hasta completar la migración a Redis Streams."
        )

    async def mark_batch_as_sent(self, provider: str, env: str, updates: List[dict]) -> None:
        raise NotImplementedError(
            "[RedisQueue] mark_batch_as_sent no está implementado. "
            "Usar queue_backend='sqlite' hasta completar la migración a Redis Streams."
        )

    async def mark_batch_as_failed(self, provider: str, env: str, updates: List[dict]) -> None:
        raise NotImplementedError(
            "[RedisQueue] mark_batch_as_failed no está implementado. "
            "Usar queue_backend='sqlite' hasta completar la migración a Redis Streams."
        )

    async def schedule_batch_retry(self, provider: str, env: str, updates: List[dict]) -> None:
        raise NotImplementedError(
            "[RedisQueue] schedule_batch_retry no está implementado. "
            "Usar queue_backend='sqlite' hasta completar la migración a Redis Streams."
        )

    # Mantener retrocompatibilidad con métodos únicos heredados de la interfaz base si aplica
    async def mark_as_sent(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str) -> None:
        await self.mark_batch_as_sent(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id}])

    async def mark_as_failed(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str) -> None:
        await self.mark_batch_as_failed(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id}])

    async def schedule_retry(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str, retry_count: int, next_retry_at: datetime) -> None:
        await self.schedule_batch_retry(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id, "retry_count": retry_count, "next_retry_at": next_retry_at}])
