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
    os.environ.pop("WEBHOOK_RATE_LIMIT_PER_MIN", None)
    yield
    rl.reset()
    os.environ.pop("WEBHOOK_RATE_LIMIT_PER_MIN", None)


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


def test_limite_default_es_generoso():
    """
    Schmitz envía ~80 ev/min en el peor caso documentado (40 cada 30s).
    El default debe dejar margen amplio para no rechazar tráfico legítimo.
    """
    os.environ.pop("WEBHOOK_RATE_LIMIT_PER_MIN", None)
    assert rl._limit() >= 600


def test_limite_invalido_cae_al_default():
    os.environ["WEBHOOK_RATE_LIMIT_PER_MIN"] = "no-es-un-numero"
    assert rl._limit() == 600


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
