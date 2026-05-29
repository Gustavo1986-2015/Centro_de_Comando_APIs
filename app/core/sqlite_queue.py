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

    async def get_pending_count(self, provider: str, env: str) -> int:
        db = get_session(provider, env)
        try:
            now_time = datetime.now()
            return db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.status == "pending",
                or_(
                    NormalizedRCEvent.next_retry_at == None,
                    NormalizedRCEvent.next_retry_at <= now_time
                )
            ).count()
        finally:
            db.close()

    async def get_pending_batch(self, provider: str, env: str, limit: int = 150) -> List[Any]:
        db = get_session(provider, env)
        try:
            now_time = datetime.now()
            query = db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.status == "pending",
                or_(
                    NormalizedRCEvent.next_retry_at == None,
                    NormalizedRCEvent.next_retry_at <= now_time
                )
            ).order_by(NormalizedRCEvent.id.asc()).limit(limit)
            
            events = query.all()
            
            if events:
                event_ids = [ev.id for ev in events]
                
                for ev in events:
                    db.expunge(ev)
                    ev.status = "processing"
                    
                db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id.in_(event_ids)).update(
                    {"status": "processing"}, synchronize_session=False
                )
                db.commit()
                
            return events
        finally:
            db.close()

    async def mark_batch_as_sent(self, provider: str, env: str, updates: List[dict]) -> None:
        db = get_session(provider, env)
        try:
            for u in updates:
                db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id == u['event_id']).update({
                    "status": "sent",
                    "rc_response": u['rc_response'],
                    "job_id": u['job_id'],
                    "rc_latency_sec": u['elapsed_sec'],
                    "retry_count": 0,
                    "next_retry_at": None
                }, synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def mark_batch_as_failed(self, provider: str, env: str, updates: List[dict]) -> None:
        db = get_session(provider, env)
        try:
            for u in updates:
                db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id == u['event_id']).update({
                    "status": "failed",
                    "rc_response": u['rc_response'],
                    "job_id": u['job_id'],
                    "rc_latency_sec": u['elapsed_sec']
                }, synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def schedule_batch_retry(self, provider: str, env: str, updates: List[dict]) -> None:
        db = get_session(provider, env)
        try:
            for u in updates:
                db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id == u['event_id']).update({
                    "status": "pending",
                    "rc_response": u['rc_response'],
                    "job_id": u['job_id'],
                    "rc_latency_sec": u['elapsed_sec'],
                    "retry_count": u['retry_count'],
                    "next_retry_at": u['next_retry_at']
                }, synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def mark_as_sent(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str) -> None:
        await self.mark_batch_as_sent(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id}])

    async def mark_as_failed(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str) -> None:
        await self.mark_batch_as_failed(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id}])

    async def schedule_retry(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str, retry_count: int, next_retry_at: datetime) -> None:
        await self.schedule_batch_retry(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id, "retry_count": retry_count, "next_retry_at": next_retry_at}])
