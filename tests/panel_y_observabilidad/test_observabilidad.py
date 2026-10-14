"""
Tests de dos puntos ciegos que aparecieron durante las pruebas de carga.

1. Un 401 por clave incorrecta no dejaba ningún rastro. Se acumularon 5.320
   rechazos en tres minutos y en el log del servidor no había una sola línea
   que explicara la causa: solo los códigos 401 de los access logs. Es el
   síntoma exacto de una integración mal configurada y tiene que ser visible.

2. El tiempo de respuesta del endpoint PUSH no sirve para saber si el pipeline
   aguanta: el handler responde apenas encola, así que mide sub-milisegundo
   aunque el consumidor esté atrasado. Lo que sí lo revela es la espera en cola.
"""
import time

import pytest

from app.core import auth_alerts
from app.core import queue_metrics


@pytest.fixture(autouse=True)
def _limpiar():
    auth_alerts.reset()
    queue_metrics.reset()
    yield
    auth_alerts.reset()
    queue_metrics.reset()


# ═══════════════════════════════════════════════════════════════════
# Visibilidad de los rechazos de autenticación
# ═══════════════════════════════════════════════════════════════════

def test_el_primer_rechazo_se_reporta_de_inmediato(caplog):
    """
    Sin esto, una clave mal cargada del lado del proveedor es invisible hasta
    que ellos reclaman.
    """
    with caplog.at_level("WARNING"):
        auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")

    assert any("SCHMITZ-test" in r.message for r in caplog.records)
    assert any("API key incorrecta" in r.message for r in caplog.records)


def test_una_avalancha_no_produce_una_linea_por_rechazo(caplog):
    """
    REGRESIÓN del diseño: a 40 mensajes por segundo, una línea por rechazo
    esconde el problema tanto como el silencio.
    """
    with caplog.at_level("WARNING"):
        for _ in range(5000):
            auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")

    assert len(caplog.records) <= 5, (
        f"Se emitieron {len(caplog.records)} líneas para 5.000 rechazos"
    )


def test_el_resumen_periodico_informa_el_acumulado(caplog, monkeypatch):
    monkeypatch.setattr(auth_alerts, "INTERVALO_RESUMEN_SEG", 0.2)

    with caplog.at_level("WARNING"):
        for _ in range(100):
            auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")
        time.sleep(0.25)
        for _ in range(100):
            auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")

    # El resumen se emite en el primer rechazo posterior al intervalo, no al
    # final de la ráfaga: informa lo acumulado hasta ese instante.
    resumenes = [r for r in caplog.records if "en total" in r.message]
    assert resumenes, "No se emitió ningún resumen acumulado"
    assert "101" in resumenes[0].message


def test_los_motivos_se_cuentan_por_separado():
    """
    Una clave equivocada y una clave sin configurar son problemas distintos y
    requieren acciones distintas.
    """
    auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")
    auth_alerts.registrar_rechazo("schmitz", "test", "falta configurar la API key")

    motivos = {e["motivo"] for e in auth_alerts.resumen_actual()}
    assert motivos == {"API key incorrecta", "falta configurar la API key"}


def test_cada_integracion_lleva_su_propio_recuento():
    auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")
    auth_alerts.registrar_rechazo("schmitz", "prod", "API key incorrecta")

    entornos = {e["env"] for e in auth_alerts.resumen_actual()}
    assert entornos == {"test", "prod"}


def test_un_episodio_terminado_no_se_lista_como_en_curso(monkeypatch):
    """
    v1.9.9 (B-25). Reemplaza a test_al_cesar_los_rechazos_queda_constancia,
    que ejecutaba cerrar_episodios_resueltos: la app nunca la llamaba (código
    muerto, se quitó). Lo que sí importa: un episodio sin rechazos desde hace
    más de SILENCIO_PARA_CERRAR_SEG ya no aparece en el panel como en curso.
    """
    monkeypatch.setattr(auth_alerts, "SILENCIO_PARA_CERRAR_SEG", 0.1)
    for _ in range(50):
        auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")
    assert auth_alerts.resumen_actual()[0]["total"] == 50
    time.sleep(0.15)
    assert auth_alerts.resumen_actual() == []


def test_un_problema_nuevo_no_se_suma_al_anterior(monkeypatch):
    """Dos episodios separados en el tiempo son incidentes distintos."""
    monkeypatch.setattr(auth_alerts, "SILENCIO_PARA_CERRAR_SEG", 0.1)

    auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")
    time.sleep(0.15)
    auth_alerts.registrar_rechazo("schmitz", "test", "API key incorrecta")

    assert auth_alerts.resumen_actual()[0]["total"] == 1


# ═══════════════════════════════════════════════════════════════════
# Espera en cola
# ═══════════════════════════════════════════════════════════════════

def test_la_espera_en_cola_se_acumula():
    queue_metrics.registrar_espera_cola("schmitz", [10.0, 20.0, 30.0], 5, 20000)
    stats = queue_metrics.obtener_estadisticas("schmitz")

    assert stats["muestras"] == 3
    assert stats["media_ms"] == pytest.approx(20.0)
    assert stats["max_ms"] == pytest.approx(30.0)


def test_se_informa_la_ocupacion_de_la_cola():
    """
    La profundidad distingue una espera alta puntual de una cola que se está
    llenando de verdad.
    """
    queue_metrics.registrar_espera_cola("schmitz", [5.0], 2000, 20000)
    stats = queue_metrics.obtener_estadisticas("schmitz")

    assert stats["profundidad"] == 2000
    assert stats["ocupacion_pct"] == pytest.approx(10.0)


def test_una_espera_excesiva_genera_alerta(caplog):
    """
    REGRESIÓN: este es el escenario que la métrica de respuesta del endpoint no
    detecta. El endpoint seguiría respondiendo en sub-milisegundo mientras los
    eventos esperan medio minuto para persistirse.
    """
    with caplog.at_level("WARNING"):
        queue_metrics.registrar_espera_cola("schmitz", [30000.0], 15000, 20000)

    assert any("atrasando" in r.message for r in caplog.records)
    assert any("30.0s" in r.message for r in caplog.records)


def test_una_espera_normal_no_genera_ruido(caplog):
    with caplog.at_level("WARNING"):
        for _ in range(100):
            queue_metrics.registrar_espera_cola("schmitz", [15.0, 22.0], 10, 20000)

    assert not caplog.records


def test_la_alerta_no_se_repite_en_cada_lote(caplog, monkeypatch):
    """Durante una congestión, repetir la advertencia en cada lote sería ruido."""
    with caplog.at_level("WARNING"):
        for _ in range(50):
            queue_metrics.registrar_espera_cola("schmitz", [30000.0], 15000, 20000)

    assert len(caplog.records) == 1


def test_la_memoria_esta_acotada():
    for _ in range(100):
        queue_metrics.registrar_espera_cola("schmitz", [1.0] * 200, 0, 20000)

    assert queue_metrics.obtener_estadisticas("schmitz")["muestras"] <= 5000


def test_sin_muestras_no_falla():
    stats = queue_metrics.obtener_estadisticas("inexistente")
    assert stats["muestras"] == 0
    assert stats["media_ms"] is None


# ═══════════════════════════════════════════════════════════════════
# La métrica de aceptación no promete lo que no mide
# ═══════════════════════════════════════════════════════════════════

def test_la_tarjeta_no_afirma_cumplimiento_de_sla(config_aislada):
    """
    Mostraba "SLA <250ms" sobre una métrica que solo mide el encolado. v1.9.9
    (B-20): se mira la página que SIRVE el hub (antes se leía el archivo).
    """
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard
    app = FastAPI()
    app.include_router(dashboard.router)
    r = TestClient(app).get("/dashboard", auth=(os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"]))
    assert r.status_code == 200
    assert "LATENCIA PUSH" not in r.text
    assert "ACEPTACIÓN PUSH" in r.text
    assert "push-sla-pct" not in r.text


def test_el_middleware_separa_por_entorno(config_aislada, monkeypatch):
    """
    Agrupaba test y prod bajo la misma clave. v1.9.9 (B-20): se EJECUTA el
    middleware real (main.app) con peticiones aceptadas a los dos entornos.
    """
    import asyncio
    from fastapi.testclient import TestClient
    import main
    from app.api.routers import dashboard, schmitz
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    for entorno in ("prod", "test"):
        db.add(ProviderConfig(provider_name="schmitz", env=entorno, provider_type="push", is_active=True,
                              use_mock=True, webhook_auth_secret_enc=encrypt("CLAVE")))
    db.commit()
    db.close()
    schmitz.invalidar_cache_auth()
    monkeypatch.setattr(schmitz, "_webhook_queue", asyncio.Queue(maxsize=10))
    dashboard.reset_push_stats()
    c = TestClient(main.app)
    cuerpo = {"ChassisNumber": "R5868BDP", "DeviceTime": "2026-10-04T22:23:36Z"}
    for entorno in ("prod", "test", "test"):
        assert c.post(f"/Json/Data?env={entorno}", json=cuerpo, headers={"x-api-key": "CLAVE"}).status_code == 202
    assert {k: v["count"] for k, v in dashboard.push_latency_store.items()} == {"schmitz:prod": 1, "schmitz:test": 2}
    dashboard.reset_push_stats()
    schmitz.invalidar_cache_auth()


def test_las_estadisticas_toleran_ambas_formas_de_clave():
    """Los llamadores existentes consultan por proveedor, sin entorno."""
    from app.api.routers.dashboard import (
        push_latency_store, get_push_stats, record_push_latency,
    )

    push_latency_store.clear()
    record_push_latency("schmitz:test", 0.001)
    record_push_latency("schmitz:prod", 0.002)

    assert get_push_stats("schmitz:test")["count"] == 1
    assert get_push_stats("schmitz")["count"] == 2      # agrega los entornos
    assert get_push_stats("all")["count"] == 2
    push_latency_store.clear()
