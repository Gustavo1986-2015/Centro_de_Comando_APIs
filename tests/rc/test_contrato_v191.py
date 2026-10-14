# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v191.py (reorganización por tema: rc).
"""
v1.9.1 — Reglas anidadas, filtro de admisión e integridad del payload a RC.

Todo sale de la prueba real de Tive del 01/10/2026:

  · El webhook trajo 171 eventos y solo 6 tenían envío: el filtro de admisión
    deja entrar solo lo que la integración necesita.
  · Dos eventos eran los ejemplos del botón de prueba de Tive (cuenta -1,
    equipo SAMPLEDEVICEID): sin filtro, llegarían a RC como un camión real.
  · El envío de las alertas viene anidado en Alert.ShipmentId, y las reglas
    solo leían el primer nivel (deuda B1).
  · El hub mandaba a RC "ignition: false" para trackers que no miden
    ignición. El contrato D-TI-15 v14 la define OPCIONAL y pide "en blanco o
    0" cuando no hay dato: "false" afirma algo que nadie midió.
  · El botón "JSON RC" del panel no mostraba lo que se envía de verdad.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import admision
from app.core.dynamic_mapper import DynamicMapper
from app.services.rc_soap import construir_evento_rc


@pytest.fixture(autouse=True)
def contadores_limpios():
    admision.reset()
    yield
    admision.reset()


# ═══════════════════════════════════════════════════════════════════════════
# Integridad del payload a RC — contrato D-TI-15 v14
# ═══════════════════════════════════════════════════════════════════════════

def _evento(**campos):
    base = dict(chassis_number="Q92951", code="1", date=datetime(2026, 10, 1, 15, 29, 39),
                course=None, ignition=None, latitude=20.6, longitude=-100.1, speed=None,
                altitude=None, battery=None, humidity=None, odometer=None,
                temperature=None, serial_number=None, shipment=None)
    base.update(campos)
    return SimpleNamespace(**base)


def test_la_ignicion_desconocida_no_se_envia():
    """
    El caso de Tive: no mide ignición. Antes salía "false", que afirma que el
    vehículo está apagado.
    """
    assert "ignition" not in construir_evento_rc(_evento(ignition=None))


@pytest.mark.parametrize("valor, esperado", [(True, "true"), (False, "false")])
def test_la_ignicion_conocida_se_envia_igual_que_siempre(valor, esperado):
    """Producción: Schmitz y Protrack reportan ignición y no cambia nada."""
    assert construir_evento_rc(_evento(ignition=valor))["ignition"] == esperado


def test_los_obligatorios_siguen_yendo_con_cero_si_faltan():
    """
    El contrato marca obligatorios speed, latitude y longitude, y pide "en
    blanco o 0" cuando no hay dato. Omitirlos haría que RC rechace el evento.
    """
    ev = construir_evento_rc(_evento(speed=None, latitude=None, longitude=None))
    assert ev["speed"] == "0"
    assert ev["latitude"] == "0"
    assert ev["longitude"] == "0"


def test_los_obligatorios_estan_siempre():
    ev = construir_evento_rc(_evento())
    for campo in ("asset", "code", "date", "latitude", "longitude", "speed"):
        assert campo in ev, f"Falta el obligatorio {campo}"


def test_la_fecha_va_sin_z():
    """El contrato pide YYYY-MM-DDTHH:MM:SS; RC rechaza el lote si no la lee."""
    assert construir_evento_rc(_evento())["date"] == "2026-10-01T15:29:39"
