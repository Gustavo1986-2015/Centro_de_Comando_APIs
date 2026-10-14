# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_auditoria_fixes.py (reorganización por tema: rc).
"""
Tests de los hallazgos M2, M3 y M5 de la auditoría.

Los tres afectan la calidad de lo que llega a Recurso Confiable o la protección
del panel, y ninguno estaba cubierto.
"""
import os
import pytest
from unittest.mock import MagicMock, patch

from app.core.dynamic_mapper import DynamicMapper, _a_booleano
from app.core.auth import (
    get_dashboard_password,
    verificar_credenciales_al_arrancar,
)


# ═══════════════════════════════════════════════════════════════════
# M2 — El fallback pasaba el schema en lugar del payload
# ═══════════════════════════════════════════════════════════════════

def _db_con_traduccion(valor="C180673"):
    entrada = MagicMock()
    entrada.dict_value = valor
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = entrada
    return db


# ═══════════════════════════════════════════════════════════════════
# M3 — bool() invertía los estados reportados como texto
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("valor", ["false", "False", "FALSE", "0", "off", "no", ""])
def test_los_falsos_en_texto_se_interpretan_como_falsos(valor):
    """
    REGRESIÓN: bool("false") y bool("0") son True. Un proveedor que reporte la
    ignición como cadena mandaba a RC el estado invertido: dato incorrecto, que
    es peor que dato ausente porque el operador lo lee como real.
    """
    assert _a_booleano(valor) is False


@pytest.mark.parametrize("valor", ["true", "True", "1", "on", "yes", "si"])
def test_los_verdaderos_en_texto_se_interpretan_como_verdaderos(valor):
    assert _a_booleano(valor) is True


@pytest.mark.parametrize("valor,esperado", [
    (True, True), (False, False), (1, True), (0, False), (1.0, True), (0.0, False),
])
def test_booleanos_y_numeros_nativos(valor, esperado):
    assert _a_booleano(valor) is esperado


def test_ausencia_de_dato_se_mantiene_como_nulo():
    assert _a_booleano(None) is None


def test_valor_no_reconocido_se_reporta_como_nulo():
    """Ante un valor inesperado es preferible no enviar dato a inventar uno."""
    assert _a_booleano("estado_raro") is None


def test_ignition_llega_correcta_al_modelo_canonico():
    """Verificación de punta a punta sobre el mapeo real."""
    schema = {"chassis_number": "imei", "latitude": "lat", "longitude": "lng", "ignition": "acc"}

    with patch("app.database.get_session", return_value=_db_con_traduccion()):
        apagado = DynamicMapper.map_payload(
            {"imei": "8683", "lat": 1.0, "lng": 2.0, "acc": "false"}, schema, "protrack", "prod"
        )
        encendido = DynamicMapper.map_payload(
            {"imei": "8683", "lat": 1.0, "lng": 2.0, "acc": "true"}, schema, "protrack", "prod"
        )

    assert apagado.ignition is False
    assert encendido.ignition is True
