"""
Tests de las señales de resincronización manual (app/core/resync.py) y del
descarte de eventos sin traducción en el diccionario.

Ambos comportamientos son transversales: aplican a cualquier proveedor con
diccionario habilitado, sin lógica específica por AVL.
"""
import asyncio
import pytest

from app.core import resync


@pytest.fixture(autouse=True)
def _reset():
    resync.reset()
    yield
    resync.reset()


# ── Señales de resync ────────────────────────────────────────────────────────

def test_get_signal_crea_y_reutiliza():
    s1 = resync.get_signal("protrack", "prod")
    s2 = resync.get_signal("protrack", "prod")
    assert s1 is s2


def test_signal_normaliza_mayusculas():
    s1 = resync.get_signal("PROTRACK", "PROD")
    s2 = resync.get_signal("protrack", "prod")
    assert s1 is s2


def test_signals_separadas_por_entorno():
    """prod y test son integraciones distintas: no deben compartir señal."""
    assert resync.get_signal("protrack", "prod") is not resync.get_signal("protrack", "test")


def test_request_resync_sin_worker_registrado():
    """Pedir resync de una integración que no arrancó no debe explotar."""
    assert resync.request_resync("inexistente", "prod") is False


def test_request_resync_con_worker_registrado():
    resync.get_signal("protrack", "prod")
    assert resync.request_resync("protrack", "prod") is True


def test_request_resync_activa_la_señal():
    sig = resync.get_signal("protrack", "prod")
    assert not sig.is_set()
    resync.request_resync("protrack", "prod")
    assert sig.is_set()


# ── sleep_or_resync ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sleep_or_resync_cumple_el_timeout():
    """Sin pedido manual, duerme el tiempo indicado y retorna False."""
    woke = await resync.sleep_or_resync("protrack", "prod", 0.05)
    assert woke is False


@pytest.mark.asyncio
async def test_sleep_or_resync_despierta_por_pedido_manual():
    """
    El caso de uso real: el operador corrige credenciales y no quiere esperar
    los 5 minutos (o las 23 horas) del próximo ciclo.
    """
    resync.get_signal("protrack", "prod")

    async def pedir():
        await asyncio.sleep(0.02)
        resync.request_resync("protrack", "prod")

    asyncio.create_task(pedir())
    # Timeout largo: si no despertara por la señal, el test tardaría 10s
    woke = await asyncio.wait_for(
        resync.sleep_or_resync("protrack", "prod", 10), timeout=2
    )
    assert woke is True


@pytest.mark.asyncio
async def test_signal_se_limpia_tras_despertar():
    """Un resync no debe disparar los ciclos siguientes."""
    resync.get_signal("protrack", "prod")
    resync.request_resync("protrack", "prod")

    primero = await resync.sleep_or_resync("protrack", "prod", 5)
    assert primero is True

    segundo = await resync.sleep_or_resync("protrack", "prod", 0.05)
    assert segundo is False


@pytest.mark.asyncio
async def test_resync_no_afecta_otras_integraciones():
    resync.get_signal("protrack", "prod")
    resync.get_signal("protrack", "test")
    resync.request_resync("protrack", "prod")

    otro = await resync.sleep_or_resync("protrack", "test", 0.05)
    assert otro is False


# ── Descarte por falta de traducción ─────────────────────────────────────────

def test_mapper_descarta_id_sin_traduccion():
    """
    REGRESIÓN — decisión de negocio.

    Si el proveedor no tiene cargada la patente de un dispositivo, el evento NO
    se envía a RC. Enviar el ID crudo (o un "0" genérico) produce activos no
    identificables y, peor, colapsa varios dispositivos distintos en el mismo
    identificador. Es responsabilidad del proveedor completar la asignación.
    """
    from unittest.mock import MagicMock, patch
    from app.core.dynamic_mapper import DynamicMapper

    schema = {"chassis_number": "imei", "latitude": "lat", "longitude": "lng"}
    payload = {"imei": "868307060968914", "lat": 9.98, "lng": -84.73}

    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with patch("app.database.get_session", return_value=mock_db):
        resultado = DynamicMapper.map_payload(
            payload, schema, "protrack", "prod", require_dict_match=True
        )

    assert resultado is None


def test_mapper_no_descarta_si_no_se_exige_traduccion():
    """Un proveedor sin diccionario configurado no debe verse afectado."""
    from unittest.mock import MagicMock, patch
    from app.core.dynamic_mapper import DynamicMapper

    schema = {"chassis_number": "imei", "latitude": "lat", "longitude": "lng"}
    payload = {"imei": "868307060968914", "lat": 9.98, "lng": -84.73}

    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with patch("app.database.get_session", return_value=mock_db):
        resultado = DynamicMapper.map_payload(
            payload, schema, "protrack", "prod", require_dict_match=False
        )

    assert resultado is not None
    assert resultado.chassis_number == "868307060968914"


def test_mapper_traduce_cuando_hay_match():
    from unittest.mock import MagicMock, patch
    from app.core.dynamic_mapper import DynamicMapper

    schema = {"chassis_number": "imei", "latitude": "lat", "longitude": "lng"}
    payload = {"imei": "868307060968914", "lat": 9.98, "lng": -84.73}

    entry = MagicMock()
    entry.dict_value = "C180673"
    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = entry

    with patch("app.database.get_session", return_value=mock_db):
        resultado = DynamicMapper.map_payload(
            payload, schema, "protrack", "prod", require_dict_match=True
        )

    assert resultado is not None
    assert resultado.chassis_number == "C180673"


def test_multi_descarta_todo_el_payload_sin_traduccion():
    """Si el activo no es identificable, tampoco se emiten sus eventos de trigger."""
    from unittest.mock import MagicMock, patch
    from app.core.dynamic_mapper import DynamicMapper

    full_schema = {
        "base_mapping": {"chassis_number": "imei", "latitude": "lat", "longitude": "lng"},
        "trigger_rules": [
            {"enabled": True, "field": "acc", "operator": "eq", "value": "1", "rc_code": "12"}
        ],
        "default_rule": {"enabled": True, "rc_code": "1"},
    }
    payload = {"imei": "868307060968914", "lat": 9.98, "lng": -84.73, "acc": "1"}

    mock_db = MagicMock()
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with patch("app.database.get_session", return_value=mock_db):
        resultado = DynamicMapper.map_payload_multi(
            payload, full_schema, "protrack", "prod", require_dict_match=True
        )

    assert resultado == []


# ── El lookup del diccionario solo ocurre si el proveedor lo usa ─────────────

def test_sin_diccionario_no_se_consulta_la_base():
    """
    Un proveedor que identifica por patente y no tiene tabla de traducción
    pagaba una consulta por evento para no encontrar nada. En el camino PUSH
    eso ocurre dentro del request, contra el SLA de recepción.
    """
    from unittest.mock import MagicMock, patch
    from app.core.dynamic_mapper import DynamicMapper

    schema = {"chassis_number": "placa", "latitude": "lat", "longitude": "lng"}
    payload = {"placa": "AB123CD", "lat": 9.98, "lng": -84.73}

    db = MagicMock()
    with patch("app.database.get_session", return_value=db) as mock_sesion:
        resultado = DynamicMapper.map_payload(
            payload, schema, "proveedor_push", "prod",
            require_dict_match=False, usar_diccionario=False,
        )

    assert resultado is not None
    assert resultado.chassis_number == "AB123CD"
    mock_sesion.assert_not_called()


def test_con_diccionario_si_se_consulta():
    """Los proveedores que traducen por diccionario deben seguir haciéndolo."""
    from unittest.mock import MagicMock, patch
    from app.core.dynamic_mapper import DynamicMapper

    schema = {"chassis_number": "imei", "latitude": "lat", "longitude": "lng"}
    payload = {"imei": "868307060968914", "lat": 9.98, "lng": -84.73}

    entrada = MagicMock()
    entrada.dict_value = "C180673"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = entrada

    with patch("app.database.get_session", return_value=db) as mock_sesion:
        resultado = DynamicMapper.map_payload(
            payload, schema, "protrack", "prod",
            require_dict_match=True, usar_diccionario=True,
        )

    assert resultado.chassis_number == "C180673"
    mock_sesion.assert_called()


def test_el_valor_por_defecto_conserva_el_comportamiento_anterior():
    """Un llamador que no pase el parámetro debe seguir consultando."""
    import inspect
    from app.core.dynamic_mapper import DynamicMapper

    firma = inspect.signature(DynamicMapper.map_payload)
    assert firma.parameters["usar_diccionario"].default is True
