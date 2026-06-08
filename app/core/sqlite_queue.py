from typing import List, Any
from datetime import datetime
from sqlalchemy import or_

from app.database import session_context
from app.models.db_models import NormalizedRCEvent
from app.core.queue_interface import MessageQueueInterface

import asyncio

class SQLiteQueue(MessageQueueInterface):
    """
    Implementación concreta de la cola de mensajes usando SQLite y SQLAlchemy.
    """

    def _get_pending_count_sync(self, provider: str, env: str) -> int:
        with session_context(provider, env) as db:
            now_time = datetime.now()
            return db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.status == "pending",
                or_(
                    NormalizedRCEvent.next_retry_at == None,
                    NormalizedRCEvent.next_retry_at <= now_time
                )
            ).count()

    async def get_pending_count(self, provider: str, env: str) -> int:
        return await asyncio.to_thread(self._get_pending_count_sync, provider, env)

    def _get_pending_batch_sync(self, provider: str, env: str, limit: int) -> List[Any]:
        with session_context(provider, env) as db:
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
                    
                # Marcar atómicamente como "processing" en BD para evitar re-procesamiento.
                # Los objetos `events` retornados al caller tendrán status="pending" (valor leído
                # de BD antes del update), lo cual es inofensivo: el caller no depende del status
                # de los objetos devueltos, solo usa sus datos de payload.
                db.query(NormalizedRCEvent).filter(NormalizedRCEvent.id.in_(event_ids)).update(
                    {"status": "processing"}, synchronize_session=False
                )
            return events

    async def get_pending_batch(self, provider: str, env: str, limit: int = 150) -> List[Any]:
        return await asyncio.to_thread(self._get_pending_batch_sync, provider, env, limit)

    def _mark_batch_as_sent_sync(self, provider: str, env: str, updates: List[dict]) -> None:
        from datetime import timezone
        with session_context(provider, env) as db:
            if updates:
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                mappings = [{
                    "id": u['event_id'],
                    "status": "sent",
                    "rc_response": u['rc_response'],
                    "job_id": u['job_id'],
                    "rc_latency_sec": u['elapsed_sec'],
                    "retry_count": 0,
                    "next_retry_at": None,
                    "updated_at": now_utc
                } for u in updates]
                db.bulk_update_mappings(NormalizedRCEvent, mappings)

    async def mark_batch_as_sent(self, provider: str, env: str, updates: List[dict]) -> None:
        await asyncio.to_thread(self._mark_batch_as_sent_sync, provider, env, updates)

    def _mark_batch_as_failed_sync(self, provider: str, env: str, updates: List[dict]) -> None:
        from datetime import timezone
        with session_context(provider, env) as db:
            if updates:
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                mappings = [{
                    "id": u['event_id'],
                    "status": "failed",
                    "rc_response": u['rc_response'],
                    "job_id": u['job_id'],
                    "rc_latency_sec": u['elapsed_sec'],
                    "updated_at": now_utc
                } for u in updates]
                db.bulk_update_mappings(NormalizedRCEvent, mappings)

    async def mark_batch_as_failed(self, provider: str, env: str, updates: List[dict]) -> None:
        await asyncio.to_thread(self._mark_batch_as_failed_sync, provider, env, updates)

    def _schedule_batch_retry_sync(self, provider: str, env: str, updates: List[dict]) -> None:
        from datetime import timezone
        with session_context(provider, env) as db:
            if updates:
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                mappings = [{
                    "id": u['event_id'],
                    "status": "pending",
                    "rc_response": u['rc_response'],
                    "job_id": u['job_id'],
                    "rc_latency_sec": u['elapsed_sec'],
                    "retry_count": u['retry_count'],
                    "next_retry_at": u['next_retry_at'],
                    "updated_at": now_utc
                } for u in updates]
                db.bulk_update_mappings(NormalizedRCEvent, mappings)

    async def schedule_batch_retry(self, provider: str, env: str, updates: List[dict]) -> None:
        await asyncio.to_thread(self._schedule_batch_retry_sync, provider, env, updates)

    async def mark_as_sent(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str) -> None:
        await self.mark_batch_as_sent(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id}])

    async def mark_as_failed(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str) -> None:
        await self.mark_batch_as_failed(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id}])

    async def schedule_retry(self, provider: str, env: str, event_id: int, elapsed_sec: float, rc_response: str, job_id: str, retry_count: int, next_retry_at: datetime) -> None:
        await self.schedule_batch_retry(provider, env, [{"event_id": event_id, "elapsed_sec": elapsed_sec, "rc_response": rc_response, "job_id": job_id, "retry_count": retry_count, "next_retry_at": next_retry_at}])
