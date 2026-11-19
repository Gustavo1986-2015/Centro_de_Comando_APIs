"""
v1.9.12: CSRF (A), encabezados de seguridad (B), documentación de la API
protegida (C) e ingresos fallidos (D). Contra la app real (main.app).
"""
import asyncio
import logging
import os
import threading
import time
from pathlib import Path

import pytest

USUARIO = os.environ["DASHBOARD_USER"]
CLAVE = os.environ["DASHBOARD_PASSWORD"]
AUTH = (USUARIO, CLAVE)
ENTRE_SITIOS = {"Sec-Fetch-Site": "cross-site"}


@pytest.fixture
def hub(config_aislada, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app
    from app.core import auth, dos_pasos
    monkeypatch.setattr(auth, "DEMORA_FALLO_SEG", 0)
    dos_pasos.invalidar()
    yield TestClient(app)
    dos_pasos.invalidar()


@pytest.fixture
def registros(caplog):
    """Captura en la raíz: importar main reconfigura el logging y saca el
    handler de caplog."""
    raiz = logging.getLogger()
    raiz.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG)
    yield caplog
    raiz.removeHandler(caplog.handler)


@pytest.fixture
def integraciones(config_aislada, monkeypatch):
    from app.api.routers import schmitz
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    esquema = {"base_mapping": {"chassis_number": "placa", "latitude": "lat", "longitude": "lon", "date": "fecha"}}
    db = get_session("system_config", "global")
    db.add_all([ProviderConfig(provider_name="conclave", env="prod", provider_type="push", is_active=True,
                               use_mock=True, webhook_auth_secret_enc=encrypt("CLAVE"), mapping_schema=esquema),
                ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                               use_mock=True, webhook_auth_secret_enc=encrypt("clave-schmitz"))])
    db.commit()
    db.close()
    schmitz.invalidar_cache_auth()
    monkeypatch.setattr(schmitz, "_webhook_queue", asyncio.Queue(maxsize=10))
    yield
    schmitz.invalidar_cache_auth()


CUERPO_DINAMICO = {"placa": "AB123CD", "lat": 1.0, "lon": 2.0, "fecha": "2026-10-10T12:00:00Z"}
CUERPO_SCHMITZ = {"ChassisNumber": "R5868BDP", "DeviceTime": "2026-10-04T22:23:36Z"}


# ═══════════════════════════════════════════════════════════════════════════
# A. CSRF
# ═══════════════════════════════════════════════════════════════════════════

# Los ataques verificados: una página ajena hace que el navegador del operador
# (que tiene la contraseña guardada) dispare estas acciones.
ATAQUES = [
    ("POST", "/api/maintenance/purge/schmitz/prod?ignorar_retencion=true"),
    ("POST", "/api/integrations/schmitz/prod/resync"),
    ("POST", "/api/config/refresh-token/protrack/prod"),
    ("POST", "/api/diagnostico/latencia/reiniciar"),
    ("POST", "/api/config/purge-logs"),
    ("DELETE", "/api/logs"),
    ("PUT", "/api/config/retention"),
    ("POST", "/api/seguridad/dos-pasos/cerrar-todas"),
    ("POST", "/auth/salir"),
]


@pytest.mark.parametrize("metodo,ruta", ATAQUES)
def test_csrf_entre_sitios_se_rechaza(hub, registros, metodo, ruta):
    r = hub.request(metodo, ruta, auth=AUTH, headers=ENTRE_SITIOS, json={})
    assert r.status_code == 403, (ruta, r.status_code, r.text[:200])
    assert "otro sitio" in r.json()["detail"]
    aviso = [x for x in registros.records if x.name == "app.core.seguridad_http"]
    assert aviso and aviso[0].levelno == logging.WARNING
    texto = aviso[0].getMessage()
    assert metodo in texto and ruta.split("?")[0] in texto and "cross-site" in texto


def test_csrf_el_rechazo_no_llega_a_ejecutar_la_accion(hub, monkeypatch):
    from app.core import latencia
    llamadas = []
    monkeypatch.setattr(latencia, "limpiar", lambda *a, **k: llamadas.append(1))
    hub.post("/api/diagnostico/latencia/reiniciar", auth=AUTH, headers=ENTRE_SITIOS)
    assert llamadas == []
    hub.post("/api/diagnostico/latencia/reiniciar", auth=AUTH, headers={"Sec-Fetch-Site": "same-origin"})
    assert llamadas == [1], "el control de la prueba: desde el mismo sitio sí se ejecuta"


@pytest.mark.parametrize("valor", ["same-origin", "same-site", "none", None])
def test_csrf_mismo_sitio_o_sin_encabezado_pasa(hub, valor):
    encabezados = {"Sec-Fetch-Site": valor} if valor else {}
    r = hub.post("/api/diagnostico/latencia/reiniciar", auth=AUTH, headers=encabezados)
    assert r.status_code == 200, (valor, r.status_code, r.text[:200])


def test_csrf_las_lecturas_no_se_tocan(hub):
    assert hub.get("/api/seguridad/sesion", auth=AUTH, headers=ENTRE_SITIOS).status_code == 200


def test_csrf_no_afecta_la_ingesta(hub, integraciones):
    r = hub.post("/webhook/dynamic/conclave?env=prod", json=CUERPO_DINAMICO,
                 headers={"x-api-key": "CLAVE", **ENTRE_SITIOS})
    assert r.status_code == 200, r.text
    for ruta in ("/Json/Data?env=prod", "/schmitz/webhook?env=prod"):
        r = hub.post(ruta, json=CUERPO_SCHMITZ, headers={"x-api-key": "clave-schmitz", **ENTRE_SITIOS})
        assert r.status_code == 202, (ruta, r.text)
    r = hub.post("/inspector/catch/s1", json={"a": 1}, auth=AUTH, headers=ENTRE_SITIOS)
    assert r.status_code == 200
    assert hub.get("/health", headers=ENTRE_SITIOS).status_code == 200


def test_csrf_no_decide_por_origin_ni_host(hub):
    """Origin y Host ajenos, sin Sec-Fetch-Site: pasa (no se usan para decidir)."""
    r = hub.post("/api/diagnostico/latencia/reiniciar", auth=AUTH,
                 headers={"Origin": "https://otro.example", "Host": "otro.example"})
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# B. Encabezados de seguridad
# ═══════════════════════════════════════════════════════════════════════════

FIJOS = {"x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "no-referrer"}


def _fijos_presentes(r):
    for k, v in FIJOS.items():
        assert r.headers.get(k) == v, (k, r.headers.get(k))


def test_encabezados_en_el_panel_en_la_api_y_en_el_webhook(hub, integraciones):
    respuestas = [
        hub.get("/dashboard", auth=AUTH),
        hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH),
        hub.post("/webhook/dynamic/conclave?env=prod", json=CUERPO_DINAMICO, headers={"x-api-key": "CLAVE"}),
        hub.post("/Json/Data?env=prod", json=CUERPO_SCHMITZ, headers={"x-api-key": "clave-schmitz"}),
        hub.get("/health"),
        hub.get("/static/dashboard.css"),
        hub.get("/api/seguridad/sesion"),                                   # 401
        hub.post("/api/diagnostico/latencia/reiniciar", auth=AUTH, headers=ENTRE_SITIOS),  # 403 CSRF
    ]
    assert [r.status_code for r in respuestas] == [200, 200, 200, 202, 200, 200, 401, 403]
    assert "text/html" in respuestas[0].headers["content-type"]
    assert "application/json" in respuestas[1].headers["content-type"]
    for r in respuestas:
        _fijos_presentes(r)
        assert "strict-transport-security" not in r.headers, "por HTTP plano no va HSTS"
    assert "content-security-policy" not in respuestas[0].headers, "CSP queda pendiente"


def test_hsts_solo_por_https(config_aislada):
    from fastapi.testclient import TestClient
    from main import app
    https = TestClient(app, base_url="https://testserver")
    for r in (https.get("/health"), https.get("/dashboard", auth=AUTH),
              TestClient(app).get("/health", headers={"X-Forwarded-Proto": "https"}),
              TestClient(app).get("/health", headers={"X-Forwarded-Proto": "https, http"})):
        assert r.headers["strict-transport-security"] == "max-age=31536000"
        _fijos_presentes(r)
    r = TestClient(app).get("/health", headers={"X-Forwarded-Proto": "http"})
    assert "strict-transport-security" not in r.headers


def test_el_stream_sse_conserva_sus_encabezados():
    """El middleware es ASGI puro: agrega encabezados sin tocar el cuerpo."""
    from app.core.seguridad_http import EncabezadosDeSeguridad
    enviados = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream"), (b"x-frame-options", b"SAMEORIGIN")]})
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    async def enviar(m):
        enviados.append(m)

    asyncio.run(EncabezadosDeSeguridad(app)({"type": "http", "scheme": "http", "headers": []}, None, enviar))
    encabezados = dict(enviados[0]["headers"])
    assert encabezados[b"content-type"] == b"text/event-stream"
    assert encabezados[b"x-frame-options"] == b"SAMEORIGIN", "no pisa uno que ya puso la ruta"
    assert encabezados[b"x-content-type-options"] == b"nosniff"
    assert enviados[1]["body"] == b"data: 1\n\n" and enviados[1]["more_body"] is True


# ═══════════════════════════════════════════════════════════════════════════
# C. Documentación de la API
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("ruta", ["/docs", "/redoc", "/openapi.json"])
def test_la_documentacion_pide_credenciales(hub, ruta):
    assert hub.get(ruta).status_code == 401
    assert hub.get(ruta, auth=(USUARIO, "mala")).status_code == 401
    r = hub.get(ruta, auth=AUTH)
    assert r.status_code == 200
    if ruta == "/openapi.json":
        rutas = r.json()["paths"]
        assert "/api/maintenance/purge/{provider}/{env}" in rutas and "/docs" not in rutas
    else:
        assert "/openapi.json" in r.text


def test_la_documentacion_no_queda_en_las_rutas_por_defecto():
    from main import app
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


# ═══════════════════════════════════════════════════════════════════════════
# D. Ingresos fallidos
# ═══════════════════════════════════════════════════════════════════════════

def test_credenciales_incorrectas_quedan_en_el_log_sin_la_contrasena(hub, registros):
    largo = "u" * 50
    r = hub.get("/api/seguridad/sesion", auth=(largo, "LaClaveQueNoVa-77"),
                headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Basic"
    avisos = [x for x in registros.records if x.name == "app.core.auth" and x.levelno == logging.WARNING]
    assert len(avisos) == 1
    texto = avisos[0].getMessage()
    assert "LaClaveQueNoVa-77" not in texto and "LaContrase" not in repr(avisos[0].args)
    assert "/api/seguridad/sesion" in texto and "testclient" in texto and "203.0.113.7" in texto
    assert "u" * 32 in texto and "u" * 33 not in texto, "usuario truncado a 32"
    assert "fallos_ultimos_10_min=" in texto


def test_el_contador_de_fallos_crece(hub, registros):
    for _ in range(3):
        hub.get("/api/seguridad/sesion", auth=(USUARIO, "mala"))
    cuentas = [int(x.getMessage().rsplit("=", 1)[1]) for x in registros.records if x.name == "app.core.auth"]
    assert len(cuentas) == 3 and cuentas[1] == cuentas[0] + 1 and cuentas[2] == cuentas[1] + 1


def test_el_fallo_demora_un_segundo_fuera_del_bucle_de_eventos(config_aislada, monkeypatch):
    """La dependencia es `def`: FastAPI la corre en el threadpool, así que el
    time.sleep no frena el bucle de eventos (ni la ingesta)."""
    from fastapi.testclient import TestClient
    from main import app
    from app.core import auth
    assert auth.DEMORA_FALLO_SEG == 1.0
    vistos = []
    dormir = time.sleep

    def espiar(seg):
        try:
            asyncio.get_running_loop()
            en_bucle = True
        except RuntimeError:
            en_bucle = False
        vistos.append((seg, en_bucle, threading.current_thread().name))
        dormir(seg)

    monkeypatch.setattr(auth.time, "sleep", espiar)
    c = TestClient(app)
    inicio = time.monotonic()
    assert c.get("/api/seguridad/sesion", auth=(USUARIO, "mala")).status_code == 401
    assert time.monotonic() - inicio >= 1.0
    assert vistos and vistos[0][0] == 1.0 and vistos[0][1] is False, vistos


def test_credenciales_correctas_no_demoran(hub, monkeypatch):
    from app.core import auth
    llamadas = []
    monkeypatch.setattr(auth.time, "sleep", lambda s: llamadas.append(s))
    inicio = time.monotonic()
    for _ in range(5):
        assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200
    assert llamadas == [] and time.monotonic() - inicio < 2.0


def test_sin_credenciales_no_cuenta_como_fallo(hub, registros):
    """El navegador pide primero sin credenciales y recién después muestra el
    cuadro: eso no es un intento fallido."""
    assert hub.get("/api/seguridad/sesion").status_code == 401
    assert not [x for x in registros.records if x.name == "app.core.auth"]


def test_no_se_bloquea_por_ip(hub):
    """Detrás del proxy todas las peticiones vienen de la misma IP: muchos
    fallos no pueden dejar afuera al que tiene la contraseña."""
    for _ in range(20):
        hub.get("/api/seguridad/sesion", auth=(USUARIO, "mala"))
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# G. Versiones fijas
# ═══════════════════════════════════════════════════════════════════════════

RAIZ = Path(__file__).resolve().parents[2]


def _requisitos(nombre):
    salida = []
    for linea in (RAIZ / nombre).read_text(encoding="utf-8").splitlines():
        linea = linea.split("#", 1)[0].strip()
        if linea:
            salida.append(linea)
    return salida


def test_produccion_con_versiones_exactas():
    lineas = _requisitos("requirements.txt")
    assert lineas and all("==" in l and not any(c in l for c in "<>~!*") for l in lineas), lineas
    nombres = {l.split("==")[0].lower() for l in lineas}
    assert "segno" in nombres
    assert not nombres & {"pytest", "pytest-asyncio", "iniconfig", "pluggy", "packaging", "pygments"}
    assert {"opentelemetry-api", "annotated-doc"} <= nombres, "las pide fastapi==0.162.0"


def test_desarrollo_incluye_produccion_y_solo_el_testing():
    lineas = _requisitos("requirements-dev.txt")
    assert lineas[0] == "-r requirements.txt"
    assert sorted(lineas[1:]) == sorted(["pytest==9.2.0", "pytest-asyncio==1.4.1.post1", "iniconfig==2.3.1",
                                         "pluggy==1.7.0", "packaging==26.3", "Pygments==2.21.0"])
