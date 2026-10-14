import time
import pytest
from app.core.state_dedup import (
    should_emit_event, get_base_code, cleanup_stale_entries,
    _STATE_CACHE, _TTL_SECONDS
)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _state_schema(*codes, dedup_key=None):
    """Genera un mapping_schema con reglas de tipo 'state' para los codes dados."""
    rules = []
    for c in codes:
        rule = {"rc_code": str(c), "event_type": "state"}
        if dedup_key:
            rule["dedup_key"] = dedup_key
        rules.append(rule)
    return {"trigger_rules": rules, "base_mapping": {}}

def _momentary_schema(*codes):
    """Genera un mapping_schema con reglas de tipo 'momentary' para los codes dados."""
    return {
        "trigger_rules": [{"rc_code": str(c), "event_type": "momentary"} for c in codes],
        "base_mapping": {}
    }


# ─── get_base_code ────────────────────────────────────────────────────────────

def test_get_base_code_empty():
    assert get_base_code({}) == "1"
    assert get_base_code(None) == "1"

def test_get_base_code_from_schema():
    schema = {"base_mapping": {}, "default_rule": {"rc_code": "5"}}
    assert get_base_code(schema) == "5"


# ─── Base code (GPS) siempre emite ────────────────────────────────────────────

def test_base_code_always_emits():
    # Posición base siempre pasa, sin importar cache
    should_emit_event("p", "e", "chassis1", "1", "1", enabled=True)
    assert should_emit_event("p", "e", "chassis1", "1", "1", enabled=True) is True


# ─── Toggle desactivado ────────────────────────────────────────────────────────

def test_toggle_disabled_emits_all():
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)
    assert should_emit_event("p", "e", "chassis1", "12", "1", enabled=False) is True


# ─── Backward compatibility (sin mapping_schema) ─────────────────────────────

def test_sensor_emits_on_first_occurrence():
    _STATE_CACHE.clear()
    assert should_emit_event("p", "e", "chassis1", "12", "1", enabled=True) is True

def test_sensor_suppressed_on_second_occurrence():
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)
    assert should_emit_event("p", "e", "chassis1", "12", "1", enabled=True) is False

def test_sensor_transition_emits_no_schema():
    """Sin schema: motor apagado -> motor encendido = transición (dedup por rc_code)."""
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)  # motor apagado
    assert should_emit_event("p", "e", "chassis1", "11", "1", enabled=True) is True

def test_different_chassis_independent():
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)
    assert should_emit_event("p", "e", "chassis2", "12", "1", enabled=True) is True


# ─── Nuevos tests: momentary (SOS, crash, geofence) ─────────────────────────

def test_momentary_event_always_emits():
    """SOS, crash — siempre emiten sin importar cache."""
    _STATE_CACHE.clear()
    schema = _momentary_schema("99")
    assert should_emit_event("p", "e", "c1", "99", "1", schema, True) is True
    assert should_emit_event("p", "e", "c1", "99", "1", schema, True) is True  # 2da vez también


# ─── Nuevos tests: transiciones con dedup_key compartido ─────────────────────

def test_state_transition_emits():
    """Puerta 1→0→1: la 2da apertura debe emitir (transición)."""
    _STATE_CACHE.clear()
    schema = _state_schema("10", "34", dedup_key="door")
    assert should_emit_event("p", "e", "c1", "10", "1", schema, True) is True   # abrir
    assert should_emit_event("p", "e", "c1", "10", "1", schema, True) is False  # sigue abierto
    assert should_emit_event("p", "e", "c1", "34", "1", schema, True) is True   # cerrar (transición)
    assert should_emit_event("p", "e", "c1", "10", "1", schema, True) is True   # abrir de nuevo (transición)

def test_state_continuous_suppressed():
    """Motor apagado 5 ciclos: solo emite el primero."""
    _STATE_CACHE.clear()
    schema = _state_schema("12", dedup_key="acc")
    assert should_emit_event("p", "e", "c1", "12", "1", schema, True) is True
    assert should_emit_event("p", "e", "c1", "12", "1", schema, True) is False
    assert should_emit_event("p", "e", "c1", "12", "1", schema, True) is False


# ─── Nuevos tests: fail-open y backward compat ───────────────────────────────

def test_unknown_rule_emits_fail_open():
    """Si el code no está en mapping, emitir (fail-open)."""
    _STATE_CACHE.clear()
    schema = {"trigger_rules": [{"rc_code": "11"}], "base_mapping": {}}
    # code 99 no está en las reglas → fail-open
    assert should_emit_event("p", "e", "c1", "99", "1", schema, True) is True

def test_backward_compat_no_event_type_defaults_state():
    """Regla sin event_type → default state (backward compat)."""
    _STATE_CACHE.clear()
    schema = {"trigger_rules": [{"rc_code": "12"}], "base_mapping": {}}  # sin event_type
    assert should_emit_event("p", "e", "c1", "12", "1", schema, True) is True
    assert should_emit_event("p", "e", "c1", "12", "1", schema, True) is False


# ─── Cleanup ─────────────────────────────────────────────────────────────────

def test_cleanup_removes_stale():
    _STATE_CACHE.clear()
    # Simular entrada vieja con estructura (code, timestamp)
    _STATE_CACHE["old_key"] = ("12", time.time() - (13 * 60 * 60))  # 13h ago
    cleanup_stale_entries()
    assert "old_key" not in _STATE_CACHE

def test_cleanup_keeps_fresh():
    _STATE_CACHE.clear()
    _STATE_CACHE["fresh_key"] = ("12", time.time())  # ahora
    cleanup_stale_entries()
    assert "fresh_key" in _STATE_CACHE
