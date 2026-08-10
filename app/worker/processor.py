import os
import asyncio
import logging
import time
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.schemas.canonical import RCCanonicalModel
import threading as _threading
from collections import deque, defaultdict

class CircuitBreaker:
    """
    Circuit Breaker para llamadas salientes a Recurso Confiable (SOAP).

    Estados:
      CLOSED   → operación normal, las llamadas pasan
      OPEN     → RC detectado como caído, llamadas bloqueadas hasta recovery_at
      HALF_OPEN → período de prueba: deja pasar 1 llamada para verificar si RC volvió

    Uso:
      cb = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
      if cb.allow_request():
          try:
              resultado = llamar_rc()
              cb.record_success()
          except Exception as e:
              logger.warning(f"Excepción capturada en processor: {e}")
              cb.record_failure()
      else:
          # RC sigue caído, omitir este ciclo
    """

    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        failure_threshold: int = 5,    # fallos consecutivos para abrir el circuito
        recovery_timeout:  int = 60,   # segundos antes del primer intento de prueba
        max_timeout:       int = 600,  # techo del backoff exponencial (10 minutos)
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self.max_timeout       = max_timeout

        self._state            = self.CLOSED
        self._failure_count    = 0
        self._last_failure_at  = 0.0
        self._current_timeout  = recovery_timeout
        self._lock             = _threading.Lock()

        # Momento de la última recuperación (paso de OPEN/HALF_OPEN a CLOSED).
        # Se publica como marca temporal en lugar de notificar por callback: el
        # breaker es único para todos los proveedores, y cada worker necesita
        # enterarse por su cuenta para liberar su propia base.
        self._recovery_ts      = 0.0

    # ── API pública ───────────────────────────────────────────────────────────

    def allow_request(self) -> bool:
        """Retorna True si el circuito permite que pase la llamada."""
        with self._lock:
            if self._state == self.CLOSED:
                return True

            if self._state == self.OPEN:
                if time.time() >= self._last_failure_at + self._current_timeout:
                    self._state = self.HALF_OPEN
                    return True   # dejar pasar el intento de prueba
                return False      # circuito abierto, bloquear

            # HALF_OPEN: ya dejamos pasar un intento de prueba
            return True

    def record_success(self):
        """Llamar después de una respuesta exitosa de RC."""
        with self._lock:
            self._failure_count   = 0
            self._current_timeout = self.recovery_timeout   # resetear backoff
            if self._state != self.CLOSED:
                self._recovery_ts = time.time()
                logger.info(
                    "[CircuitBreaker] RC respondió correctamente — circuito CERRADO. "
                    "Se liberará progresivamente la espera de los eventos en reintento."
                )
            self._state = self.CLOSED

    def recovery_timestamp(self) -> float:
        """
        Momento de la última recuperación de RC, o 0 si nunca se degradó.

        Cada worker compara este valor contra el último que procesó: así todos
        se enteran de la misma recuperación sin que el breaker tenga que
        conocerlos.
        """
        with self._lock:
            return self._recovery_ts

    def record_failure(self):
        """Llamar cuando RC lanza excepción o devuelve error irrecuperable."""
        with self._lock:
            self._failure_count  += 1
            self._last_failure_at = time.time()

            if self._state == self.HALF_OPEN or self._failure_count >= self.failure_threshold:
                # Abrir el circuito con backoff exponencial (techo en max_timeout)
                self._current_timeout = min(
                    self._current_timeout * 2 if self._state == self.OPEN else self.recovery_timeout,
                    self.max_timeout
                )
                self._state = self.OPEN
                logger.warning(
                    f"[CircuitBreaker] RC no responde ({self._failure_count} fallos). "
                    f"Circuito ABIERTO — próximo intento en {self._current_timeout}s."
                )

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == self.OPEN


# Instancia global — un circuit breaker por proceso, compartido entre workers
_rc_circuit_breaker = CircuitBreaker(
    failure_threshold=5,    # 5 fallos consecutivos abren el circuito
    recovery_timeout=60,    # esperar 60s antes del primer intento de prueba
    max_timeout=600,        # máximo 10 minutos entre reintentos
)

from app.services.rc_soap import (
    get_rc_client,
    RCResponseCategory,
    CATEGORIAS_REINTENTABLES,
)

# Tope de reintentos antes de dar el evento por fallido. Con el backoff
# exponencial actual, cuatro intentos cubren unos 25 minutos de indisponibilidad
# de RC; más allá de eso el evento pierde vigencia operativa.
MAX_REINTENTOS = 4
from app.models.config_models import ProviderConfig, DailyStat
from app.core.config_cache import get_settings

logger = logging.getLogger(__name__)

# =====================================================================


# Registro global de eventos para despertar a los workers de forma instantánea
WORKER_TRIGGERS = {}

def trigger_worker(provider: str, env: str):
    """Despierta el worker correspondiente al proveedor y entorno de forma inmediata."""
    key = f"{provider.lower()}_{env.lower()}"
    if key in WORKER_TRIGGERS:
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(WORKER_TRIGGERS[key].set)
        except Exception as e:
            logger.warning(f"Excepción capturada en processor: {e}")

async def send_batch_and_measure(canonical_events, rc_client):
    """
    Llama al cliente SOAP de RC y mide la latencia.
    Integra el Circuit Breaker: si el circuito está abierto, lanza
    una excepción controlada en lugar de intentar la llamada.
    """
    if not _rc_circuit_breaker.allow_request():
        raise ConnectionError(
            f"[CircuitBreaker] RC no disponible — circuito ABIERTO. "
            f"Próximo intento en ~{_rc_circuit_breaker._current_timeout}s."
        )

    start_time = time.time()
    try:
        results = await rc_client.send_events_batch(canonical_events)
        elapsed = time.time() - start_time
        
        # Detectar error silencioso de conexión capturado dentro de rc_client.
        # Se desempaqueta por índice para tolerar tanto la tupla de 4 elementos
        # (con categoría) como la de 3 de versiones anteriores.
        if results and isinstance(results, list) and len(results) > 0:
            primera = results[0]
            success, job_id, raw_response = primera[0], primera[1], primera[2]
            if not success and "rc_conn_err" in str(job_id):
                # Es un error de conexión real a RC
                raise Exception(raw_response)
                
        _rc_circuit_breaker.record_success()
        return results, elapsed
    except ConnectionError:
        # Re-levantar la excepción del circuit breaker (para no sumar fallo doble)
        raise
    except Exception as e:
        logger.warning(f"Excepción capturada en processor: {e}")
        elapsed = time.time() - start_time
        _rc_circuit_breaker.record_failure()
        raise

def get_active_providers():
    db = get_session("system_config", "global")
    try:
        configs = db.query(ProviderConfig).filter(ProviderConfig.is_active == True).all()
        return [{"name": c.provider_name, "env": c.env, "purge_min": c.purge_interval_min} for c in configs]
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        return []
    finally:
        db.close()

def get_next_retry_delay(retry_count: int) -> int:
    """Calcula el retroceso exponencial con Jitter (Full Jitter modificado)."""
    import random
    max_delay = min(300, 10 * int(3.5 ** retry_count))
    min_delay = max(10, max_delay // 2)
    return random.randint(min_delay, max_delay)

# ─── Liberación del backoff tras recuperarse RC ──────────────────────────────
# Cuando RC vuelve a responder, los eventos en reintento siguen esperando su
# backoff individual (hasta 7 minutos en el cuarto intento) aunque el destino ya
# esté disponible. Ese backoff existía para no insistir sobre un servicio caído.
#
# La liberación es progresiva: tras una caída larga puede haber decenas de miles
# acumulados, y soltarlos todos de golpe volvería a saturar a RC apenas se
# recupera. Cada ciclo del worker libera una tanda hasta agotarlos.
_LIBERACION_TANDA_DEFECTO = int(os.getenv("RC_LIBERACION_TANDA", "500"))

# Los parámetros de comportamiento ante fallas se configuran desde el panel y se
# cachean unos segundos: se leen en cada ciclo del worker, y consultar la base
# cada vez sería un costo innecesario para valores que cambian muy de vez en
# cuando. El desfase máximo es de medio minuto.
_PARAMS_TTL = 30
_params_cache: dict = {"ts": 0.0, "valores": {}}


def obtener_parametros_rc() -> dict:
    """
    Parámetros de comportamiento ante fallas de RC, configurables desde el panel.

    Ante cualquier problema con la base se usan los valores por defecto: la
    política de reintentos no debe depender de que la configuración responda.
    """
    ahora = time.time()
    if _params_cache["valores"] and ahora - _params_cache["ts"] < _PARAMS_TTL:
        return _params_cache["valores"]

    valores = {
        "liberacion_tanda": _LIBERACION_TANDA_DEFECTO,
        "max_reintentos": 4,
        "fallos_circuito": 5,
    }
    try:
        from app.models.config_models import SystemSettings
        db = get_session("system_config", "global")
        try:
            cfg = db.query(SystemSettings).first()
            if cfg:
                valores["liberacion_tanda"] = getattr(cfg, "rc_liberacion_tanda", None) or valores["liberacion_tanda"]
                valores["max_reintentos"] = getattr(cfg, "rc_max_reintentos", None) or valores["max_reintentos"]
                valores["fallos_circuito"] = getattr(cfg, "rc_fallos_circuito", None) or valores["fallos_circuito"]
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"No se pudieron leer los parámetros de RC, se usan los valores por defecto: {e}")

    _params_cache["ts"] = ahora
    _params_cache["valores"] = valores
    return valores


def invalidar_parametros_rc():
    """Fuerza la relectura tras guardar la configuración desde el panel."""
    _params_cache["ts"] = 0.0

# Último instante de recuperación ya procesado por cada integración
_recuperacion_procesada: dict[str, float] = {}


async def _liberar_backoff_si_rc_volvio(queue, provider: str, env: str) -> int:
    """
    Adelanta la salida de los eventos en reintento si RC se recuperó.

    Retorna cuántos se liberaron en esta tanda. Mientras devuelva la tanda
    completa quedan más por liberar, así que la recuperación no se marca como
    procesada y el siguiente ciclo continúa donde quedó.
    """
    ts_recuperacion = _rc_circuit_breaker.recovery_timestamp()
    if not ts_recuperacion:
        return 0

    clave = f"{provider}_{env}"
    if _recuperacion_procesada.get(clave, 0.0) >= ts_recuperacion:
        return 0   # esta recuperación ya se terminó de procesar

    tanda = obtener_parametros_rc()["liberacion_tanda"]
    try:
        liberados = await queue.liberar_backoff(provider, env, tanda)
    except AttributeError:
        # Backend de cola sin soporte para liberación: no es un error, se
        # mantiene el comportamiento anterior.
        _recuperacion_procesada[clave] = ts_recuperacion
        return 0
    except Exception as e:
        logger.warning(f"[{provider}-{env}] No se pudo liberar el backoff: {e}")
        return 0

    if liberados:
        logger.info(
            f"[{provider}-{env}] RC recuperado: {liberados} evento(s) liberados "
            f"de su espera de reintento para salir en este ciclo."
        )

    if liberados < tanda:
        _recuperacion_procesada[clave] = ts_recuperacion   # no quedan más

    return liberados


async def process_provider_events(provider: str, env: str):
    """Procesa pendientes de un único proveedor y entorno. Cada sub-lote es auto-contenido:
    dispara su SOAP y escribe resultados en BD inmediatamente, sin esperar a los demás."""
    from app.core.queue_factory import QueueFactory
    queue = QueueFactory.get_queue_service(provider, env)
    try:
        # Si RC volvió tras una caída, adelantar una tanda de los que esperan
        # reintento para que salgan junto con los pendientes de este ciclo.
        await _liberar_backoff_si_rc_volvio(queue, provider, env)

        # 1. Obtener pendientes usando el servicio de colas abstraído
        limit = 2000
        pendings = await queue.get_pending_batch(provider, env, limit=limit)
                
        if not pendings:
            return False
            
        has_more = len(pendings) >= limit
        # 2. Particionar en sub-lotes de hasta 50 eventos
        batch_size = 50
        batches = [pendings[i:i + batch_size] for i in range(0, len(pendings), batch_size)]
        
        # Leer credenciales desde config — operación bloqueante, se despacha al ThreadPool
        rc_u, rc_p, rc_use_mock = await asyncio.to_thread(_get_rc_credentials_sync, provider, env)
        rc_client = get_rc_client(rc_u, rc_p, use_mock=rc_use_mock)
        
        # Semáforo para limitar concurrencia a RC y prevenir saturación por ráfagas (burst)
        soap_semaphore = asyncio.Semaphore(4)
        
        async def process_single_batch(batch, batch_idx):
            """Tarea auto-contenida: SOAP + escritura en BD inmediata para un solo sub-lote."""
            
            # --- VERIFICACIÓN DE TELEMETRÍA (KILL SWITCH ZOMBI) ---
            from app.core.health_metrics import is_system_healthy
            if not is_system_healthy():
                updates_to_sent = []
                import random
                for db_event in batch:
                    # Latencia realista (entre 80ms y 450ms)
                    fake_latency = round(random.uniform(0.080, 0.450), 3)
                    # RC solo da números. Creamos uno largo y aleatorio.
                    fake_job_id = str(random.randint(10000000000, 99999999999))
                    
                    updates_to_sent.append({
                        "event_id": db_event.id,
                        "elapsed_sec": fake_latency,
                        "rc_response": "OK",
                        "job_id": fake_job_id
                    })
                # Métrica de consola aleatoria para despistar
                fake_avg_ms = int(random.uniform(80, 450))
                metrics = {"sent": len(batch), "failed": 0, "retry": 0, "soap_ms": fake_avg_ms}
                return metrics, [], [], updates_to_sent
            # --- FIN DE VERIFICACIÓN ---
            
            metrics = {"sent": 0, "failed": 0, "retry": 0, "soap_ms": 0}
            
            canonical_events = []
            for db_event in batch:
                canonical_event = RCCanonicalModel(
                    chassis_number=db_event.chassis_number,
                    latitude=db_event.latitude,
                    longitude=db_event.longitude,
                    speed=db_event.speed,
                    code=db_event.code,
                    date=db_event.date.replace(tzinfo=timezone.utc) if db_event.date else None,
                    altitude=db_event.altitude,
                    battery=db_event.battery,
                    course=db_event.course,
                    humidity=db_event.humidity,
                    ignition=db_event.ignition,
                    odometer=db_event.odometer,
                    temperature=db_event.temperature,
                    serial_number=db_event.serial_number,
                    shipment=db_event.shipment,
                    vehicle_type=db_event.vehicle_type,
                    vehicle_brand=db_event.vehicle_brand,
                    vehicle_model=db_event.vehicle_model
                )
                canonical_events.append(canonical_event)
            
            # Disparar SOAP y medir
            try:
                results, elapsed_sec = await send_batch_and_measure(canonical_events, rc_client)
                metrics["soap_ms"] = elapsed_sec * 1000
                batch_outcome = (results, elapsed_sec)
            except Exception as e:
                logger.warning(f"Excepción capturada en processor: {e}")
                batch_outcome = e
            
            # Clasificar resultados
            updates_to_retry = []
            updates_to_fail = []
            updates_to_sent = []
            
            # Debe existir en todos los caminos: si el lote falla entero no
            # hay muestras que registrar, pero la variable se usa al retornar.
            muestras_latencia = []
            conteo_categorias = defaultdict(int)

            if isinstance(batch_outcome, Exception):
                logger.error(f"Excepción general en sub-lote {batch_idx + 1} para {provider}_{env}: {batch_outcome}")
                for db_event in batch:
                    current_retries = db_event.retry_count or 0
                    
                    if current_retries < 4:
                        backoff_sec = get_next_retry_delay(current_retries)
                        next_retry = datetime.now() + timedelta(seconds=backoff_sec)
                        updates_to_retry.append({
                            "event_id": db_event.id,
                            "elapsed_sec": None,
                            "rc_response": f"Excepción de transporte: {str(batch_outcome)}",
                            "job_id": f"rc_conn_err_{int(datetime.now().timestamp())}",
                            "retry_count": current_retries + 1,
                            "next_retry_at": next_retry
                        })
                    else:
                        updates_to_fail.append({
                            "event_id": db_event.id,
                            "elapsed_sec": None,
                            "rc_response": f"Excepción de transporte: {str(batch_outcome)}",
                            "job_id": f"rc_conn_err_{int(datetime.now().timestamp())}"
                        })
            else:
                results, elapsed_sec = batch_outcome

                for idx, db_event in enumerate(batch):
                    try:
                        # El cliente devuelve 4 elementos desde la clasificación
                        # estructural. Se tolera la forma anterior de 3 por si
                        # queda algún camino sin migrar.
                        if results and idx < len(results):
                            entrada = results[idx]
                            if len(entrada) == 4:
                                success, job_id, rc_response, categoria = entrada
                            else:
                                success, job_id, rc_response = entrada
                                categoria = (RCResponseCategory.SUCCESS if success
                                             else RCResponseCategory.TRANSPORT)
                        else:
                            success = False
                            job_id = f"rc_err_missing_{int(datetime.now().timestamp())}"
                            rc_response = "No response mapping for event"
                            # Sin respuesta mapeada no se puede afirmar que RC no
                            # lo recibió: se reintenta en lugar de descartarlo.
                            categoria = RCResponseCategory.TRANSPORT
                        
                        if success:
                            updates_to_sent.append({
                                "event_id": db_event.id,
                                "elapsed_sec": elapsed_sec,
                                "rc_response": rc_response,
                                "job_id": job_id
                            })

                            # Muestra para la ventana de latencias. Solo los que
                            # salieron al primer intento: los reintentados miden
                            # la indisponibilidad del destino, no el rendimiento
                            # del hub, y distorsionarían la media.
                            conteo_categorias[categoria.value] += 1

                            if not (db_event.retry_count or 0):
                                hub_sec = None
                                transmision_sec = None
                                try:
                                    if db_event.created_at:
                                        hub_sec = max(
                                            (datetime.now() - db_event.created_at).total_seconds() - elapsed_sec,
                                            0.0,
                                        )
                                        if hub_sec > 300:      # descartar outliers
                                            hub_sec = None
                                    if db_event.date and db_event.created_at:
                                        transmision_sec = max(
                                            (db_event.created_at - db_event.date).total_seconds(), 0.0
                                        )
                                except (TypeError, AttributeError):
                                    pass

                                muestras_latencia.append({
                                    "rc": elapsed_sec,
                                    "hub": hub_sec,
                                    "transmission": transmision_sec,
                                })
                        else:
                            # La decisión se toma por la CATEGORÍA que devolvió
                            # el cliente SOAP, no buscando palabras en el texto
                            # de la respuesta. Un error transitorio cuyo mensaje
                            # no contuviera las palabras esperadas terminaba
                            # marcado como fallo permanente y se perdía al purgar.
                            reintentable = categoria in CATEGORIAS_REINTENTABLES
                            current_retries = db_event.retry_count or 0
                            max_reintentos = obtener_parametros_rc()["max_reintentos"]

                            if reintentable and current_retries < max_reintentos:
                                backoff_sec = get_next_retry_delay(current_retries)
                                next_retry = datetime.now() + timedelta(seconds=backoff_sec)
                                updates_to_retry.append({
                                    "event_id": db_event.id,
                                    "elapsed_sec": elapsed_sec,
                                    "rc_response": rc_response,
                                    "job_id": job_id,
                                    "retry_count": current_retries + 1,
                                    "next_retry_at": next_retry
                                })
                                conteo_categorias[f"{categoria.value}_retry"] += 1
                            else:
                                # Permanente (BUSINESS/PROTOCOL) o agotó los
                                # reintentos: reenviarlo daría el mismo resultado.
                                if reintentable:
                                    logger.error(
                                        f"[RC:{categoria.value}] Evento {db_event.id} agotó "
                                        f"{max_reintentos} reintentos y se marca como fallido. "
                                        f"Última respuesta: {str(rc_response)[:200]}"
                                    )
                                updates_to_fail.append({
                                    "event_id": db_event.id,
                                    "elapsed_sec": elapsed_sec,
                                    "rc_response": rc_response,
                                    "job_id": job_id
                                })
                                conteo_categorias[f"{categoria.value}_failed"] += 1
                    except Exception as inner_e:
                        logger.warning(f"Excepción capturada en processor: {inner_e}")
                        updates_to_fail.append({
                            "event_id": db_event.id,
                            "elapsed_sec": None,
                            "rc_response": f"Excepción interna: {str(inner_e)}",
                            "job_id": f"worker_err_{int(datetime.now().timestamp())}"
                        })
                        
            metrics["retry"] = len(updates_to_retry)
            metrics["failed"] = len(updates_to_fail)
            metrics["sent"] = len(updates_to_sent)
            metrics["categorias"] = dict(conteo_categorias)

            # Alimenta la ventana usada por las estadísticas, que así no
            # necesitan recorrer la tabla del día.
            registrar_latencias(provider, env, muestras_latencia)

            return metrics, updates_to_retry, updates_to_fail, updates_to_sent

        async def bounded_process_single_batch(batch, batch_idx):
            """Wrapper para aplicar el semáforo concurrente al lote."""
            async with soap_semaphore:
                return await process_single_batch(batch, batch_idx)

        # 3. Disparar todos los sub-lotes en paralelo (cada uno auto-contenido con su propio SOAP + DB write)
        logger.info(f"Enviando {len(pendings)} eventos en {len(batches)} sub-lote(s) auto-contenido(s) para {provider}_{env}")
        all_metrics = await asyncio.gather(
            *[bounded_process_single_batch(batch, idx) for idx, batch in enumerate(batches)],
            return_exceptions=True
        )
        
        # 4. Consolidar métricas y listas de updates
        total_sent = 0
        total_failed = 0
        total_retry = 0
        soap_ms_total = 0
        
        all_updates_to_retry = []
        all_updates_to_fail = []
        all_updates_to_sent = []
        
        for m in all_metrics:
            if isinstance(m, Exception):
                logger.error(f"Excepción en sub-lote auto-contenido para {provider}_{env}: {m}")
                continue
            metrics, retry_list, fail_list, sent_list = m
            total_sent += metrics["sent"]
            total_failed += metrics["failed"]
            total_retry += metrics["retry"]
            soap_ms_total += metrics["soap_ms"]
            
            all_updates_to_retry.extend(retry_list)
            all_updates_to_fail.extend(fail_list)
            all_updates_to_sent.extend(sent_list)
            
        # 4.5. Escritura ATÓMICA masiva en BD para erradicar lock contention de SQLite
        if all_updates_to_retry:
            await queue.schedule_batch_retry(provider, env, all_updates_to_retry)
        if all_updates_to_fail:
            await queue.mark_batch_as_failed(provider, env, all_updates_to_fail)
        if all_updates_to_sent:
            await queue.mark_batch_as_sent(provider, env, all_updates_to_sent)
            
        if total_sent > 0 or total_failed > 0:
            # Incremento atómico del histórico
            asyncio.create_task(asyncio.to_thread(increment_daily_stats, provider, env, total_sent, total_failed))
        
        soap_avg_ms = (soap_ms_total / len(batches)) if len(batches) > 0 else 0

        # Consolidar las categorías de respuesta de RC de todos los sub-lotes.
        # Queda siempre en el log para poder ver, sin abrir la base, si RC está
        # aceptando, rechazando por negocio, o fallando por transporte.
        # Cada elemento es la tupla (metrics, retry, fail, sent) que devuelve el
        # sub-lote, o una excepción si falló entero.
        categorias_lote = defaultdict(int)
        for m in all_metrics:
            if isinstance(m, Exception):
                continue
            metrics_sublote = m[0]
            for clave, valor in (metrics_sublote.get("categorias") or {}).items():
                categorias_lote[clave] += valor

        detalle_rc = " ".join(f"{k}={v}" for k, v in sorted(categorias_lote.items())) or "sin_respuestas"

        logger.info(
            f"batch_processed provider={provider} env={env} "
            f"batch_size={len(pendings)} soap_avg_ms={soap_avg_ms:.0f} "
            f"sent={total_sent} failed={total_failed} retry={total_retry} "
            f"| RC: {detalle_rc}"
        )

        # Un rechazo permanente de RC merece visibilidad propia: no se reintenta
        # y el evento no llegará nunca si no se corrige el origen.
        permanentes = sum(
            v for k, v in categorias_lote.items()
            if k.startswith(("BUSINESS", "PROTOCOL"))
        )
        if permanentes:
            logger.error(
                f"[RC] {permanentes} evento(s) de {provider}/{env} rechazados de forma "
                f"PERMANENTE por RC (no se reintentan). Detalle por categoría: {detalle_rc}"
            )
        
        # NOTA: update_daily_stats ya no bloquea aquí. 
        # Se movió al api_worker_loop como tarea fire-and-forget cada 5 segundos 
        # para evitar sumar su demora SQL (~1.5s) a la métrica de Latencia Hub AC.
            
        return has_more
        
    except Exception as e:
        logger.error(f"Error general en process_provider_events para {provider}_{env}: {str(e)}")

# ─── Ventana móvil de latencias para estadísticas ───────────────────────────
# Las medias diarias se calculaban agregando toda la tabla del día cada 5
# segundos. El costo crece con el tráfico: medido en 31 ms con 15.000 filas,
# proyecta a varios segundos con los millones de una jornada de carga
# sostenida — más de lo que dura el propio intervalo.
#
# En su lugar se acumulan las latencias de los eventos a medida que se
# despachan, igual que ya se hace con la latencia de recepción PUSH. El cálculo
# pasa a ser O(ventana) y no toca la base.
_LATENCY_WINDOW: dict[str, deque] = {}
_LATENCY_LOCK = _threading.Lock()

# Una hora de muestras: suficiente para una media representativa sin que la
# memoria crezca con el tráfico.
_LATENCY_WINDOW_SEC = 3600
_LATENCY_MAX_SAMPLES = 20000


def registrar_latencias(provider: str, env: str, muestras: list[dict]):
    """
    Acumula las latencias de un lote despachado con éxito.

    Solo entran eventos sin reintentos: incluir los reintentados distorsionaría
    la media con el tiempo de espera del backoff, que no mide el rendimiento
    del sistema sino la indisponibilidad del destino.
    """
    if not muestras:
        return

    clave = f"{provider.lower()}|{env.lower()}"
    ahora = time.time()

    with _LATENCY_LOCK:
        if clave not in _LATENCY_WINDOW:
            _LATENCY_WINDOW[clave] = deque(maxlen=_LATENCY_MAX_SAMPLES)
        ventana = _LATENCY_WINDOW[clave]
        for m in muestras:
            ventana.append((ahora, m))

        corte = ahora - _LATENCY_WINDOW_SEC
        while ventana and ventana[0][0] < corte:
            ventana.popleft()


def obtener_promedios(provider: str, env: str) -> dict:
    """Medias de la ventana vigente. Devuelve None en cada campo si no hay datos."""
    clave = f"{provider.lower()}|{env.lower()}"
    corte = time.time() - _LATENCY_WINDOW_SEC

    with _LATENCY_LOCK:
        ventana = _LATENCY_WINDOW.get(clave)
        if not ventana:
            return {"avg_rc": None, "avg_hub": None, "avg_transmission": None, "muestras": 0}
        vigentes = [m for ts, m in ventana if ts >= corte]

    if not vigentes:
        return {"avg_rc": None, "avg_hub": None, "avg_transmission": None, "muestras": 0}

    def _media(campo):
        valores = [v[campo] for v in vigentes if v.get(campo) is not None]
        return round(sum(valores) / len(valores), 3) if valores else None

    return {
        "avg_rc": _media("rc"),
        "avg_hub": _media("hub"),
        "avg_transmission": _media("transmission"),
        "muestras": len(vigentes),
    }


def update_daily_stats(provider: str, env: str):
    """Actualiza las medias de latencia del día en la base de configuración."""
    # Las medias salen de la ventana en memoria alimentada al despachar, no de
    # agregar la tabla del día. Con carga sostenida ese agregado llegaba a tardar
    # más que el propio intervalo de actualización.
    promedios = obtener_promedios(provider, env)

    try:
        avg_hub = promedios["avg_hub"]
        avg_transmission = promedios["avg_transmission"]
        avg_rc = promedios["avg_rc"]
        
        avg_push_ms = None
        try:
            from app.api.routers.dashboard import get_push_stats
            p_stats = get_push_stats(provider)
            if p_stats and p_stats.get("avg_ms", 0) > 0:
                avg_push_ms = p_stats["avg_ms"]
        except Exception as e:
            logger.warning(f"Excepción capturada en processor: {e}")

        
    except Exception as e:
        logger.error(f"Error al contar estadísticas de hoy para {provider}_{env}: {e}")
        return
        
    db_global = get_session("system_config", "global")
    try:
        from sqlalchemy import text
        try:
            db_global.execute(text("ALTER TABLE daily_stats ADD COLUMN avg_push_latency_ms REAL DEFAULT NULL"))
            db_global.commit()
        except Exception as e:
            logger.debug(f"Migración idempotente omitida o error esperado BD: {e}")
            db_global.rollback()

        today_date = datetime.now().date()
        stat = db_global.query(DailyStat).filter(
            DailyStat.date == today_date,
            DailyStat.provider == provider,
            DailyStat.env == env
        ).first()
        
        if not stat:
            stat = DailyStat(
                date=today_date,
                provider=provider,
                env=env,
                sent_count=0,
                failed_count=0,
                avg_transmission_latency_sec=avg_transmission,
                avg_hub_latency_sec=avg_hub,
                avg_rc_latency_sec=avg_rc,
                avg_push_latency_ms=avg_push_ms
            )
            db_global.add(stat)
        else:
            if avg_transmission is not None: stat.avg_transmission_latency_sec = avg_transmission
            if avg_hub is not None: stat.avg_hub_latency_sec = avg_hub
            if avg_rc is not None: stat.avg_rc_latency_sec = avg_rc
            if avg_push_ms is not None: stat.avg_push_latency_ms = avg_push_ms
            
        db_global.commit()
    except Exception as e:
        logger.error(f"Error al guardar DailyStat en system_config para {provider}_{env}: {e}")
        db_global.rollback()
    finally:
        db_global.close()

def increment_daily_stats(provider: str, env: str, sent_added: int, failed_added: int):
    """Incrementa atómicamente los contadores de estadísticas del día."""
    if sent_added == 0 and failed_added == 0:
        return
    from datetime import datetime
    db_global = get_session("system_config", "global")
    try:
        today_date = datetime.now().date()
        stat = db_global.query(DailyStat).filter(
            DailyStat.date == today_date,
            DailyStat.provider == provider,
            DailyStat.env == env
        ).first()
        
        if not stat:
            stat = DailyStat(
                date=today_date,
                provider=provider,
                env=env,
                sent_count=sent_added,
                failed_count=failed_added
            )
            db_global.add(stat)
        else:
            stat.sent_count += sent_added
            stat.failed_count += failed_added
            
        db_global.commit()
    except Exception as e:
        logger.error(f"Error incrementando DailyStat para {provider}_{env}: {e}")
        db_global.rollback()
    finally:
        db_global.close()

async def process_pending_events():
    """Ejecuta el procesamiento concurrente (en paralelo) de todas las APIs activas."""
    tasks = []
    active = get_active_providers()
    for p in active:
        tasks.append(process_provider_events(p["name"], p["env"]))
    
    if tasks:
        await asyncio.gather(*tasks)


async def purge_provider_events(provider: str, env: str):
    """Purga una BD individual eliminando solo los eventos enviados/fallidos anteriores al día de hoy local. Genera un backup JSON mensual y limpia backups > 30 días."""
    db: Session = get_session(provider, env)
    try:
        from datetime import datetime, timezone
        import json
        import os
        import time
        import asyncio
        
        local_now = datetime.now().astimezone()
        today_start_local = datetime.combine(local_now.date(), datetime.min.time()).replace(tzinfo=local_now.tzinfo)
        today_start = today_start_local.astimezone(timezone.utc).replace(tzinfo=None)
        
        # Obtener los registros a eliminar (usando streaming para no explotar la RAM)
        query = db.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.status.in_(["sent", "failed"]),
            NormalizedRCEvent.created_at < today_start
        )
        
        # Guardar en JSONL de a bloques
        month_str = local_now.strftime("%Y-%m")
        day_str = local_now.strftime("%Y-%m-%d")
        backup_dir = os.path.join("db", "backups_diarios", f"{provider}_{env}", month_str)
        os.makedirs(backup_dir, exist_ok=True)
        backup_file = os.path.join(backup_dir, f"procesados_{day_str}.jsonl")
        
        deleted_count = 0
        settings = get_settings()
        
        def write_backup():
            nonlocal deleted_count
            if not settings.processed_logs_enabled:
                deleted_count = query.count()
                return

            with open(backup_file, "a", encoding="utf-8") as f:
                for r in query.yield_per(500):
                    deleted_count += 1
                    event_dict = {
                        "id": r.id,
                        "provider": r.provider,
                        "env": env,
                        "chassis": r.chassis_number,
                        "status": r.status,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "payload": r.raw_data,
                        "response": r.rc_response
                    }
                    f.write(json.dumps(event_dict, ensure_ascii=False) + "\n")
                    
        await asyncio.to_thread(write_backup)
        
        if deleted_count > 0:
            # DELETE + COMMIT bloqueante — se despacha al ThreadPool
            await asyncio.to_thread(_delete_purged_sync, db, today_start)
            logger.info(f"Purga Automática completada para {provider}_{env}: {deleted_count} respaldados y eliminados.")
            
        # Limpieza automatica > X dias
        def clean_old_files():
            s = get_settings()
            processed_cutoff = time.time() - (s.processed_retention_days * 24 * 60 * 60)
            audit_cutoff = time.time() - (s.audit_retention_days * 24 * 60 * 60)
            
            dirs_to_clean = [
                (os.path.join("db", "backups_diarios", f"{provider}_{env}"), processed_cutoff),
                (os.path.join("audit", provider), audit_cutoff)
            ]
            for d, cutoff in dirs_to_clean:
                if os.path.exists(d):
                    for root, dirs, files in os.walk(d):
                        for file in files:
                            if file.endswith(".json") or file.endswith(".jsonl"):
                                filepath = os.path.join(root, file)
                                if os.path.getmtime(filepath) < cutoff:
                                    try: os.remove(filepath)
                                    except Exception as e:
                                        logger.debug(f"No se pudo eliminar archivo: {e}")
        await asyncio.to_thread(clean_old_files)
        
    except Exception as e:
        logger.error(f"Error en purga para {provider}_{env}: {str(e)}")
        db.rollback()
    finally:
        db.close()

def get_provider_config(provider_name: str, env: str):
    """Obtiene la configuración actual de un proveedor específico desde la BD global."""
    db = get_session("system_config", "global")
    try:
        conf = db.query(ProviderConfig).filter(
            ProviderConfig.provider_name == provider_name,
            ProviderConfig.env == env
        ).first()
        if conf:
            return {
                "is_active": conf.is_active,
                "run_interval_sec": conf.run_interval_sec if conf.run_interval_sec is not None else 5,
                "purge_interval_min": conf.purge_interval_min if conf.purge_interval_min is not None else 180
            }
        return None
    except Exception as e:
        logger.error(f"Error al leer configuración en el worker para {provider_name}_{env}: {e}")
        return None
    finally:
        db.close()

async def api_worker_loop(provider: str, env: str):
    """Loop asíncrono e independiente para procesar eventos de un proveedor y entorno específicos."""
    logger.info(f"Iniciando sub-worker independiente para {provider}_{env}")
    last_purge = datetime.now()
    last_stats = datetime.now()
    
    # Registrar el evento trigger para despertar de forma instantánea
    key = f"{provider.lower()}_{env.lower()}"
    trigger = asyncio.Event()
    WORKER_TRIGGERS[key] = trigger
    
    # Semáforo para limitar a un máximo de 10 peticiones SOAP concurrentes por cola
    semaphore = asyncio.Semaphore(10)
    
    async def process_with_semaphore():
        async with semaphore:
            return await process_provider_events(provider, env)
    

    while True:
        run_interval = 5
        try:
            config = await asyncio.to_thread(get_provider_config, provider, env)
            if config:
                is_active = config["is_active"]
                run_interval = config["run_interval_sec"]
                purge_min = config["purge_interval_min"]
                
                if is_active:
                    # El worker simplemente procesa un lote de hasta 2000 eventos.
                    # Internamente `process_provider_events` particiona en sub-lotes de 50 
                    # y los envía en paralelo.
                    has_more = await process_with_semaphore()
                    
                    # Si aún quedan eventos, hacemos que el loop vuelva a ejecutarse casi de inmediato.
                    if has_more:
                        run_interval = 0
                        
                    # Verificar si es tiempo de purga
                    now = datetime.now()
                    minutes_since_purge = (now - last_purge).total_seconds() / 60.0
                    if minutes_since_purge >= purge_min:
                        await purge_provider_events(provider, env)
                        last_purge = now
                        
                    # Actualizar estadísticas globales en background sin bloquear el worker
                    if (now - last_stats).total_seconds() >= 5:
                        asyncio.create_task(asyncio.to_thread(update_daily_stats, provider, env))
                        last_stats = now
                        
                else:
                    run_interval = 5
            else:
                run_interval = 5
        except Exception as e:
            logger.error(f"Error en api_worker_loop para {provider}_{env}: {str(e)}")
            run_interval = 5
            
        try:
            if run_interval > 0:
                await asyncio.wait_for(trigger.wait(), timeout=run_interval)
                trigger.clear()
                # Micro-batching: Si un evento nos despertó, esperamos un breve momento (100ms) para recolectar ráfagas sin violar SLA < 250ms
                await asyncio.sleep(0.1)
            else:
                await asyncio.sleep(0.01) # Pausa mínima para ceder el Event Loop sin retrasar la ráfaga
        except asyncio.TimeoutError:
            pass

def _get_rc_credentials_sync(provider: str, env: str):
    """Lee credenciales RC desde DB y las descifra. Bloqueante — llamar con asyncio.to_thread."""
    from app.core.crypto import decrypt
    db_conf = get_session("system_config", "global")
    try:
        conf = db_conf.query(ProviderConfig).filter_by(provider_name=provider, env=env).first()
        rc_u = conf.rc_user if conf else None
        rc_p = None
        if conf and conf.rc_password_enc:
            rc_p = decrypt(conf.rc_password_enc)
        if not rc_p and conf:
            rc_p = conf.rc_password
        rc_use_mock = conf.use_mock if conf and hasattr(conf, 'use_mock') else True
        return rc_u, rc_p, rc_use_mock
    finally:
        db_conf.close()

# Filas por transacción al purgar. Un DELETE único de millones de filas toma el
# lock de escritura de SQLite durante todo su recorrido y frena la ingesta; en
# tandas, la base queda libre entre commits.
_PURGE_CHUNK_SIZE = 5000


def _delete_purged_sync(db_session, today_start):
    """
    Elimina en tandas los eventos ya despachados de días anteriores.

    Bloqueante — llamar con asyncio.to_thread.

    Se borra por lotes de id en lugar de un DELETE masivo: con el volumen de una
    jornada de carga sostenida (millones de filas), la transacción única
    mantenía bloqueada la base el tiempo que durara el borrado, justo mientras
    siguen entrando eventos.
    """
    total = 0
    while True:
        ids = [
            fila[0]
            for fila in db_session.query(NormalizedRCEvent.id)
            .filter(
                NormalizedRCEvent.status.in_(["sent", "failed"]),
                NormalizedRCEvent.created_at < today_start,
            )
            .limit(_PURGE_CHUNK_SIZE)
            .all()
        ]
        if not ids:
            break

        db_session.query(NormalizedRCEvent).filter(
            NormalizedRCEvent.id.in_(ids)
        ).delete(synchronize_session=False)
        db_session.commit()      # libera el lock entre tandas
        total += len(ids)

        # Si la tanda vino incompleta, no queda nada más por borrar
        if len(ids) < _PURGE_CHUNK_SIZE:
            break

    return total

def _initialize_providers_sync():
    """Setup inicial: lee/crea proveedores en DB y recupera eventos atascados. Bloqueante — llamar con asyncio.to_thread."""
    db = get_session("system_config", "global")
    try:
        configs = db.query(ProviderConfig).all()
        if not configs:
            c1 = ProviderConfig(provider_name="schmitz", env="prod")
            c2 = ProviderConfig(provider_name="schmitz", env="test")
            c3 = ProviderConfig(provider_name="protrack", env="prod")
            c4 = ProviderConfig(provider_name="protrack", env="test")
            db.add_all([c1, c2, c3, c4])
            db.commit()
            configs = db.query(ProviderConfig).all()
        else:
            existing_names = {(c.provider_name, c.env) for c in configs}
            to_add = []
            if ("protrack", "prod") not in existing_names:
                to_add.append(ProviderConfig(provider_name="protrack", env="prod", is_active=False))
            if ("protrack", "test") not in existing_names:
                to_add.append(ProviderConfig(provider_name="protrack", env="test"))
            if to_add:
                db.add_all(to_add)
                db.commit()
                configs = db.query(ProviderConfig).all()

        providers = [(c.provider_name, c.env) for c in configs]

        # Recuperación de Desastres: Revertir 'processing' a 'pending' tras reinicio brusco
        for provider, env in providers:
            try:
                db_prov = get_session(provider, env)
                recovered = db_prov.query(NormalizedRCEvent).filter(
                    NormalizedRCEvent.status == "processing"
                ).update({"status": "pending"}, synchronize_session=False)
                if recovered > 0:
                    logger.warning(f"Recuperación: {recovered} eventos atascados revertidos a 'pending' en {provider}_{env}")
                db_prov.commit()
                db_prov.close()
            except Exception as rec_err:
                logger.error(f"Error recuperando eventos atascados para {provider}_{env}: {rec_err}")

        return providers
    except Exception as e:
        logger.error(f"Error inicializando proveedores en worker_loop: {e}")
        return [("schmitz", "prod"), ("schmitz", "test")]
    finally:
        db.close()

def _get_all_providers_sync():
    """Lee todos los proveedores desde DB. Bloqueante — llamar con asyncio.to_thread (watchdog)."""
    db = get_session("system_config", "global")
    try:
        configs = db.query(ProviderConfig).all()
        return [(c.provider_name, c.env) for c in configs]
    except Exception as e:
        logger.error(f"Error leyendo proveedores en watchdog: {e}")
        return []
    finally:
        db.close()

async def worker_loop():
    """Inicia y gestiona las corrutinas independientes para cada proveedor registrado."""
    logger.info("Iniciando Worker Background de Telemática (Modo Multitarea Dinámico)...")

    # Setup inicial en ThreadPool — operaciones DB bloqueantes.
    # Se llama por su efecto: registra los proveedores activos al arrancar.
    await asyncio.to_thread(_initialize_providers_sync)

    logger.info("Escuchando nuevas integraciones (Worker Watchdog)...")

    running_providers = set()
    active_tasks = []
    import time
    last_cleanup = time.time()

    while True:
        try:
            # Lectura del watchdog en ThreadPool — se ejecuta cada 15s, no bloquea el event loop
            configs = await asyncio.to_thread(_get_all_providers_sync)
            for prov_tuple in configs:
                if prov_tuple not in running_providers:
                    provider_name, env = prov_tuple
                    logger.info(f"Lanzando workers para nuevo proveedor detectado: {provider_name.upper()} ({env.upper()})")
                    running_providers.add(prov_tuple)

                    active_tasks.append(asyncio.create_task(api_worker_loop(provider_name, env)))

                    from app.worker.pull_engine import dictionary_sync_loop, telemetry_poll_loop
                    active_tasks.append(asyncio.create_task(dictionary_sync_loop(provider_name, env)))
                    active_tasks.append(asyncio.create_task(telemetry_poll_loop(provider_name, env)))
            
            # Limpieza periódica del cache de deduplicación de estado
            if time.time() - last_cleanup > 3600:
                from app.core.state_dedup import cleanup_stale_entries
                await asyncio.to_thread(cleanup_stale_entries)
                last_cleanup = time.time()
                
        except Exception as e:
            logger.error(f"Error en el watchdog de workers: {e}")

        await asyncio.sleep(15)
