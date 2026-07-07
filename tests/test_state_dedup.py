import time
import pytest
from app.core.state_dedup import should_emit_event, get_base_code, cleanup_stale_entries, _STATE_CACHE, _TTL_SECONDS

def test_get_base_code_empty():
    assert get_base_code({}) == "1"
    assert get_base_code(None) == "1"

def test_get_base_code_from_schema():
    schema = {"base_mapping": {}, "default_rule": {"rc_code": "5"}}
    assert get_base_code(schema) == "5"

def test_base_code_always_emits():
    # Posición base siempre pasa, sin importar cache
    should_emit_event("p", "e", "chassis1", "1", "1", enabled=True)  # primera vez
    assert should_emit_event("p", "e", "chassis1", "1", "1", enabled=True) is True

def test_sensor_emits_on_first_occurrence():
    _STATE_CACHE.clear()
    assert should_emit_event("p", "e", "chassis1", "12", "1", enabled=True) is True

def test_sensor_suppressed_on_second_occurrence():
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)  # primera
    assert should_emit_event("p", "e", "chassis1", "12", "1", enabled=True) is False

def test_sensor_transition_emits():
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)  # motor apagado
    assert should_emit_event("p", "e", "chassis1", "11", "1", enabled=True) is True  # motor encendido = transición

def test_toggle_disabled_emits_all():
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)
    assert should_emit_event("p", "e", "chassis1", "12", "1", enabled=False) is True

def test_different_chassis_independent():
    _STATE_CACHE.clear()
    should_emit_event("p", "e", "chassis1", "12", "1", enabled=True)
    assert should_emit_event("p", "e", "chassis2", "12", "1", enabled=True) is True

def test_cleanup_removes_stale():
    _STATE_CACHE.clear()
    _STATE_CACHE["old_key"] = time.time() - (13 * 60 * 60)  # 13h ago
    cleanup_stale_entries()
    assert "old_key" not in _STATE_CACHE
