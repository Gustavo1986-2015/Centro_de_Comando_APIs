from typing import List, Any
from datetime import datetime
from sqlalchemy import or_

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.core.queue_interface import MessageQueueInterface

class SQLiteQueue(MessageQueueInterface):
    """
    Implementación concreta de la cola de mensajes usando SQLite y SQLAlchemy.
    Mantiene la compatibilidad con el esquema de almacenamiento relacional del Hub.
    """

    async def get_pending_batch(self, provider: str, env: str, limit: int = 150) -> List[Any]:
        db = get_session(provider, env)
        try:
            now_time = datetime.now()
            # Buscar eventos pendientes que ya cumplieron el tiempo de retroceso/reintento (o que no tienen reintentos programados)
            query = db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.status == "pending",
                or_(
                    NormalizedRCEvent.next_retry_at == None,
                    NormalizedRCEvent.next_retry_at <= now_time
                )
            ).order_by(NormalizedRCEvent.id.asc()).limit(limit)
            
            events = query.all()
            
            # Desvincular de la sesión para evitar DetachedInstanceError al cerrar la conexión
            for ev in events:
                db.expunge(ev)
                
            return events
        finally:
            db.close()

    async def mark_as_sent(
        self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str
    ) -> None:
        db = get_session(provider, env)
        try:
            event = db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id == event_id).first()
            if event:
                event.status = "sent"
                event.rc_response = rc_response
                event.job_id = job_id
                event.rc_latency_sec = elapsed_sec
                # Resetear reintentos al enviar con éxito
                event.retry_count = 0
                event.next_retry_at = None
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def mark_as_failed(
        self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str
    ) -> None:
        db = get_session(provider, env)
        try:
            event = db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id == event_id).first()
            if event:
                event.status = "failed"
                event.rc_response = rc_response
                event.job_id = job_id
                event.rc_latency_sec = elapsed_sec
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def schedule_retry(
        self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str,
        retry_count: int, next_retry_at: datetime
    ) -> None:
        db = get_session(provider, env)
        try:
            event = db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id == event_id).first()
            if event:
                event.status = "pending"
                event.rc_response = rc_response
                event.job_id = job_id
                event.rc_latency_sec = elapsed_sec
                event.retry_count = retry_count
                event.next_retry_at = next_retry_at
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
