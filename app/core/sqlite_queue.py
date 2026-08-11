from typing import List, Any
from datetime import datetime, timedelta, timezone
from sqlalchemy import or_

from app.database import session_context
from app.models.db_models import NormalizedRCEvent
from app.core.queue_interface import MessageQueueInterface

import asyncio

# ─── Piso del umbral de rescate de eventos atascados ─────────────────────────
# El reaper devuelve a 'pending' los eventos que llevan demasiado tiempo en
# 'processing'. Ese umbral es configurable desde el panel, pero nunca puede
# bajar de este piso: un lote en vuelo permanece en 'processing' solo lo que
# dura un intento SOAP (timeout del orden de decenas de segundos). Un umbral
# menor a este piso arriesga arrebatarle a un worker un lote que todavía está
# despachando. El piso se aplica en la propia cola, así que se respeta
# cualquiera sea el valor configurado y quien llame al método.
_PISO_UMBRAL_RESCATE_SEG = 300


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

    def _liberar_backoff_sync(self, provider: str, env: str, limite: int) -> int:
        """
        Adelanta la salida de eventos que esperan su reintento, en tandas.

        Se usa cuando RC vuelve a responder tras una caída: el backoff existía
        para no insistir sobre un destino caído, y una vez recuperado no tiene
        sentido que un evento siga esperando siete minutos.

        Libera de a tandas y no todo junto: tras una caída larga puede haber
        decenas de miles acumulados, y soltarlos de una golpearía a RC apenas
        se recupera. Los ciclos siguientes van liberando el resto.

        No se toca retry_count: el tope de intentos sigue vigente, así que un
        evento que RC rechaza sistemáticamente no queda dando vueltas.

        `next_retry_at` se compara con el mismo reloj con el que se agenda en el
        processor (`datetime.now()` naive local); ambos lados usan la hora local
        del proceso, así que la comparación es consistente. No se toca acá.

        Retorna cuántos se liberaron.
        """
        with session_context(provider, env) as db:
            ahora = datetime.now()
            ids = [
                fila[0]
                for fila in db.query(NormalizedRCEvent.id)
                .filter(
                    NormalizedRCEvent.status == "pending",
                    NormalizedRCEvent.next_retry_at.isnot(None),
                    NormalizedRCEvent.next_retry_at > ahora,
                )
                .order_by(NormalizedRCEvent.id.asc())
                .limit(limite)
                .all()
            ]
            if not ids:
                return 0

            db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.id.in_(ids)
            ).update({"next_retry_at": None}, synchronize_session=False)
            return len(ids)

    async def liberar_backoff(self, provider: str, env: str, limite: int = 500) -> int:
        return await asyncio.to_thread(self._liberar_backoff_sync, provider, env, limite)

    def _recuperar_estancados_sync(self, provider: str, env: str, umbral_seg: int) -> int:
        """
        Devuelve a la cola los eventos que quedaron atascados en 'processing'.

        Un lote se marca 'processing' al tomarlo y cambia de estado al terminar
        de despacharse. Si el proceso muere entre esos dos momentos —o si una
        excepción corta el camino antes de escribir los resultados— esos eventos
        quedan en un estado del que nadie los saca: no están pendientes, así que
        ningún ciclo los vuelve a tomar, y no están enviados, así que la purga
        tampoco los toca. Se acumulan invisibles.

        Existía una recuperación equivalente, pero solo al arrancar el proceso:
        un lote atascado esperaba al próximo reinicio.

        Reloj: la comparación se hace en UTC naive, el mismo huso con el que se
        escribe `updated_at`. La columna la fija `onupdate=func.now()` al marcar
        'processing' —en SQLite `CURRENT_TIMESTAMP` es siempre UTC— y los demás
        caminos que la tocan (sent/failed/retry) escriben `datetime.now(utc)`
        naive. Antes se comparaba con `datetime.now()` local: como el contenedor
        corre en America/Argentina/Buenos_Aires (UTC-3), el umbral efectivo se
        inflaba en tres horas y un lote atascado esperaba ~3 h de más antes de
        ser rescatado. Debe usarse UTC en ambos lados para que el umbral valga
        lo que dice valer.

        Piso: el umbral nunca baja de `_PISO_UMBRAL_RESCATE_SEG` para no
        arrebatarle a un worker un lote que todavía está en vuelo.

        Retorna cuántos se recuperaron.
        """
        umbral_efectivo = max(_PISO_UMBRAL_RESCATE_SEG, umbral_seg)

        with session_context(provider, env) as db:
            ahora_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            limite = ahora_utc - timedelta(seconds=umbral_efectivo)

            ids = [
                fila[0]
                for fila in db.query(NormalizedRCEvent.id)
                .filter(
                    NormalizedRCEvent.status == "processing",
                    NormalizedRCEvent.updated_at.isnot(None),
                    NormalizedRCEvent.updated_at < limite,
                )
                .all()
            ]
            if not ids:
                return 0

            # Vuelven a 'pending' sin next_retry_at: nunca llegaron a intentarse
            # contra RC, así que no corresponde imponerles una espera.
            db.query(NormalizedRCEvent).filter(
                NormalizedRCEvent.id.in_(ids)
            ).update({"status": "pending"}, synchronize_session=False)
            return len(ids)

    async def recuperar_estancados(self, provider: str, env: str, umbral_seg: int = 600) -> int:
        return await asyncio.to_thread(
            self._recuperar_estancados_sync, provider, env, umbral_seg
        )

    def _mark_batch_as_sent_sync(self, provider: str, env: str, updates: List[dict]) -> None:
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
