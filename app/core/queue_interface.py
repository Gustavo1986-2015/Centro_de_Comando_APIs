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
    async def schedule_retry(
        self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str,
        retry_count: int, next_retry_at: datetime
    ) -> None:
        """
        Programa un reintento para el evento, actualizando el conteo de reintentos
        y la fecha/hora en la que volverá a ser elegible.
        """
        pass
