import time
import logging
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

# Cache: key -> (code: str, timestamp: float)
_STATE_CACHE: dict[str, tuple[str, float]] = {}
_LOCK = Lock()
_TTL_SECONDS = 12 * 3600  # 12 hours


def _make_cache_key(provider: str, env: str, chassis: str, dedup_key: str) -> str:
    return f"{provider}:{env}:{chassis}:{dedup_key}"


def _find_rule_by_code(mapping_schema: dict, code: str) -> Optional[dict]:
    """Busca en trigger_rules la regla cuyo rc_code coincide con el code dado."""
    if not mapping_schema or not isinstance(mapping_schema, dict):
        return None
    rules = mapping_schema.get("trigger_rules", [])
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if isinstance(rule, dict) and str(rule.get("rc_code", "")) == str(code):
            return rule
    return None


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
    mapping_schema: Optional[dict] = None,
    enabled: bool = True
) -> bool:
    """
    Decide si un evento debe emitirse o descartarse como duplicado.

    Reglas (en orden de prioridad):
    1. Si enabled=False -> siempre emitir (toggle apagado).
    2. Si code == base_code -> siempre emitir (posición GPS).
    3. Si event_type == "momentary" -> siempre emitir (SOS, crash, geofence).
    4. Si event_type == "state" (default):
       - Cache key = (provider, env, chassis, dedup_key)
       - Si cache tiene el MISMO code -> descartar (estado continuo).
       - Si cache tiene OTRO code o está vacío -> emitir + actualizar cache (transición/primera vez).
    5. Code desconocido (no en mapping) -> emitir (fail-open).

    Backward compat:
    - Reglas sin event_type -> default "state".
    - Reglas sin dedup_key -> default = rc_code.
    - mapping_schema=None -> comportamiento original (dedup simple por code).
    """
    if not enabled:
        return True

    # Posición base siempre pasa
    if not code or str(code) == str(base_code):
        return True

    # Buscar regla para este code en el mapping_schema
    rule = _find_rule_by_code(mapping_schema, code) if mapping_schema else None

    # Eventos momentáneos SIEMPRE emiten (SOS, crash, geofence, etc.)
    if rule and rule.get("event_type") == "momentary":
        return True

    # Code desconocido (no registrado en ninguna regla) -> fail-open
    if mapping_schema and rule is None:
        trigger_rules = mapping_schema.get("trigger_rules", [])
        if isinstance(trigger_rules, list) and len(trigger_rules) > 0:
            # Hay reglas definidas pero este code no está en ninguna -> fail-open
            return True

    # State rule (default): dedup por dedup_key
    dedup_key = str(rule.get("dedup_key", code)) if rule else str(code)
    if not dedup_key:
        dedup_key = str(code)

    cache_key = _make_cache_key(provider, env, chassis, dedup_key)
    now = time.time()

    with _LOCK:
        cached = _STATE_CACHE.get(cache_key)
        if cached is not None:
            cached_code, _ = cached
            if cached_code == str(code):
                # Mismo estado continuo -> descartar, actualizar timestamp
                _STATE_CACHE[cache_key] = (str(code), now)
                return False
        # Transición o primera vez -> emitir y guardar
        _STATE_CACHE[cache_key] = (str(code), now)
        return True


def cleanup_stale_entries():
    """Elimina entradas del cache que no se actualizaron en TTL_SECONDS.
    Llamar periódicamente para evitar memory leak."""
    now = time.time()
    with _LOCK:
        stale = [k for k, (code, ts) in list(_STATE_CACHE.items()) if (now - ts) > _TTL_SECONDS]
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
