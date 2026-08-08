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
def client(monkeypatch):
    """
    Cliente sin lifespan: estos tests solo consultan endpoints de configuración
    y estadísticas, no necesitan los workers de fondo.

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
