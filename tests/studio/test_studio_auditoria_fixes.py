# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_auditoria_fixes.py (reorganización por tema: studio).
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


def _db_sin_traduccion():
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    return db


SCHEMA_SIN_DEFAULT = {
    # Con fecha: desde la v1.9.2 un evento sin fecha no entra (contrato.py).
    "base_mapping": {"chassis_number": "imei", "latitude": "lat", "longitude": "lng",
                     "date": "gpstime"},
    "trigger_rules": [
        {"enabled": True, "field": "acc", "operator": "eq", "value": "99", "rc_code": "11"}
    ],
    "default_rule": {"enabled": False, "rc_code": "1"},
}
PAYLOAD = {"imei": "868307060968914", "lat": 9.98, "lng": -84.73, "acc": "0",
           "gpstime": 1786102275}


def test_el_fallback_conserva_los_datos_del_payload():
    """
    REGRESIÓN: se pasaba el schema como payload, así que no se encontraba
    ningún campo y el evento salía con chassis_number="UNKNOWN" y coordenadas
    nulas — un activo no identificable llegando a RC.
    """
    with patch("app.database.get_session", return_value=_db_con_traduccion()):
        r = DynamicMapper.map_payload_multi(PAYLOAD, SCHEMA_SIN_DEFAULT, "protrack", "prod", True)

    assert len(r) == 1
    assert r[0].chassis_number == "C180673"      # traducido, no UNKNOWN
    assert r[0].latitude == 9.98                  # el payload real, no nulo
    assert r[0].longitude == -84.73


def test_el_fallback_respeta_el_control_de_traduccion():
    """
    Con chassis_number="UNKNOWN" el guard del diccionario se salteaba y el
    evento se enviaba igual, violando "sin traducción no se envía".
    """
    with patch("app.database.get_session", return_value=_db_sin_traduccion()):
        r = DynamicMapper.map_payload_multi(PAYLOAD, SCHEMA_SIN_DEFAULT, "protrack", "prod", True)

    assert r == []


def test_el_camino_normal_no_se_altera():
    """El fallback solo actúa cuando nada más generó eventos."""
    schema = dict(SCHEMA_SIN_DEFAULT)
    schema["default_rule"] = {"enabled": True, "rc_code": "1"}

    with patch("app.database.get_session", return_value=_db_con_traduccion()):
        r = DynamicMapper.map_payload_multi(PAYLOAD, schema, "protrack", "prod", True)

    assert len(r) >= 1
    assert r[0].chassis_number == "C180673"
