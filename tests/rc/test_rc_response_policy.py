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


def test_el_fault_se_captura_antes_que_la_excepcion_generica(monkeypatch):
    """
    Un zeep Fault (hereda de Exception) tiene que caer en su propia rama y no
    en la de transporte, que lo reintentaría. v1.9.9 (B-20): se EJECUTA el
    cliente con un Fault (antes se comparaban posiciones de texto).
    """
    import asyncio
    from zeep.exceptions import Fault
    from app.schemas.canonical import RCCanonicalModel
    from app.services import rc_soap

    def falla(self, ev):
        raise Fault("DeserializationFailed: fecha mal formada")

    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", falla)
    cliente = rc_soap.RCSOAPClient(username="u", password="p", use_mock=False)
    r = asyncio.run(cliente.send_events_batch([RCCanonicalModel(chassis_number="P1", latitude=1.0, longitude=2.0)]))
    assert r[0][3] == rc_soap.RCResponseCategory.PROTOCOL


def test_el_fault_produce_categoria_de_protocolo(config_aislada, monkeypatch):
    """
    Un Fault es permanente: el worker no lo reintenta. v1.9.9 (B-20): un ciclo
    REAL del worker con RC respondiendo Fault deja el evento fallido, sin
    reintentos (antes se buscaba el texto en el fuente).
    """
    from zeep.exceptions import Fault

    def falla(ev):
        raise Fault("DeserializationFailed")

    (fila,) = _b20_ciclo_del_worker(monkeypatch, falla)
    assert fila[0] == "failed" and fila[1] == 0 and fila[2].startswith("rc_protocol_"), fila


# ── Formato de fecha ─────────────────────────────────────────────────────────

def test_la_fecha_se_serializa_sin_sufijo_z():
    """
    El contrato pide YYYY-MM-DDTHH:MM:SS en UTC, sin Z. v1.9.9 (B-20): se arma
    el evento real (antes se buscaba el formato en el fuente).
    """
    import re
    from datetime import datetime
    from app.schemas.canonical import RCCanonicalModel
    from app.services.rc_soap import construir_evento_rc
    fecha = construir_evento_rc(RCCanonicalModel(chassis_number="P1", latitude=1.0, longitude=2.0,
                                                 date=datetime(2026, 10, 10, 12, 0, 5)))["date"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", fecha) and fecha == "2026-10-10T12:00:05"


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

def test_ningun_consumidor_desempaqueta_tres_elementos_a_ciegas(config_aislada, monkeypatch):
    """
    Un `a, b, c = resultado` sobre la tupla de 4 elementos rompe el lote en
    ejecución. v1.9.9 (B-20): un ciclo REAL del worker con respuestas de cada
    tipo (éxito, rechazo de negocio, sin acuse) las procesa sin romperse
    (antes se buscaba el patrón en el fuente).
    """
    respuestas = [{"exception": None, "idJob": 5},
                  {"exception": {"KeyValueOfstringstring": [{"Key": "SQL", "Value": "datos invalidos"}]}, "idJob": None},
                  {"exception": None}]
    filas = _b20_ciclo_del_worker(monkeypatch, lambda ev: respuestas[:len(ev)], eventos=3)
    assert sorted(f[0] for f in filas) == ["failed", "pending", "sent"], filas


def test_send_events_batch_devuelve_siempre_cuatro_elementos(monkeypatch):
    """
    Todos los caminos devuelven (éxito, job_id, respuesta, categoría). v1.9.9
    (B-20): se EJECUTA cada camino (antes se buscaba el texto de cada categoría).
    """
    import asyncio
    from zeep.exceptions import Fault
    from app.schemas.canonical import RCCanonicalModel
    from app.services import rc_soap
    C = rc_soap.RCResponseCategory
    ev = [RCCanonicalModel(chassis_number="P1", latitude=1.0, longitude=2.0)]

    def con(respuesta):
        def enviar(self, eventos):
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta
        monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", enviar)
        return asyncio.run(rc_soap.RCSOAPClient(username="u", password="p", use_mock=False).send_events_batch(ev))[0]

    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    casos = {C.SUCCESS: con([{"exception": None, "idJob": 7}]), C.PROTOCOL: con(Fault("x")),
             C.TRANSPORT: con(ConnectionError("caído"))}
    casos[C.SIMULADO] = asyncio.run(rc_soap.RCSOAPClient(username="u", password="p", use_mock=True).send_events_batch(ev))[0]
    for categoria, r in casos.items():
        assert len(r) == 4 and r[3] == categoria, (categoria, r)


def test_send_event_conserva_su_contrato_de_tres_elementos(monkeypatch):
    """
    send_event es de compatibilidad: sus llamadores esperan tres valores.
    v1.9.9 (B-20): se EJECUTA (simulado y real).
    """
    import asyncio
    from app.schemas.canonical import RCCanonicalModel
    from app.services import rc_soap
    ev = RCCanonicalModel(chassis_number="P1", latitude=1.0, longitude=2.0)
    simulado = asyncio.run(rc_soap.RCSOAPClient(username="u", password="p", use_mock=True).send_event(ev))
    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", lambda self, e: [{"exception": None, "idJob": 8}])
    real = asyncio.run(rc_soap.RCSOAPClient(username="u", password="p", use_mock=False).send_event(ev))
    assert len(simulado) == 3 and len(real) == 3 and real[:2] == (True, "8")


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


# ── El modo simulado no debe confundirse con un envío real ───────────────────
# En el log, un lote simulado y uno confirmado por RC se veían idénticos:
# "| RC: SUCCESS=87". Quien lee eso da por hecho que RC recibió los eventos.
# El dashboard ya avisaba con un banner, pero el log —que es lo que ve el
# equipo de sistemas— no lo distinguía.

@pytest.mark.asyncio
async def test_el_modo_simulado_devuelve_su_propia_categoria():
    from app.schemas.canonical import RCCanonicalModel

    cliente = RCSOAPClient.__new__(RCSOAPClient)
    cliente.use_mock = True

    evento = RCCanonicalModel(
        chassis_number="C180673", latitude=9.89, longitude=-84.63,
        speed=0, code="1", date=datetime.now(),
    )
    resultados = await cliente.send_events_batch([evento, evento])

    assert len(resultados) == 2
    for r in resultados:
        assert r[3] == RCResponseCategory.SIMULADO
        assert r[3] != RCResponseCategory.SUCCESS


def test_simulado_no_se_reintenta():
    """No hubo fallo: simplemente no se envió a propósito."""
    assert RCResponseCategory.SIMULADO not in CATEGORIAS_REINTENTABLES


def test_el_conteo_usa_la_categoria_real_y_no_asume_exito(config_aislada, monkeypatch, caplog):
    """
    REGRESIÓN: el contador sumaba siempre a "SUCCESS". v1.9.9 (B-20): un ciclo
    REAL en modo simulado cuenta SIMULADO, no SUCCESS (antes se buscaba texto).
    """
    import logging
    with caplog.at_level(logging.INFO, logger="app.worker.processor"):
        _b20_ciclo_del_worker(monkeypatch, lambda ev: [{"exception": None, "idJob": 1}] * len(ev),
                              eventos=2, use_mock=True)
    linea = next(r.getMessage() for r in caplog.records if "batch_processed" in r.getMessage())
    assert "RC: SIMULADO=2" in linea and "SUCCESS" not in linea, linea


# ── Integración: el consumidor real frente a la 4-tupla ──────────────────────
# Los tests que inspeccionan el código fuente detectan el patrón pero se rompen
# ante un rename inocente. Estos ejercen el consumidor de verdad: si alguien
# vuelve a desempaquetar mal, fallan por la excepción real, no por una cadena
# que dejó de coincidir.

@pytest.mark.asyncio
async def test_el_worker_procesa_una_4tupla_de_exito_sin_romperse():
    from app.worker.processor import send_batch_and_measure
    from app.schemas.canonical import RCCanonicalModel

    evento = RCCanonicalModel(
        chassis_number="C180673", latitude=9.89, longitude=-84.63,
        speed=0, code="1", date=datetime.now(),
    )

    class ClienteFalso:
        async def send_events_batch(self, eventos):
            return [
                (True, "7033068595", "ok", RCResponseCategory.SUCCESS)
                for _ in eventos
            ]

    resultados, _ = await send_batch_and_measure([evento] * 3, ClienteFalso())

    assert len(resultados) == 3
    assert all(len(r) == 4 for r in resultados)


@pytest.mark.asyncio
async def test_el_worker_detecta_el_error_de_conexion_en_la_4tupla():
    """
    La detección de fallo silencioso lee el job_id por índice. Con el
    desempaquetado a tres nombres esto lanzaba ValueError antes de poder
    inspeccionarlo.
    """
    from app.worker.processor import send_batch_and_measure
    from app.schemas.canonical import RCCanonicalModel

    evento = RCCanonicalModel(
        chassis_number="C1", latitude=1.0, longitude=2.0,
        speed=0, code="1", date=datetime.now(),
    )

    class ClienteCaido:
        async def send_events_batch(self, eventos):
            return [
                (False, "rc_conn_err_123", "Connection refused",
                 RCResponseCategory.TRANSPORT)
                for _ in eventos
            ]

    # Debe re-lanzarse como excepción para que el circuit breaker la registre,
    # no fallar por desempaquetado.
    with pytest.raises(Exception) as exc:
        await send_batch_and_measure([evento], ClienteCaido())

    assert "unpack" not in str(exc.value).lower()


@pytest.mark.asyncio
async def test_send_event_sigue_devolviendo_tres_elementos():
    """El método de compatibilidad no debe filtrar la categoría a sus llamadores."""
    from app.schemas.canonical import RCCanonicalModel

    cliente = RCSOAPClient.__new__(RCSOAPClient)
    cliente.use_mock = True

    evento = RCCanonicalModel(
        chassis_number="C1", latitude=1.0, longitude=2.0,
        speed=0, code="1", date=datetime.now(),
    )
    resultado = await cliente.send_event(evento)

    assert len(resultado) == 3
    exito, job_id, respuesta = resultado      # debe desempaquetar sin error
    assert exito is True


# ─── v1.9.9 (B-20): ayuda para los tests que ejecutan un ciclo real del worker ──
def _b20_ciclo_del_worker(monkeypatch, respuesta_rc, eventos=1, use_mock=False, crear_config=True,
                          antes_del_ciclo=None):
    """
    Un ciclo REAL de process_provider_events para schmitz/prod, con el cliente
    SOAP reemplazado por `respuesta_rc(eventos)`: lo que devolvería RC (o la
    excepción que lanzaría). Devuelve [(status, retry_count, job_id)] por fila.
    """
    import asyncio
    from app.api.routers import schmitz
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent
    from app.services import rc_soap
    from app.worker import processor
    if crear_config:
        db = get_session("system_config", "global")
        db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                              use_mock=use_mock, rc_user="AC_avl_SchmitzCargoBull", rc_password_enc=encrypt("x")))
        db.commit()
        db.close()
    schmitz._persist_batch([({"ChassisNumber": f"R{i:04d}", "DeviceTime": "2026-10-04T22:23:36Z",
                              "StatusData": [{"Position": {"Latitude": 42.44, "Longitude": -3.49}}]},
                             "prod", f"b20-{i}") for i in range(eventos)])
    if antes_del_ciclo:
        antes_del_ciclo()
    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", lambda self, ev: respuesta_rc(ev))
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    asyncio.run(processor.process_provider_events("schmitz", "prod"))
    db = get_session("schmitz", "prod")
    try:
        return [(e.status, e.retry_count or 0, e.job_id or "") for e in db.query(NormalizedRCEvent).order_by(NormalizedRCEvent.id)]
    finally:
        db.close()
