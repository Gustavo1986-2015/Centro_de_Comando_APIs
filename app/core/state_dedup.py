import time
import logging
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_CACHE = {}
_LOCK = Lock()
_TTL_SECONDS = 12 * 3600  # 12 hours

def _make_key(provider: str, env: str, chassis: str, code: str) -> str:
    return f"{provider}:{env}:{chassis}:{code}"

def get_base_code(mapping_schema: dict) -> str:
    if not mapping_schema:
        return "1"
    if "base_mapping" not in mapping_schema:
        return "1"
    return str(mapping_schema.get("default_rule", {}).get("rc_code", "1"))

def should_emit_event(
    provider: str,
    env: str,
    chassis: str,
    code: Optional[str],
    base_code: str,
    enabled: bool = True
) -> bool:
    """
    Decide si un evento debe emitirse o descartarse como duplicado.
    
    Reglas:
    - Si enabled=False -> siempre emitir (toggle apagado)
    - Si code == base_code -> siempre emitir (es posición GPS)
    - Si code != base_code -> emitir solo si NO estaba activo en el ciclo anterior
    """
    if not enabled:
        return True
    
    # Posición base siempre pasa
    if not code or str(code) == str(base_code):
        return True
    
    # Evento de sensor: aplicar dedup
    key = _make_key(provider, env, chassis, str(code))
    now = time.time()
    
    with _LOCK:
        if key in _STATE_CACHE:
            # Ya estaba activo -> es estado continuo, descartar
            _STATE_CACHE[key] = now  # actualizar timestamp
            return False
        # No estaba -> es transición, emitir y marcar activo
        _STATE_CACHE[key] = now
        return True

def cleanup_stale_entries():
    """Elimina entradas del cache que no se actualizaron en TTL_SECONDS.
    Llamar periódicamente para evitar memory leak."""
    now = time.time()
    with _LOCK:
        stale = [k for k, ts in _STATE_CACHE.items() if (now - ts) > _TTL_SECONDS]
        for k in stale:
            del _STATE_CACHE[k]
    if stale:
        logger.info(f"Dedup cache cleanup: {len(stale)} entradas expiradas eliminadas.")

def get_cache_stats() -> dict:
    """Retorna estadísticas del cache (para dashboard/monitoring)."""
    with _LOCK:
        return {
            "total_entries": len(_STATE_CACHE),
            "ttl_seconds": _TTL_SECONDS
        }
