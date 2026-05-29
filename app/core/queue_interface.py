from abc import ABC, abstractmethod
from typing import List, Any
from datetime import datetime

class MessageQueueInterface(ABC):
    """
    Interfaz abstracta unificada para la cola de mensajes y persistencia
    de eventos telemáticos normalizados en el Hub.
    """

    @abstractmethod
    async def get_pending_batch(self, provider: str, env: str, limit: int = 150) -> List[Any]:
        """
        Obtiene una lista de eventos que están en estado 'pending' y listos
        para ser despachados (incluye filtro de tiempo para los reintentos).
        """
        pass

    @abstractmethod
    async def mark_as_sent(
        self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str
    ) -> None:
        """
        Marca un evento como enviado de forma exitosa a Recurso Confiable.
        """
        pass

    @abstractmethod
    async def mark_as_failed(
        self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str
    ) -> None:
        """
        Marca un evento como fallido definitivo.
        """
        pass

    @abstractmethod
    async def get_pending_count(self, provider: str, env: str) -> int:
        """Retorna la cantidad total de eventos encolados listos para procesar."""
        pass

    @abstractmethod
    async def schedule_retry(
        self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str,
        retry_count: int, next_retry_at: datetime
    ) -> None:
        """
        Programa un reintento para el evento, actualizando el conteo de reintentos
        y la fecha/hora en la que volverá a ser elegible.
        """
        pass

    @abstractmethod
    async def mark_batch_as_sent(self, provider: str, env: str, updates: List[dict]) -> None:
        """Actualiza el estado a 'sent' para múltiples eventos en una sola transacción."""
        pass

    @abstractmethod
    async def mark_batch_as_failed(self, provider: str, env: str, updates: List[dict]) -> None:
        """Actualiza el estado a 'failed' para múltiples eventos en una sola transacción."""
        pass

    @abstractmethod
    async def schedule_batch_retry(self, provider: str, env: str, updates: List[dict]) -> None:
        """Programa el reintento para múltiples eventos en una sola transacción."""
        pass
