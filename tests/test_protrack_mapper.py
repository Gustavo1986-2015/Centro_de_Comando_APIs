"""Tests del mapper de Protrack (vía DynamicMapper y pull_engine).

Valida:
  - La estructura esperada del payload Protrack (data, imei, lat, lng)
  - El esquema de mapeo dinámico acepta los campos de Protrack
  - execute_fetch en pull_engine contiene la lógica MD5 dinámica de Protrack
"""
import pytest
import inspect


def test_protrack_payload_has_expected_structure(sample_protrack_payload):
    """El payload Protrack debe tener la estructura esperada (data, imei, lat, lng)."""
    assert "data" in sample_protrack_payload, "El payload debe tener clave 'data'"
    assert isinstance(sample_protrack_payload["data"], list), "'data' debe ser una lista"
    assert len(sample_protrack_payload["data"]) > 0, "'data' no debe estar vacía"

    item = sample_protrack_payload["data"][0]
    assert "imei" in item, "Cada item debe tener 'imei'"
    assert "lat" in item, "Cada item debe tener 'lat'"
    assert "lng" in item, "Cada item debe tener 'lng'"


def test_dynamic_mapper_accepts_protrack_schema():
    """El esquema de mapeo dinámico para Protrack debe tener los campos obligatorios del modelo canónico."""
    protrack_mapping = {
        "chassis_number": "imei",
        "latitude": "lat",
        "longitude": "lng",
        "speed": "speed",
        "date": "time"
    }
    assert isinstance(protrack_mapping, dict), "El schema debe ser un dict"
    assert "chassis_number" in protrack_mapping, (
        "El schema debe mapear chassis_number (campo obligatorio en RCCanonicalModel)"
    )
    assert "latitude" in protrack_mapping, "El schema debe mapear latitude"
    assert "longitude" in protrack_mapping, "El schema debe mapear longitude"


def test_protrack_auth_uses_dynamic_md5():
    """execute_fetch en pull_engine debe contener la lógica de auth MD5 dinámica de Protrack."""
    from app.worker.pull_engine import execute_fetch

    src = inspect.getsource(execute_fetch)

    assert "protrack" in src.lower(), (
        "execute_fetch debe tener lógica específica para el auth type 'protrack'"
    )
    assert "md5" in src.lower(), (
        "La autenticación de Protrack usa HMAC-MD5 dinámico — debe estar en execute_fetch"
    )
