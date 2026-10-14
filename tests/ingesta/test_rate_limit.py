"""
Tests del rate limiting de webhooks (app/core/rate_limit.py).

Protege los endpoints de ingesta ante ráfagas descontroladas: un proveedor con
bug de reenvío, un reintento agresivo tras una caída, o tráfico malicioso.

El límite es transversal — aplica igual a cualquier proveedor, actual o futuro,
sin lógica específica por AVL.
"""
import os
import time
import pytest

from app.core import rate_limit as rl


@pytest.fixture(autouse=True)
def _reset():
    rl.reset()
    for k in [k for k in os.environ if k.startswith("WEBHOOK_RATE_LIMIT")]:
        os.environ.pop(k, None)
    yield
    rl.reset()
    for k in [k for k in os.environ if k.startswith("WEBHOOK_RATE_LIMIT")]:
        os.environ.pop(k, None)


# ── Comportamiento básico ────────────────────────────────────────────────────

def test_permite_dentro_del_limite():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "5"
    for i in range(5):
        allowed, remaining, _ = rl.check_rate_limit("protrack", "prod")
        assert allowed is True
        assert remaining == 4 - i


def test_rechaza_al_superar_el_limite():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "3"
    for _ in range(3):
        assert rl.check_rate_limit("protrack", "prod")[0] is True

    allowed, remaining, retry_after = rl.check_rate_limit("protrack", "prod")
    assert allowed is False
    assert remaining == 0
    assert retry_after > 0


def test_retry_after_es_razonable():
    """No debe pedir esperar más que la ventana completa."""
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "1"
    rl.check_rate_limit("protrack", "prod")
    _, _, retry_after = rl.check_rate_limit("protrack", "prod")
    assert 1 <= retry_after <= rl.WINDOW_SECONDS + 1


# ── Aislamiento entre integraciones ──────────────────────────────────────────

def test_limites_independientes_por_proveedor():
    """Una ráfaga de un proveedor no debe bloquear a otro."""
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "2"
    rl.check_rate_limit("protrack", "prod")
    rl.check_rate_limit("protrack", "prod")
    assert rl.check_rate_limit("protrack", "prod")[0] is False

    # schmitz tiene su propio contador
    assert rl.check_rate_limit("schmitz", "prod")[0] is True


def test_limites_independientes_por_entorno():
    """prod y test se contabilizan por separado."""
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "2"
    rl.check_rate_limit("protrack", "prod")
    rl.check_rate_limit("protrack", "prod")
    assert rl.check_rate_limit("protrack", "prod")[0] is False
    assert rl.check_rate_limit("protrack", "test")[0] is True


def test_normaliza_mayusculas():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "2"
    rl.check_rate_limit("PROTRACK", "PROD")
    rl.check_rate_limit("protrack", "prod")
    assert rl.check_rate_limit("Protrack", "Prod")[0] is False


# ── Ventana deslizante ───────────────────────────────────────────────────────

def test_la_ventana_libera_cupo_con_el_tiempo():
    """Las peticiones viejas salen de la ventana y liberan cupo."""
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "2"
    rl.reset()

    # Sembrar hits ya vencidos
    key = "protrack|prod"
    from collections import deque
    viejo = time.time() - rl.WINDOW_SECONDS - 5
    rl._HITS[key] = deque([viejo, viejo])

    allowed, remaining, _ = rl.check_rate_limit("protrack", "prod")
    assert allowed is True
    assert remaining == 1


# ── Configuración ────────────────────────────────────────────────────────────

def test_limite_configurable_por_entorno():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "10"
    assert rl._limit() == 10


def test_limite_default_cubre_la_prueba_de_certificacion():
    """
    REGRESIÓN: el default estaba en 600/min y la prueba de certificación de
    Schmitz envía 80 eventos/segundo (4.800/min) durante 24 horas. Con aquel
    valor se habría rechazado el 87% del tráfico y la prueba habría fallado.

    Un límite por debajo del volumen real del proveedor no protege: rompe la
    integración.
    """
    os.environ.pop("WEBHOOK_RATE_LIMIT_PER_MIN", None)
    necesario_schmitz = 80 * 60          # 80 ev/s sostenidos
    assert rl._limit() >= necesario_schmitz * 2   # con margen para ráfagas


def test_limite_invalido_cae_al_default():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "no-es-un-numero"
    assert rl._limit() == rl._DEFAULT_LIMIT


def test_limite_por_proveedor_tiene_prioridad():
    """
    Permite subir el techo de un proveedor de alto volumen sin aflojar el
    límite del resto.
    """
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "12000"
    os.environ["WEBHOOK_RATE_LIMIT_SCHMITZ"] = "30000"
    try:
        assert rl._limit("schmitz") == 30000
        assert rl._limit("protrack") == 12000
        assert rl._limit() == 12000
    finally:
        os.environ.pop("WEBHOOK_RATE_LIMIT_SCHMITZ", None)


def test_limite_por_proveedor_invalido_cae_al_global():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "12000"
    os.environ["WEBHOOK_RATE_LIMIT_SCHMITZ"] = "invalido"
    try:
        assert rl._limit("schmitz") == 12000
    finally:
        os.environ.pop("WEBHOOK_RATE_LIMIT_SCHMITZ", None)


def test_limite_cero_o_negativo_se_normaliza():
    """Un límite de 0 bloquearía todo el tráfico: se fuerza mínimo 1."""
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "0"
    assert rl._limit() >= 1


# ── Uso para el panel ────────────────────────────────────────────────────────

def test_get_usage_sin_trafico():
    u = rl.get_usage("protrack", "prod")
    assert u["used"] == 0
    assert u["pct"] == 0


def test_get_usage_refleja_el_consumo():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "10"
    for _ in range(4):
        rl.check_rate_limit("protrack", "prod")

    u = rl.get_usage("protrack", "prod")
    assert u["used"] == 4
    assert u["limit"] == 10
    assert u["pct"] == 40.0


def test_get_usage_no_cuenta_peticiones_vencidas():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "10"
    from collections import deque
    rl._HITS["protrack|prod"] = deque([time.time() - rl.WINDOW_SECONDS - 5])
    assert rl.get_usage("protrack", "prod")["used"] == 0


def test_snapshot_de_salud_incluye_el_uso():
    """El panel debe poder mostrar cuán cerca del límite está cada integración."""
    from app.core import provider_health as ph
    ph.reset()
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "100"

    ph.set_mode("schmitz", "prod", "push")
    rl.check_rate_limit("schmitz", "prod")

    e = next(x for x in ph.get_health_snapshot() if x["provider"] == "schmitz")
    assert e["rate"]["used"] == 1
    assert e["rate"]["limit"] == 100
    ph.reset()
