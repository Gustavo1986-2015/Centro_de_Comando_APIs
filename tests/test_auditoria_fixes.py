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
    "base_mapping": {"chassis_number": "imei", "latitude": "lat", "longitude": "lng"},
    "trigger_rules": [
        {"enabled": True, "field": "acc", "operator": "eq", "value": "99", "rc_code": "11"}
    ],
    "default_rule": {"enabled": False, "rc_code": "1"},
}
PAYLOAD = {"imei": "868307060968914", "lat": 9.98, "lng": -84.73, "acc": "0"}


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


# ═══════════════════════════════════════════════════════════════════
# M5 — Contraseña del panel: fuente única y arranque protegido
# ═══════════════════════════════════════════════════════════════════

def test_sin_variable_no_hay_contrasena_adivinable(monkeypatch):
    """
    REGRESIÓN: había tres defaults distintos ("changeme", "admin", "") según el
    archivo, así que la protección dependía de por dónde entrara la petición.
    """
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert get_dashboard_password() == ""


def test_la_contrasena_configurada_se_respeta(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "clave_real_2026")
    assert get_dashboard_password() == "clave_real_2026"


def test_produccion_sin_contrasena_no_arranca(monkeypatch):
    """
    Un panel abierto puede pasar semanas inadvertido; un fallo de arranque se
    ve de inmediato. Expone configuración de proveedores y edición de base.
    """
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        verificar_credenciales_al_arrancar()


def test_desarrollo_sin_contrasena_arranca_con_aviso(monkeypatch):
    """En desarrollo no debe bloquear el trabajo, pero sí advertir."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    verificar_credenciales_al_arrancar()   # no debe lanzar


def test_produccion_con_contrasena_arranca(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "una_clave_larga_y_segura")

    verificar_credenciales_al_arrancar()   # no debe lanzar


def test_el_acceso_queda_bloqueado_sin_contrasena_configurada(monkeypatch):
    """
    Sin contraseña no debe haber ninguna combinación que abra el panel,
    tampoco enviando una contraseña vacía.
    """
    from fastapi import HTTPException
    from fastapi.security import HTTPBasicCredentials
    from app.core.auth import verify_dashboard_auth

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DASHBOARD_USER", "admin")

    for intento in ("", "changeme", "admin", "cualquiera"):
        with pytest.raises(HTTPException) as exc:
            verify_dashboard_auth(HTTPBasicCredentials(username="admin", password=intento))
        assert exc.value.status_code == 401
