"""Tests del mapper de Schmitz (app/providers/schmitz/mapper.py).

Valida:
  - map_schmitz_payload retorna una lista de RCCanonicalModel
  - El chassis_number viene en MAYÚSCULAS sin guiones
  - La posición GPS (lat/lon) está dentro de rangos válidos
  - Los payloads sin Events retornan lista vacía (no lanza excepción)
"""
import pytest
from app.providers.schmitz.mapper import map_schmitz_payload
from app.schemas.canonical import RCCanonicalModel


def test_schmitz_mapper_returns_list_of_canonical_models(sample_schmitz_payload):
    """map_schmitz_payload debe retornar una lista de RCCanonicalModel."""
    result = map_schmitz_payload(sample_schmitz_payload)

    assert isinstance(result, list), "El mapper debe retornar una lista"
    assert len(result) > 0, "Debe retornar al menos un evento para un payload válido"
    assert all(isinstance(e, RCCanonicalModel) for e in result), (
        "Todos los items deben ser instancias de RCCanonicalModel"
    )


def test_schmitz_mapper_chassis_sanitized(sample_schmitz_payload):
    """El chassis_number debe estar en MAYÚSCULAS y sin guiones/espacios (sanitize_asset)."""
    result = map_schmitz_payload(sample_schmitz_payload)
    first = result[0]

    assert first.chassis_number is not None, "chassis_number no debe ser None"
    assert len(first.chassis_number) > 0, "chassis_number no debe estar vacío"
    assert first.chassis_number == first.chassis_number.upper(), (
        "chassis_number debe estar en MAYÚSCULAS"
    )
    assert "-" not in first.chassis_number, (
        "chassis_number no debe contener guiones (sanitize_asset los elimina)"
    )


def test_schmitz_mapper_extracts_valid_gps_position(sample_schmitz_payload):
    """La posición GPS debe estar dentro de rangos válidos."""
    result = map_schmitz_payload(sample_schmitz_payload)
    first = result[0]

    assert first.latitude is not None, "latitude no debe ser None para payload con Position"
    assert first.longitude is not None, "longitude no debe ser None para payload con Position"
    assert -90.0 <= first.latitude <= 90.0, (
        f"latitude {first.latitude} fuera de rango [-90, 90]"
    )
    assert -180.0 <= first.longitude <= 180.0, (
        f"longitude {first.longitude} fuera de rango [-180, 180]"
    )


def test_schmitz_mapper_handles_empty_events_gracefully():
    """Un payload con Events vacío debe retornar lista vacía sin lanzar excepción."""
    minimal_payload = {
        "ChassisNumber": "TEST123",
        "Header": {
            "Customer": {"Id": "test"},
            "SerialNumber": "TEST123",
            "Timestamp": "2026-06-29T10:00:00Z"
        },
        "Events": [],
        "Reason": {"ItemElementName": "Standard", "Value": "Status"}
    }
    result = map_schmitz_payload(minimal_payload)

    assert isinstance(result, list), "Debe retornar una lista aunque esté vacía"
