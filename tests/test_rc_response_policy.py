"""
Tests de la clasificación de respuestas de Recurso Confiable (hallazgo A3).

Los fixtures reproducen las respuestas del contrato D-TI-15 v14 tal como están
documentadas, para que la clasificación quede atada al contrato y no a una
interpretación.

El problema que se corrige: la decisión de reintentar o descartar se tomaba
buscando palabras sueltas en el texto de la respuesta. Un fallo transitorio
cuyo mensaje no contuviera esas palabras se marcaba como fallo permanente, y
se perdía en la purga de 24 horas sin dejar rastro.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from zeep.exceptions import Fault

from app.services.rc_soap import (
    RCSOAPClient,
    RCResponseCategory,
    CATEGORIAS_REINTENTABLES,
)


@pytest.fixture
def cliente():
    c = RCSOAPClient.__new__(RCSOAPClient)
    c._clear_token_cache = MagicMock()
    return c


# ── Fixtures del contrato D-TI-15 v14 ────────────────────────────────────────

# Response GPSAssetTracking exitoso (pág. 10): exception nil, idJob presente
RESPUESTA_EXITO = {"exception": None, "idJob": 7033068595}

# El cliente SOAP puede materializar el nodo nil como lista vacía
RESPUESTA_EXITO_LISTA_VACIA = {
    "exception": {"KeyValueOfstringstring": []},
    "idJob": 7033068595,
}

# Error de autenticación (pág. 11): SQL:USERUNK
RESPUESTA_AUTH = {
    "exception": {
        "KeyValueOfstringstring": [
            {"Key": "key", "Value": "SQL:USERUNK"},
            {"Key": "code", "Value": "2004"},
            {"Key": "severity", "Value": "User"},
            {"Key": "layer", "Value": "SQL"},
            {"Key": "message", "Value": "Autentificación incorrecta (Usuario, Contraseña o Token)."},
            {"Key": "message_args", "Value": "59339832-5634-BBF9-C42B-E1A8409B12C4"},
        ]
    },
    "idJob": None,
}

# Rechazo de negocio sin relación con credenciales
RESPUESTA_NEGOCIO = {
    "exception": {
        "KeyValueOfstringstring": [
            {"Key": "key", "Value": "CGI:UNKNOWN"},
            {"Key": "code", "Value": "5001"},
            {"Key": "message", "Value": "Error desconocido."},
        ]
    },
    "idJob": None,
}

# s:Fault por formato de fecha inválido (pág. 12)
FAULT_DESERIALIZACION = (
    "El formateador inició una excepción al intentar deserializar el mensaje: "
    "El valor '2021-03-0213:01:00' no se puede analizar como tipo 'DateTime'."
)


# ── SUCCESS ──────────────────────────────────────────────────────────────────

def test_respuesta_exitosa_del_contrato(cliente):
    exito, job_id, _, categoria = cliente._parse_single_response(RESPUESTA_EXITO)

    assert exito is True
    assert job_id == "7033068595"
    assert categoria == RCResponseCategory.SUCCESS


def test_exception_como_lista_vacia_tambien_es_exito(cliente):
    """Un nodo exception presente pero vacío no es un error."""
    exito, job_id, _, categoria = cliente._parse_single_response(RESPUESTA_EXITO_LISTA_VACIA)

    assert exito is True
    assert job_id == "7033068595"
    assert categoria == RCResponseCategory.SUCCESS


def test_el_exito_no_se_reintenta():
    assert RCResponseCategory.SUCCESS not in CATEGORIAS_REINTENTABLES


# ── AUTH ─────────────────────────────────────────────────────────────────────

def test_error_de_autenticacion_se_clasifica_como_auth(cliente):
    exito, _, _, categoria = cliente._parse_single_response(RESPUESTA_AUTH)

    assert exito is False
    assert categoria == RCResponseCategory.AUTH


def test_el_error_de_autenticacion_limpia_el_token(cliente):
    """Sin renovar el token, todos los reintentos fallarían igual."""
    cliente._parse_single_response(RESPUESTA_AUTH)
    cliente._clear_token_cache.assert_called_once()


def test_el_error_de_autenticacion_se_reintenta():
    """
    Es el único caso permanente en apariencia que sí conviene reintentar:
    con el token renovado, el mismo evento se acepta.
    """
    assert RCResponseCategory.AUTH in CATEGORIAS_REINTENTABLES


# ── BUSINESS ─────────────────────────────────────────────────────────────────

def test_rechazo_de_negocio_se_clasifica_como_business(cliente):
    exito, _, _, categoria = cliente._parse_single_response(RESPUESTA_NEGOCIO)

    assert exito is False
    assert categoria == RCResponseCategory.BUSINESS


def test_el_rechazo_de_negocio_no_limpia_el_token(cliente):
    """El token es válido: el problema está en los datos del evento."""
    cliente._parse_single_response(RESPUESTA_NEGOCIO)
    cliente._clear_token_cache.assert_not_called()


def test_idjob_cero_es_rechazo_de_negocio(cliente):
    """idJob=0 significa que RC no registró el pulso."""
    exito, _, _, categoria = cliente._parse_single_response({"exception": None, "idJob": 0})

    assert exito is False
    assert categoria == RCResponseCategory.BUSINESS


def test_el_rechazo_de_negocio_no_se_reintenta():
    """Reenviar los mismos datos produciría el mismo rechazo."""
    assert RCResponseCategory.BUSINESS not in CATEGORIAS_REINTENTABLES


# ── TRANSPORT ────────────────────────────────────────────────────────────────

def test_sin_respuesta_se_trata_como_transporte(cliente):
    """
    No poder afirmar que RC recibió el evento no equivale a saber que lo
    rechazó: ante la duda se reintenta.
    """
    exito, _, _, categoria = cliente._parse_single_response(None)

    assert exito is False
    assert categoria == RCResponseCategory.TRANSPORT


def test_respuesta_sin_idjob_ni_excepcion_se_reintenta(cliente):
    """Respuesta bien formada pero sin acuse: no se puede dar por enviada."""
    exito, _, _, categoria = cliente._parse_single_response({"algo": "inesperado"})

    assert exito is False
    assert categoria == RCResponseCategory.TRANSPORT


def test_el_transporte_se_reintenta():
    assert RCResponseCategory.TRANSPORT in CATEGORIAS_REINTENTABLES


# ── PROTOCOL ─────────────────────────────────────────────────────────────────

def test_el_fault_de_wcf_no_se_reintenta():
    """
    REGRESIÓN: un s:Fault por formato caía en el mismo balde que un timeout y
    se reintentaba cuatro veces, cuando reenviar los mismos bytes produce
    siempre el mismo error.
    """
    assert RCResponseCategory.PROTOCOL not in CATEGORIAS_REINTENTABLES


def test_el_fault_se_captura_antes_que_la_excepcion_generica():
    """
    El orden de los except importa: zeep.exceptions.Fault hereda de Exception,
    así que debe capturarse primero. Si quedara después, un Fault permanente
    caería en la rama de transporte y se reintentaría cuatro veces.
    """
    import inspect
    from app.services import rc_soap

    fuente = inspect.getsource(rc_soap.RCSOAPClient.send_events_batch)
    pos_fault = fuente.find("except Fault")
    pos_generica = fuente.find("except Exception")

    assert pos_fault != -1, "send_events_batch debe capturar zeep Fault"
    assert pos_fault < pos_generica, "El except Fault debe ir antes del genérico"


def test_el_fault_produce_categoria_de_protocolo():
    """El bloque de Fault debe clasificar como PROTOCOL, no como TRANSPORT."""
    import inspect
    from app.services import rc_soap

    fuente = inspect.getsource(rc_soap.RCSOAPClient.send_events_batch)
    bloque_fault = fuente[fuente.find("except Fault"):fuente.find("except Exception")]

    assert "RCResponseCategory.PROTOCOL" in bloque_fault
    assert "RCResponseCategory.TRANSPORT" not in bloque_fault


# ── Formato de fecha ─────────────────────────────────────────────────────────

def test_la_fecha_se_serializa_sin_sufijo_z():
    """
    El contrato especifica YYYY-MM-DDTHH:MM:SS en UTC, y así lo envía el
    cliente de referencia en producción. RC valida el formato de forma estricta.
    """
    import inspect
    from app.services import rc_soap

    fuente = inspect.getsource(rc_soap)
    assert '"%Y-%m-%dT%H:%M:%SZ"' not in fuente, "La fecha no debe llevar sufijo Z"
    assert '"%Y-%m-%dT%H:%M:%S"' in fuente


def test_el_formato_coincide_con_el_ejemplo_del_contrato():
    """El contrato ilustra 2020-07-15T10:12:00 para el nodo iron:date."""
    fecha = datetime(2020, 7, 15, 10, 12, 0)
    assert fecha.strftime("%Y-%m-%dT%H:%M:%S") == "2020-07-15T10:12:00"


# ── La clasificación no depende del texto ────────────────────────────────────

def test_un_fallo_transitorio_sin_palabras_clave_se_reintenta(cliente):
    """
    REGRESIÓN — este es el caso que provocaba la pérdida silenciosa.

    Un 503 de RC cuyo mensaje no contenga "token", "connection" ni las demás
    palabras que se buscaban, se clasificaba como fallo permanente y se perdía
    en la purga. Ahora la decisión no depende del texto.
    """
    respuesta = {"algo": "Service Unavailable"}   # sin ninguna palabra clave
    exito, _, _, categoria = cliente._parse_single_response(respuesta)

    assert exito is False
    assert categoria in CATEGORIAS_REINTENTABLES


def test_un_rechazo_de_negocio_con_la_palabra_token_no_se_confunde(cliente):
    """
    La inversa: un mensaje de negocio que mencione "token" por casualidad no
    debe tratarse como problema de autenticación... salvo que efectivamente
    lo sea según los marcadores del contrato.
    """
    respuesta = {
        "exception": {
            "KeyValueOfstringstring": [
                {"Key": "key", "Value": "CGI:UNKNOWN"},
                {"Key": "message", "Value": "El campo asset no puede estar vacío."},
            ]
        },
        "idJob": None,
    }
    exito, _, _, categoria = cliente._parse_single_response(respuesta)

    assert exito is False
    assert categoria == RCResponseCategory.BUSINESS
    cliente._clear_token_cache.assert_not_called()


# ── Contrato de la tupla de resultado ────────────────────────────────────────
# Estos tests existen porque el cambio de 3 a 4 elementos rompió la aplicación
# en ejecución mientras la suite seguía en verde: los tests unitarios validaban
# la clasificación, pero nadie verificaba que TODOS los consumidores estuvieran
# migrados. El error "too many values to unpack" apareció recién con tráfico real.

def test_ningun_consumidor_desempaqueta_tres_elementos_a_ciegas():
    """
    Un `a, b, c = resultado` sobre la tupla de 4 elementos rompe el
    procesamiento completo del lote en tiempo de ejecución.
    """
    import inspect
    import re
    from app.worker import processor

    fuente = inspect.getsource(processor)
    # Desempaquetado de exactamente tres nombres desde results[...]
    patron = re.compile(r"^\s*\w+,\s*\w+,\s*\w+\s*=\s*results\[", re.MULTILINE)
    coincidencias = patron.findall(fuente)

    assert not coincidencias, (
        f"Hay {len(coincidencias)} desempaquetado(s) de 3 elementos sobre results[...]. "
        f"La tupla ahora trae 4 (con la categoría)."
    )


def test_send_events_batch_devuelve_siempre_cuatro_elementos():
    """Todos los caminos de retorno deben incluir la categoría."""
    import inspect
    from app.services import rc_soap

    fuente = inspect.getsource(rc_soap.RCSOAPClient.send_events_batch)

    # Cada return de resultados debe mencionar una categoría
    for categoria in ("PROTOCOL", "TRANSPORT", "SUCCESS"):
        assert f"RCResponseCategory.{categoria}" in fuente, (
            f"El camino {categoria} no está devolviendo su categoría"
        )


def test_send_event_conserva_su_contrato_de_tres_elementos():
    """
    Este método es de compatibilidad y sus llamadores esperan tres valores:
    devolver cuatro los rompería.
    """
    import inspect
    from app.services import rc_soap

    fuente = inspect.getsource(rc_soap.RCSOAPClient.send_event)
    assert "primera[0], primera[1], primera[2]" in fuente or "results[0][:3]" in fuente


@pytest.mark.asyncio
async def test_el_lote_se_procesa_sin_error_de_desempaquetado():
    """
    REGRESIÓN de integración: reproduce el flujo real del worker sobre la tupla
    de 4 elementos. Con el bug, cada sub-lote moría con "too many values to
    unpack" y los 87 eventos iban a reintento sin haber salido nunca.
    """
    from app.worker.processor import send_batch_and_measure
    from app.schemas.canonical import RCCanonicalModel

    evento = RCCanonicalModel(
        chassis_number="C180673", latitude=9.89, longitude=-84.63,
        speed=0, code="1", date=datetime.now(),
    )

    respuesta = [
        (True, "7033068595", "ok", RCResponseCategory.SUCCESS),
        (True, "7033068596", "ok", RCResponseCategory.SUCCESS),
    ]

    class ClienteFalso:
        async def send_events_batch(self, eventos):
            return respuesta

    cliente_falso = ClienteFalso()

    resultados, transcurrido = await send_batch_and_measure(
        [evento, evento], cliente_falso
    )

    assert len(resultados) == 2
    assert all(len(r) == 4 for r in resultados)
    assert transcurrido >= 0
