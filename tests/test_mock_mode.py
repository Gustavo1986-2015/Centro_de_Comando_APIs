"""
Tests de la protección del modo simulado (use_mock).

Con el modo simulado activo el sistema NO llama a Recurso Confiable: genera
job_ids falsos y marca los eventos como enviados. Si eso ocurre sin que nadie
lo advierta, una alarma real (robo, pánico) queda registrada como despachada
sin haber salido nunca del hub.

Por eso activarlo exige revalidar la contraseña de administrador, y el estado
se expone al dashboard para mostrar una advertencia permanente.
"""
import os
import base64
import pytest
from fastapi.testclient import TestClient


PASSWORD = "clave_admin_de_prueba"


@pytest.fixture
def client(monkeypatch, config_aislada):
    """
    Cliente sin lifespan: estos tests solo consultan endpoints de configuración
    y estadísticas, no necesitan los workers de fondo.

    Base de configuración propia (config_aislada): estos tests alternan el modo
    simulado de la primera integración y la dejan en REAL. Contra una base
    compartida eso fue lo que cambió schmitz/prod el 05/10/2026 (v1.9.5).

    Levantar el lifespan acá dejaba las colas asyncio de los routers ligadas al
    event loop del test, y el siguiente test que abriera la app con otro loop
    disparaba "Queue is bound to a different event loop" al cerrarse.
    """
    monkeypatch.setenv("DASHBOARD_USER", "test")
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    from main import app
    return TestClient(app)


@pytest.fixture
def auth():
    token = base64.b64encode(f"test:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _payload(base, use_mock, password=None):
    u = {
        "id": base["id"],
        "is_active": base["is_active"],
        "use_mock": use_mock,
        "rc_user": base["rc_user"] or "",
        "rc_password": "",
        "purge_interval_min": 15,
        "run_interval_sec": 5,
        "queue_backend": "sqlite",
        "enable_state_dedup": True,
    }
    if password is not None:
        u["admin_password"] = password
    return [u]


def _primer_proveedor(client, auth):
    cfgs = client.get("/api/config", headers=auth).json()
    if not cfgs:
        pytest.skip("No hay proveedores configurados en el entorno de test")
    return cfgs[0]


def _estado_mock(client, auth, id_):
    for c in client.get("/api/config", headers=auth).json():
        if c["id"] == id_:
            return c["use_mock"]
    return None


# ── Activación protegida ─────────────────────────────────────────────────────

def test_activar_mock_sin_password_es_rechazado(client, auth):
    """
    REGRESIÓN: sin esta validación, cualquiera con acceso al panel podía dejar
    una integración de producción simulando envíos sin dejar rastro.
    """
    base = _primer_proveedor(client, auth)
    client.post("/api/config", headers=auth, json=_payload(base, False))   # partir de REAL

    r = client.post("/api/config", headers=auth, json=_payload(base, True))

    assert r.status_code == 403
    assert "contraseña de administrador" in r.json()["detail"]
    assert _estado_mock(client, auth, base["id"]) is False   # no cambió


def test_activar_mock_con_password_incorrecta_es_rechazado(client, auth):
    base = _primer_proveedor(client, auth)
    client.post("/api/config", headers=auth, json=_payload(base, False))

    r = client.post("/api/config", headers=auth, json=_payload(base, True, "incorrecta"))

    assert r.status_code == 403
    assert _estado_mock(client, auth, base["id"]) is False


def test_activar_mock_con_password_correcta_funciona(client, auth):
    base = _primer_proveedor(client, auth)
    client.post("/api/config", headers=auth, json=_payload(base, False))

    r = client.post("/api/config", headers=auth, json=_payload(base, True, PASSWORD))

    assert r.status_code == 200
    assert _estado_mock(client, auth, base["id"]) is True

    client.post("/api/config", headers=auth, json=_payload(base, False))   # limpiar


def test_desactivar_mock_no_requiere_password(client, auth):
    """Volver al modo real es la operación segura: no debe tener fricción."""
    base = _primer_proveedor(client, auth)
    client.post("/api/config", headers=auth, json=_payload(base, True, PASSWORD))

    r = client.post("/api/config", headers=auth, json=_payload(base, False))

    assert r.status_code == 200
    assert _estado_mock(client, auth, base["id"]) is False


def test_guardar_sin_tocar_mock_no_requiere_password(client, auth):
    """Cambiar otros campos con el mock ya activo no debe pedir contraseña."""
    base = _primer_proveedor(client, auth)
    client.post("/api/config", headers=auth, json=_payload(base, True, PASSWORD))

    r = client.post("/api/config", headers=auth, json=_payload(base, True))

    assert r.status_code == 200

    client.post("/api/config", headers=auth, json=_payload(base, False))   # limpiar


# ── Exposición del estado al dashboard ───────────────────────────────────────

def test_stats_expone_los_proveedores_en_modo_simulado(client, auth):
    """El frontend necesita este dato para mostrar la advertencia permanente."""
    base = _primer_proveedor(client, auth)
    client.post("/api/config", headers=auth, json=_payload(base, True, PASSWORD))

    d = client.get("/api/stats", headers=auth).json()

    assert "mock_providers" in d
    assert isinstance(d["mock_providers"], list)
    esperado = f"{base['provider_name'].lower()}/{base['env'].lower()}"
    assert esperado in [m.lower() for m in d["mock_providers"]]

    client.post("/api/config", headers=auth, json=_payload(base, False))   # limpiar


def test_stats_no_lista_proveedores_en_modo_real(client, auth):
    base = _primer_proveedor(client, auth)
    client.post("/api/config", headers=auth, json=_payload(base, False))

    d = client.get("/api/stats", headers=auth).json()

    esperado = f"{base['provider_name'].lower()}/{base['env'].lower()}"
    assert esperado not in [m.lower() for m in d["mock_providers"]]


# ── Versión y cache busting ──────────────────────────────────────────────────

def test_health_reporta_la_version_centralizada(client):
    """La versión estaba duplicada y quedó desactualizada (reportaba 1.2.0)."""
    from app.version import __version__
    assert client.get("/health").json()["version"] == __version__


def test_estaticos_usan_cache_busting_por_contenido(client, auth):
    """
    El `?v=2` fijo no cambiaba al desplegar: el navegador servía el JS anterior
    y los cambios no se veían hasta forzar una recarga.
    """
    html = client.get("/dashboard", headers=auth).text
    assert "dashboard.css?v=2\"" not in html
    assert "dashboard.js?v=2\"" not in html

    import re
    versiones = re.findall(r"dashboard\.(?:css|js)\?v=([a-f0-9]{8})", html)
    assert len(versiones) == 2
    assert versiones[0] != versiones[1]   # cada archivo tiene su propio hash


def test_static_version_es_estable_entre_llamadas():
    from app.version import static_version
    assert static_version("dashboard.js") == static_version("dashboard.js")


def test_static_version_con_archivo_inexistente():
    """No debe romper el render del dashboard si falta un estático."""
    from app.version import static_version
    assert static_version("no-existe-este-archivo.js")


# ─── v1.9.8 (auditoría B-6): el worker respeta el modo simulado, EJECUTADO ──
#
# En la suite RC_USE_MOCK=True para toda la sesión, así que "el worker ignora
# el modo simulado de la integración" pasaba inadvertido: el cliente simulaba
# igual por la variable global. Acá la variable se apaga y se instrumenta la
# llamada SOAP real (_send_batch_sync): con la integración en simulado, el
# worker no puede llegar a ella.

def _un_ciclo_del_worker(monkeypatch, use_mock: bool):
    import asyncio
    from app.api.routers import schmitz
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent
    from app.services import rc_soap
    from app.worker import processor

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          use_mock=use_mock, rc_user="AC_avl_SchmitzCargoBull", rc_password_enc=encrypt("x")))
    db.commit()
    db.close()
    schmitz._persist_batch([({"ChassisNumber": "R5868BDP", "DeviceTime": "2026-10-04T22:23:36Z",
                              "StatusData": [{"Position": {"Latitude": 42.44, "Longitude": -3.49}}]},
                             "prod", "b6")])
    llamadas_soap = []

    def _soap_real(self, eventos):
        llamadas_soap.append(len(eventos))
        raise ConnectionError("RC no disponible en el test")

    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", _soap_real)
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    asyncio.run(processor.process_provider_events("schmitz", "prod"))
    db = get_session("schmitz", "prod")
    try:
        estados = [e.status for e in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()
    return llamadas_soap, estados


def test_el_worker_no_llama_a_rc_si_la_integracion_esta_en_simulado(config_aislada, monkeypatch):
    llamadas, estados = _un_ciclo_del_worker(monkeypatch, use_mock=True)
    assert llamadas == [], "con la integración en simulado el worker llamó a RC"
    assert estados == ["simulado"]


def test_control_en_modo_real_el_worker_si_llama_a_rc(config_aislada, monkeypatch):
    """El control del anterior: confirma que la instrumentación ve la llamada real."""
    llamadas, estados = _un_ciclo_del_worker(monkeypatch, use_mock=False)
    assert llamadas == [1]
    assert estados == ["pending"], "un fallo de transporte se reintenta"
