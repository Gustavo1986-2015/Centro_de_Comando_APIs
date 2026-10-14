"""
v1.9.9 — 1.6: RC pide reintentar y el hub lo daba por perdido.

Medido en producción (v1.9.8, 13/10): RC devolvió en la excepción
`severity: Retry` (CGI:SESSION_STORE_UNAVAILABLE, code 4023) y el hub lo trató
como rechazo de negocio permanente: 35 eventos quedaron 'failed' sin reintento.
Estos tests EJECUTAN el cliente SOAP y el worker con ese texto exacto de RC.
"""
import asyncio
import logging

import pytest

from app.services import rc_soap
from app.services.rc_soap import RCResponseCategory

MENSAJE_RC = ("El servicio de autenticación no está disponible temporalmente. Intente nuevamente. "
              "Timeout performing GET (5000ms)...")


def _respuesta_rc(severidad="Retry", key="CGI:SESSION_STORE_UNAVAILABLE", code="4023"):
    """La respuesta de RC del 13/10, como la entrega zeep ya serializada."""
    pares = [{"Key": "key", "Value": key}, {"Key": "code", "Value": code}]
    if severidad is not None:
        pares.append({"Key": "severity", "Value": severidad})
    pares += [{"Key": "layer", "Value": "CGI"}, {"Key": "message", "Value": MENSAJE_RC}]
    return {"exception": {"KeyValueOfstringstring": pares}, "idJob": 0}


def _eventos(n):
    from app.schemas.canonical import RCCanonicalModel
    return [RCCanonicalModel(chassis_number=f"P{i}", latitude=1.0, longitude=2.0) for i in range(n)]


def _enviar(monkeypatch, respuesta, n=3):
    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", lambda self, ev: respuesta)
    cliente = rc_soap.RCSOAPClient(username="u", password="p", use_mock=False)
    return asyncio.run(cliente.send_events_batch(_eventos(n)))


# ─── Cliente SOAP ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("severidad", ["Retry", "RETRY", "retry", " Retry "])
def test_severity_retry_es_transitorio_y_lo_dice_en_el_log(monkeypatch, caplog, severidad):
    with caplog.at_level(logging.WARNING, logger="app.services.rc_soap"):
        resultados = _enviar(monkeypatch, [_respuesta_rc(severidad)])
    assert len(resultados) == 3
    for exito, job_id, respuesta, categoria in resultados:
        assert (exito, categoria) == (False, RCResponseCategory.TRANSPORT)
        assert job_id.startswith("rc_retry_")
        assert "SESSION_STORE_UNAVAILABLE" in respuesta and "4023" in respuesta
    assert "[RC:REINTENTO] RC pidió reintentar (CGI:SESSION_STORE_UNAVAILABLE, 4023)" in caplog.text
    assert "[RC:BUSINESS]" not in caplog.text


@pytest.mark.parametrize("severidad", [None, "Error", "Fatal", ""])
def test_sin_severity_retry_sigue_siendo_rechazo_de_negocio(monkeypatch, caplog, severidad):
    with caplog.at_level(logging.WARNING, logger="app.services.rc_soap"):
        resultados = _enviar(monkeypatch, [_respuesta_rc(severidad)])
    assert {r[3] for r in resultados} == {RCResponseCategory.BUSINESS}
    assert all(r[1].startswith("rc_business_") for r in resultados)
    assert "[RC:REINTENTO]" not in caplog.text


def test_el_caso_de_autenticacion_no_cambia(monkeypatch):
    """SQL:USERUNK sigue siendo AUTH, aunque traiga severity: Retry."""
    for severidad in (None, "Retry"):
        resultados = _enviar(monkeypatch, [_respuesta_rc(severidad, key="SQL:USERUNK", code="1")], n=1)
        assert resultados[0][3] == RCResponseCategory.AUTH and resultados[0][1].startswith("rc_auth_")


def test_respuesta_por_evento_mezclada(monkeypatch):
    """Con una respuesta por evento, cada uno se clasifica por la suya."""
    resultados = _enviar(monkeypatch, [{"exception": None, "idJob": 77}, _respuesta_rc("Retry"),
                                       _respuesta_rc(None)])
    assert [r[3] for r in resultados] == [RCResponseCategory.SUCCESS, RCResponseCategory.TRANSPORT,
                                          RCResponseCategory.BUSINESS]


# ─── Worker real ─────────────────────────────────────────────────────────────

def _integracion_schmitz():
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          use_mock=False, rc_user="AC_avl_SchmitzCargoBull", rc_password_enc=encrypt("x")))
    db.commit()
    db.close()


def _ciclo(monkeypatch, respuesta, eventos=1, prefijo="r16"):
    """Encola `eventos` de Schmitz y corre UN ciclo real del worker; RC responde `respuesta`."""
    from app.api.routers import schmitz
    from app.worker import processor
    schmitz._persist_batch([({"ChassisNumber": f"{prefijo}{i:03d}", "DeviceTime": "2026-10-13T12:00:00Z",
                              "StatusData": [{"Position": {"Latitude": 42.44, "Longitude": -3.49}}]},
                             "prod", f"{prefijo}-{i}") for i in range(eventos)])
    llamadas = []

    def rc(self, ev):
        llamadas.append(len(ev))
        return respuesta(ev) if callable(respuesta) else respuesta

    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", rc)
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    asyncio.run(processor.process_provider_events("schmitz", "prod"))
    return llamadas


def _filas(prefijo):
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("schmitz", "prod")
    try:
        return [(e.status, e.retry_count or 0, e.next_retry_at, e.job_id or "", e.rc_response or "")
                for e in db.query(NormalizedRCEvent).filter(NormalizedRCEvent.chassis_number.like(f"{prefijo}%"))
                .order_by(NormalizedRCEvent.id)]
    finally:
        db.close()


@pytest.fixture
def circuito_nuevo(monkeypatch):
    from app.worker import processor
    nuevo = processor.CircuitBreaker(failure_threshold=5, recovery_timeout=60, max_timeout=600)
    monkeypatch.setattr(processor, "_rc_circuit_breaker", nuevo)
    return nuevo


def test_cableado_el_worker_deja_pending_con_reintento(config_aislada, monkeypatch, circuito_nuevo):
    _integracion_schmitz()
    _ciclo(monkeypatch, [_respuesta_rc("Retry")], eventos=3)
    filas = _filas("r16")
    assert len(filas) == 3
    for estado, reintentos, proximo, job_id, respuesta in filas:
        assert (estado, reintentos) == ("pending", 1)
        assert proximo is not None, "sin reintento programado"
        assert job_id.startswith("rc_retry_")
        assert "SESSION_STORE_UNAVAILABLE" in respuesta, "se perdió la respuesta de RC"
    assert circuito_nuevo._failure_count == 1, "no contó para el circuito"


def test_cableado_un_rechazo_sin_severity_retry_sigue_failed(config_aislada, monkeypatch, circuito_nuevo):
    _integracion_schmitz()
    _ciclo(monkeypatch, [_respuesta_rc(None)], eventos=2)
    assert [(f[0], f[1]) for f in _filas("r16")] == [("failed", 0), ("failed", 0)]
    assert circuito_nuevo._failure_count == 0, "un rechazo de negocio no es una caída de RC"


def test_cableado_con_el_corte_sostenido_el_circuito_se_abre(config_aislada, monkeypatch, circuito_nuevo):
    """Cinco ciclos seguidos con RC pidiendo reintentar abren el circuito; el sexto no llama a RC."""
    _integracion_schmitz()
    for ciclo in range(5):
        assert _ciclo(monkeypatch, [_respuesta_rc("Retry")], prefijo=f"c{ciclo}") == [1]
    assert circuito_nuevo.is_open
    assert _ciclo(monkeypatch, [_respuesta_rc("Retry")], prefijo="c5") == [], "con el circuito abierto llamó a RC"
    assert [f[0] for f in _filas("c5")] == ["pending"]
    assert all(f[0] == "pending" for c in range(5) for f in _filas(f"c{c}")), "algún evento quedó perdido"
