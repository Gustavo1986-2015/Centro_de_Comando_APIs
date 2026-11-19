"""
v1.9.12 (E): verificación en dos pasos (TOTP, RFC 6238) del panel.

Se prueba contra la app real (main.app) sobre una base de configuración
aislada, con un reloj controlado: así los códigos de la aplicación se
calculan igual que en el teléfono y la anti-repetición y los vencimientos se
pueden recorrer sin esperar.
"""
import base64
import logging
import os
import re

import pytest

from app.core import dos_pasos

USUARIO = os.environ["DASHBOARD_USER"]
CLAVE = os.environ["DASHBOARD_PASSWORD"]
AUTH = (USUARIO, CLAVE)
T0 = 1_800_000_000.0           # instante fijo, alineado al paso de 30 s


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

class Reloj:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def avanzar(self, seg):
        self.t += seg


@pytest.fixture
def reloj(monkeypatch):
    r = Reloj(T0)
    monkeypatch.setattr(dos_pasos, "_reloj", r)
    return r


@pytest.fixture
def rapido(monkeypatch):
    """Sin la demora de 1 s por contraseña incorrecta y con PBKDF2 liviano. El
    PBKDF2 real (200.000 iteraciones) se prueba aparte."""
    from app.core import auth
    monkeypatch.setattr(auth, "DEMORA_FALLO_SEG", 0)
    monkeypatch.setattr(dos_pasos, "ITERACIONES_PBKDF2", 1000)


@pytest.fixture
def hub(config_aislada, reloj, rapido):
    from fastapi.testclient import TestClient
    from main import app
    dos_pasos.invalidar()
    yield TestClient(app)
    dos_pasos.invalidar()


def _codigo(clave, reloj, desplazamiento_pasos=0):
    return dos_pasos.totp(clave, reloj.t + desplazamiento_pasos * dos_pasos.PASO_SEG)


def _activar(hub, reloj):
    r = hub.post("/api/seguridad/dos-pasos/iniciar", json={"password": CLAVE}, auth=AUTH)
    assert r.status_code == 200, r.text
    secreto = r.json()["secreto"]
    clave = dos_pasos.clave_de_secreto(secreto)
    r = hub.post("/api/seguridad/dos-pasos/confirmar", json={"codigo": _codigo(clave, reloj)}, auth=AUTH)
    assert r.status_code == 200, r.text
    return secreto, clave, r.json()["codigos_recuperacion"], r


def _pide_verificacion(r):
    return r.status_code == 403 and r.json() == {"detail": "verificacion_requerida"}


# ═══════════════════════════════════════════════════════════════════════════
# E.1 Algoritmo
# ═══════════════════════════════════════════════════════════════════════════

# RFC 6238, apéndice B (SHA1): secreto ASCII "12345678901234567890", 8 dígitos.
VECTORES_RFC6238_SHA1 = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize("instante,esperado", VECTORES_RFC6238_SHA1)
def test_vectores_del_rfc_6238(instante, esperado):
    assert dos_pasos.totp(b"12345678901234567890", instante, digitos=8) == esperado


def test_vectores_del_rfc_4226():
    """RFC 4226, apéndice D: HOTP de 6 dígitos para los contadores 0 a 9."""
    esperados = ["755224", "287082", "359152", "969429", "338314",
                 "254676", "287922", "162583", "399871", "520489"]
    assert [dos_pasos.hotp(b"12345678901234567890", i) for i in range(10)] == esperados


def test_el_panel_usa_6_digitos_y_paso_de_30_segundos():
    assert dos_pasos.totp(b"12345678901234567890", 59) == "287082"
    assert dos_pasos.DIGITOS == 6 and dos_pasos.PASO_SEG == 30


def test_el_secreto_es_de_20_bytes_en_base32():
    secreto = dos_pasos.generar_secreto()
    assert re.fullmatch(r"[A-Z2-7]{32}", secreto)
    assert len(dos_pasos.clave_de_secreto(secreto)) == 20
    assert dos_pasos.generar_secreto() != secreto


def test_ventana_de_un_paso_y_no_mas():
    clave = b"12345678901234567890"
    for desfase, vale in [(-2, False), (-1, True), (0, True), (1, True), (2, False)]:
        codigo = dos_pasos.totp(clave, T0 + desfase * 30)
        assert (dos_pasos.paso_del_codigo(clave, codigo, T0, None) is not None) is vale, desfase


def test_anti_repeticion_por_ultimo_paso():
    clave = b"12345678901234567890"
    paso = dos_pasos.paso_de(T0)
    codigo = dos_pasos.totp(clave, T0)
    assert dos_pasos.paso_del_codigo(clave, codigo, T0, None) == paso
    assert dos_pasos.paso_del_codigo(clave, codigo, T0, paso) is None, "mismo paso: repetición"
    anterior = dos_pasos.totp(clave, T0 - 30)
    assert dos_pasos.paso_del_codigo(clave, anterior, T0, paso) is None, "paso anterior"
    siguiente = dos_pasos.totp(clave, T0 + 30)
    assert dos_pasos.paso_del_codigo(clave, siguiente, T0, paso) == paso + 1


def test_uri_de_alta():
    uri = dos_pasos.uri_de_alta("ABCDEFGHIJKLMNOP", "admin")
    assert uri == ("otpauth://totp/Hub%20Assistcargo:admin?secret=ABCDEFGHIJKLMNOP"
                   "&issuer=Hub%20Assistcargo&digits=6&period=30")


def test_qr_en_svg():
    svg = dos_pasos.qr_svg("otpauth://totp/x?secret=ABC")
    assert svg.lstrip().startswith("<svg") and "</svg>" in svg


# ═══════════════════════════════════════════════════════════════════════════
# E.3 Alta
# ═══════════════════════════════════════════════════════════════════════════

def test_apagada_el_panel_no_pide_nada(hub):
    """Por defecto: ni cookie ni 403, como en la v1.9.11."""
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200
    r = hub.get("/dashboard", auth=AUTH)
    assert r.status_code == 200 and 'id="view-dashboard"' in r.text
    assert 'data-dos-pasos="0"' in r.text
    assert "set-cookie" not in r.headers
    assert hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()["activo"] is False


def test_iniciar_pide_la_contrasena(hub):
    r = hub.post("/api/seguridad/dos-pasos/iniciar", json={"password": "otra"}, auth=AUTH)
    assert r.status_code == 403
    assert "www-authenticate" not in r.headers


def test_el_secreto_pendiente_no_protege_nada(hub):
    r = hub.post("/api/seguridad/dos-pasos/iniciar", json={"password": CLAVE}, auth=AUTH)
    assert r.status_code == 200
    datos = r.json()
    assert datos["qr_svg"].lstrip().startswith("<svg")
    assert datos["uri"].startswith("otpauth://totp/Hub%20Assistcargo:" + USUARIO + "?secret=" + datos["secreto"])
    hub.cookies.clear()
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200
    assert hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()["activo"] is False


def test_un_codigo_invalido_no_activa(hub, reloj):
    r = hub.post("/api/seguridad/dos-pasos/iniciar", json={"password": CLAVE}, auth=AUTH)
    clave = dos_pasos.clave_de_secreto(r.json()["secreto"])
    malo = _codigo(clave, reloj, 5)
    r = hub.post("/api/seguridad/dos-pasos/confirmar", json={"codigo": malo}, auth=AUTH)
    assert r.status_code == 400
    assert hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()["activo"] is False
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200


def test_el_alta_pendiente_vence(hub, reloj):
    r = hub.post("/api/seguridad/dos-pasos/iniciar", json={"password": CLAVE}, auth=AUTH)
    clave = dos_pasos.clave_de_secreto(r.json()["secreto"])
    reloj.avanzar(dos_pasos.PENDIENTE_VIGENCIA_SEG + 1)
    r = hub.post("/api/seguridad/dos-pasos/confirmar", json={"codigo": _codigo(clave, reloj)}, auth=AUTH)
    assert r.status_code == 409


def test_un_codigo_valido_activa_y_da_10_codigos_de_recuperacion(hub, reloj):
    _, _, codigos, r = _activar(hub, reloj)
    assert len(codigos) == 10 and len(set(codigos)) == 10
    for c in codigos:
        assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", c)
        assert not set(c) & set("01ILO"), "sin caracteres ambiguos"
    # El navegador que activa queda con su sesión.
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("hub_sesion=")
    for marca in ("HttpOnly", "Path=/", "SameSite=strict", "Max-Age=43200"):
        assert marca.lower() in cookie.lower(), marca
    assert "secure" not in cookie.lower(), "por HTTP plano no va Secure"
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200
    est = hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()
    assert est["activo"] is True and est["codigos_restantes"] == 10 and est["activado_en"]
    # Activa, no se puede volver a iniciar.
    assert hub.post("/api/seguridad/dos-pasos/iniciar", json={"password": CLAVE}, auth=AUTH).status_code == 409


def test_la_cookie_lleva_secure_por_https(config_aislada, reloj, rapido):
    from fastapi.testclient import TestClient
    from main import app
    dos_pasos.invalidar()
    try:
        for cliente, encabezados in ((TestClient(app, base_url="https://testserver"), {}),
                                     (TestClient(app), {"X-Forwarded-Proto": "https"})):
            if cliente.base_url.scheme == "https":
                _, _, _, r = _activar(cliente, reloj)
            else:
                # Ya activa: una sesión nueva por /auth/verificar con el proxy diciendo https.
                reloj.avanzar(60)
                clave = _clave_guardada()
                r = cliente.post("/auth/verificar", data={"codigo": _codigo(clave, reloj)},
                                 auth=AUTH, headers=encabezados, follow_redirects=False)
                assert r.status_code == 303
            assert "secure" in r.headers["set-cookie"].lower()
    finally:
        dos_pasos.invalidar()


def _clave_guardada():
    from app.core.crypto import decrypt
    from app.models.config_models import SeguridadDosPasos
    with dos_pasos._sesion() as s:
        return dos_pasos.clave_de_secreto(decrypt(s.get(SeguridadDosPasos, 1).secreto_enc))


# ═══════════════════════════════════════════════════════════════════════════
# E.4 Ingreso con la verificación activa
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_sesion_las_api_dan_403_sin_www_authenticate(hub, reloj):
    _activar(hub, reloj)
    hub.cookies.clear()
    for ruta in ("/api/seguridad/sesion", "/api/stats", "/api/config/providers",
                 "/api/db-viewer/databases", "/openapi.json", "/api/seguridad/dos-pasos/estado"):
        r = hub.get(ruta, auth=AUTH)
        assert _pide_verificacion(r), (ruta, r.status_code, r.text[:200])
        assert "www-authenticate" not in r.headers, ruta
    # La contraseña sigue yendo primero: sin ella, 401 como siempre.
    assert hub.get("/api/seguridad/sesion").status_code == 401
    assert hub.get("/api/seguridad/sesion", auth=(USUARIO, "mala")).status_code == 401


def test_sin_sesion_el_panel_muestra_la_pagina_de_verificacion(hub, reloj):
    _activar(hub, reloj)
    hub.cookies.clear()
    r = hub.get("/dashboard", auth=AUTH)
    assert r.status_code == 200
    assert 'action="/auth/verificar"' in r.text and "códigos de recuperación" in r.text
    assert 'id="view-dashboard"' not in r.text
    assert "no-store" in r.headers["cache-control"]


def test_verificar_abre_la_sesion_y_no_admite_repetir_el_codigo(hub, reloj):
    _, clave, _, _ = _activar(hub, reloj)
    hub.cookies.clear()
    reloj.avanzar(30)
    codigo = _codigo(clave, reloj)
    r = hub.post("/auth/verificar", data={"codigo": codigo}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    assert r.headers["set-cookie"].startswith("hub_sesion=")
    r = hub.get("/dashboard", auth=AUTH)
    assert 'id="view-dashboard"' in r.text and 'data-dos-pasos="1"' in r.text
    # El mismo código otra vez (otro navegador): rechazado.
    hub.cookies.clear()
    r = hub.post("/auth/verificar", data={"codigo": codigo}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 400 and 'action="/auth/verificar"' in r.text
    assert "set-cookie" not in r.headers


def test_verificar_por_json(hub, reloj):
    _, clave, _, _ = _activar(hub, reloj)
    hub.cookies.clear()
    reloj.avanzar(30)
    r = hub.post("/auth/verificar", json={"codigo": _codigo(clave, reloj)}, auth=AUTH)
    assert r.status_code == 200 and r.json()["redirigir"] == "/dashboard"
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200


def test_verificar_pide_la_contrasena(hub, reloj):
    _, clave, _, _ = _activar(hub, reloj)
    hub.cookies.clear()
    reloj.avanzar(30)
    assert hub.post("/auth/verificar", data={"codigo": _codigo(clave, reloj)}).status_code == 401


def _cookie_actual(hub):
    return hub.cookies.get("hub_sesion")


def test_cookie_adulterada_vencida_o_de_otra_generacion_no_vale(hub, reloj):
    _activar(hub, reloj)
    valor = _cookie_actual(hub)
    gen, venc, firma = valor.split(".")

    def con(cookie):
        hub.cookies.clear()
        hub.cookies.set("hub_sesion", cookie)
        return hub.get("/api/seguridad/sesion", auth=AUTH)

    assert con(valor).status_code == 200
    otra = "0" if firma[0] != "0" else "1"
    assert _pide_verificacion(con(f"{gen}.{venc}.{otra}{firma[1:]}")), "firma adulterada"
    assert _pide_verificacion(con(f"{gen}.{int(venc) + 3600}.{firma}")), "vencimiento estirado"
    assert _pide_verificacion(con(f"{int(gen) + 1}.{venc}.{firma}")), "generación cambiada"
    assert _pide_verificacion(con("basura")), "formato"
    assert _pide_verificacion(con("")), "vacía"
    # Vencida: pasa la duración configurada (12 h).
    assert con(valor).status_code == 200
    reloj.avanzar(12 * 3600 + 1)
    assert _pide_verificacion(con(valor)), "vencida"


def test_cerrar_todas_invalida_las_sesiones_existentes(hub, reloj):
    _, clave, _, _ = _activar(hub, reloj)
    primera = _cookie_actual(hub)
    # Segunda sesión (otro navegador).
    reloj.avanzar(30)
    hub.cookies.clear()
    hub.post("/auth/verificar", data={"codigo": _codigo(clave, reloj)}, auth=AUTH, follow_redirects=False)
    segunda = _cookie_actual(hub)
    assert segunda and segunda != primera
    r = hub.post("/api/seguridad/dos-pasos/cerrar-todas", auth=AUTH)
    assert r.status_code == 200
    for cookie in (primera, segunda):
        hub.cookies.clear()
        hub.cookies.set("hub_sesion", cookie)
        assert _pide_verificacion(hub.get("/api/seguridad/sesion", auth=AUTH))


def test_cerrar_esta_sesion_borra_la_cookie(hub, reloj):
    _activar(hub, reloj)
    r = hub.post("/auth/salir", auth=AUTH)
    assert r.status_code == 200
    assert 'hub_sesion=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()
    assert _pide_verificacion(hub.get("/api/seguridad/sesion", auth=AUTH))


def test_bloqueo_tras_5_fallos(hub, reloj, caplog):
    _, clave, _, _ = _activar(hub, reloj)
    hub.cookies.clear()
    reloj.avanzar(30)
    malo = _codigo(clave, reloj, 10)
    raiz = logging.getLogger()
    raiz.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            estados = [hub.post("/auth/verificar", data={"codigo": malo}, auth=AUTH,
                                follow_redirects=False).status_code for _ in range(5)]
    finally:
        raiz.removeHandler(caplog.handler)
    assert estados == [400, 400, 400, 400, 429]
    avisos = [r for r in caplog.records if r.name == "app.core.dos_pasos" and r.levelno == logging.WARNING]
    criticos = [r for r in caplog.records if r.name == "app.core.dos_pasos" and r.levelno == logging.CRITICAL]
    assert len(avisos) >= 4 and len(criticos) == 1
    # Bloqueada: ni el código correcto entra, y es global (por JSON también).
    bueno = _codigo(clave, reloj)
    assert hub.post("/auth/verificar", data={"codigo": bueno}, auth=AUTH, follow_redirects=False).status_code == 429
    assert hub.post("/auth/verificar", json={"codigo": bueno}, auth=AUTH).status_code == 429
    # A los 10 minutos, vuelve.
    reloj.avanzar(dos_pasos.BLOQUEO_SEG + 1)
    r = hub.post("/auth/verificar", data={"codigo": _codigo(clave, reloj)}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 303


def test_los_fallos_viejos_no_cuentan(hub, reloj):
    _, clave, _, _ = _activar(hub, reloj)
    hub.cookies.clear()
    for _ in range(4):
        reloj.avanzar(1)
        hub.post("/auth/verificar", data={"codigo": "000000"}, auth=AUTH, follow_redirects=False)
    reloj.avanzar(dos_pasos.VENTANA_FALLOS_SEG + 1)
    r = hub.post("/auth/verificar", data={"codigo": "000000"}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 400, "los 4 anteriores quedaron fuera de la ventana de 10 minutos"


def test_un_codigo_de_recuperacion_sirve_una_sola_vez(hub, reloj, caplog):
    _, _, codigos, _ = _activar(hub, reloj)
    hub.cookies.clear()
    raiz = logging.getLogger()
    raiz.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            r = hub.post("/auth/verificar", data={"codigo": codigos[3].lower()}, auth=AUTH,
                         follow_redirects=False)
    finally:
        raiz.removeHandler(caplog.handler)
    assert r.status_code == 303, "minúsculas también: se normaliza"
    assert any("se usó un código de recuperación, quedan 9" in x.getMessage() for x in caplog.records)
    assert hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()["codigos_restantes"] == 9
    hub.cookies.clear()
    r = hub.post("/auth/verificar", data={"codigo": codigos[3]}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 400
    r = hub.post("/auth/verificar", data={"codigo": codigos[4].replace("-", "")}, auth=AUTH,
                 follow_redirects=False)
    assert r.status_code == 303, "sin guion también"


def test_los_codigos_de_recuperacion_se_guardan_con_pbkdf2_de_200000(config_aislada, reloj, monkeypatch):
    """El alta real, sin aliviar el PBKDF2."""
    from fastapi.testclient import TestClient
    from main import app
    from app.core import auth
    from app.models.config_models import CodigoRecuperacion
    monkeypatch.setattr(auth, "DEMORA_FALLO_SEG", 0)
    assert dos_pasos.ITERACIONES_PBKDF2 == 200_000
    dos_pasos.invalidar()
    try:
        _, _, codigos, _ = _activar(TestClient(app), reloj)
        with dos_pasos._sesion() as s:
            filas = s.query(CodigoRecuperacion).all()
        assert len(filas) == 10
        assert {f.iteraciones for f in filas} == {200_000}
        assert len({f.sal for f in filas}) == 10, "sal propia por código"
        guardado = " ".join(f.hash_pbkdf2 + f.sal for f in filas)
        for c in codigos:
            assert c not in guardado and c.replace("-", "") not in guardado
    finally:
        dos_pasos.invalidar()


def test_el_visor_oculta_los_hash():
    from app.api.routers.db_viewer import _es_cifrada
    assert _es_cifrada("hash_pbkdf2") and _es_cifrada("secreto_enc") and _es_cifrada("clave_firma_enc")
    assert not _es_cifrada("sal") and not _es_cifrada("usado")


def test_con_la_verificacion_activa_la_ingesta_no_pide_cookie(hub, reloj, monkeypatch):
    import asyncio
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
    try:
        _activar(hub, reloj)
        hub.cookies.clear()
        assert hub.get("/health").status_code == 200
        r = hub.post("/webhook/dynamic/conclave?env=prod", headers={"x-api-key": "CLAVE"},
                     json={"placa": "AB123CD", "lat": 1.0, "lon": 2.0, "fecha": "2026-10-10T12:00:00Z"})
        assert r.status_code == 200, r.text
        cuerpo = {"ChassisNumber": "R5868BDP", "DeviceTime": "2026-10-04T22:23:36Z"}
        assert hub.post("/Json/Data?env=prod", json=cuerpo, headers={"x-api-key": "clave-schmitz"}).status_code == 202
        assert hub.post("/schmitz/webhook?env=prod", json=cuerpo,
                        headers={"x-api-key": "clave-schmitz"}).status_code == 202
        # Recepción del inspector: sí la contraseña (como siempre), no la cookie.
        assert hub.post("/inspector/catch/sesion-prueba", json={"a": 1}, auth=AUTH).status_code == 200
        # Lo capturado lo mira el panel: ese GET sí pide la sesión.
        assert _pide_verificacion(hub.get("/inspector/catch/sesion-prueba/latest", auth=AUTH))
        assert hub.get("/static/dashboard.js").status_code == 200
    finally:
        schmitz.invalidar_cache_auth()


def test_sin_master_enc_key_no_se_puede_activar(hub, monkeypatch):
    from app.core import crypto
    monkeypatch.delenv("MASTER_ENC_KEY", raising=False)
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)
    est = hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()
    assert est["puede_activar"] is False and "MASTER_ENC_KEY" in est["motivo_no_puede"]
    r = hub.post("/api/seguridad/dos-pasos/iniciar", json={"password": CLAVE}, auth=AUTH)
    assert r.status_code == 409 and "MASTER_ENC_KEY" in r.json()["detail"]
    assert hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()["activo"] is False


def test_desactivar_pide_contrasena_y_codigo(hub, reloj):
    _, clave, codigos, _ = _activar(hub, reloj)
    reloj.avanzar(30)
    url = "/api/seguridad/dos-pasos/desactivar"
    assert hub.post(url, json={"password": "mala", "codigo": _codigo(clave, reloj)}, auth=AUTH).status_code == 403
    assert hub.post(url, json={"password": CLAVE, "codigo": "000000"}, auth=AUTH).status_code == 400
    assert hub.get("/api/seguridad/dos-pasos/estado", auth=AUTH).json()["activo"] is True
    r = hub.post(url, json={"password": CLAVE, "codigo": codigos[0]}, auth=AUTH)
    assert r.status_code == 200, "con un código de recuperación también"
    hub.cookies.clear()
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200
    from app.models.config_models import CodigoRecuperacion, SeguridadDosPasos
    with dos_pasos._sesion() as s:
        fila = s.get(SeguridadDosPasos, 1)
        assert fila.secreto_enc is None and fila.clave_firma_enc is None
        assert s.query(CodigoRecuperacion).count() == 0


def test_nuevos_codigos_de_recuperacion_invalidan_los_anteriores(hub, reloj):
    _, clave, viejos, _ = _activar(hub, reloj)
    reloj.avanzar(30)
    url = "/api/seguridad/dos-pasos/recuperacion"
    assert hub.post(url, json={"codigo": viejos[0]}, auth=AUTH).status_code == 400, \
        "pide un código de la aplicación, no de recuperación"
    r = hub.post(url, json={"codigo": _codigo(clave, reloj)}, auth=AUTH)
    assert r.status_code == 200
    nuevos = r.json()["codigos_recuperacion"]
    assert len(nuevos) == 10 and not set(nuevos) & set(viejos)
    hub.cookies.clear()
    assert hub.post("/auth/verificar", data={"codigo": viejos[1]}, auth=AUTH,
                    follow_redirects=False).status_code == 400
    assert hub.post("/auth/verificar", data={"codigo": nuevos[1]}, auth=AUTH,
                    follow_redirects=False).status_code == 303


def test_duracion_de_la_sesion(hub, reloj):
    _, clave, _, _ = _activar(hub, reloj)
    url = "/api/seguridad/dos-pasos/duracion"
    assert hub.put(url, json={"horas": 0}, auth=AUTH).status_code == 400
    assert hub.put(url, json={"horas": 169}, auth=AUTH).status_code == 400
    assert hub.put(url, json={"horas": 24}, auth=AUTH).json() == {"duracion_horas": 24}
    hub.cookies.clear()
    reloj.avanzar(30)
    r = hub.post("/auth/verificar", data={"codigo": _codigo(clave, reloj)}, auth=AUTH, follow_redirects=False)
    assert "max-age=86400" in r.headers["set-cookie"].lower()
    reloj.avanzar(23 * 3600)
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200
    reloj.avanzar(3601)
    assert _pide_verificacion(hub.get("/api/seguridad/sesion", auth=AUTH))


def test_comando_de_emergencia(hub, reloj, caplog, monkeypatch):
    from app.core import logging_config
    from app.tools import desactivar_dos_pasos
    # En el contenedor configura el log como la app; acá lo reconfiguraría
    # encima del de la suite.
    monkeypatch.setattr(logging_config, "setup_logging", lambda: None)
    _activar(hub, reloj)
    hub.cookies.clear()
    assert _pide_verificacion(hub.get("/api/seguridad/sesion", auth=AUTH))
    raiz = logging.getLogger()
    raiz.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            assert desactivar_dos_pasos.main() == 0
    finally:
        raiz.removeHandler(caplog.handler)
    assert any(r.levelno == logging.CRITICAL and "EMERGENCIA" in r.getMessage() for r in caplog.records)
    assert hub.get("/api/seguridad/sesion", auth=AUTH).status_code == 200


def test_ningun_log_contiene_secretos_codigos_ni_la_cookie(hub, reloj, caplog):
    """Todo el recorrido, con el log en DEBUG: ni el secreto, ni los códigos
    (de la aplicación y de recuperación), ni la llave, ni la cookie, ni la
    contraseña."""
    raiz = logging.getLogger()
    raiz.addHandler(caplog.handler)
    usados = []
    try:
        with caplog.at_level(logging.DEBUG):
            secreto, clave, codigos, _ = _activar(hub, reloj)
            usados.append(_codigo(clave, reloj))
            cookies = [_cookie_actual(hub)]
            hub.cookies.clear()
            hub.post("/auth/verificar", data={"codigo": "999999"}, auth=AUTH, follow_redirects=False)
            hub.post("/auth/verificar", data={"codigo": "ZZZZ-ZZZZ"}, auth=AUTH, follow_redirects=False)
            reloj.avanzar(30)
            usados.append(_codigo(clave, reloj))
            hub.post("/auth/verificar", data={"codigo": usados[-1]}, auth=AUTH, follow_redirects=False)
            cookies.append(_cookie_actual(hub))
            hub.cookies.clear()
            hub.post("/auth/verificar", data={"codigo": codigos[0]}, auth=AUTH, follow_redirects=False)
            cookies.append(_cookie_actual(hub))
            hub.get("/api/seguridad/sesion", auth=(USUARIO, "contraseña-equivocada"))
            reloj.avanzar(30)
            usados.append(_codigo(clave, reloj))
            r = hub.post("/api/seguridad/dos-pasos/recuperacion", json={"codigo": usados[-1]}, auth=AUTH)
            codigos += r.json()["codigos_recuperacion"]
            reloj.avanzar(30)
            usados.append(_codigo(clave, reloj))
            hub.post("/api/seguridad/dos-pasos/desactivar", json={"password": CLAVE, "codigo": usados[-1]}, auth=AUTH)
    finally:
        raiz.removeHandler(caplog.handler)
    from app.models.config_models import SeguridadDosPasos  # noqa: F401
    textos = [r.getMessage() + " " + repr(r.args) for r in caplog.records]
    assert textos, "el recorrido tiene que haber dejado registros"
    prohibidos = [secreto, CLAVE, "contraseña-equivocada", *usados, *codigos,
                  *[c.replace("-", "") for c in codigos], *[c for c in cookies if c]]
    for c in cookies:
        if c:
            prohibidos.append(c.split(".")[2])
    for texto in textos:
        for p in prohibidos:
            assert p not in texto, f"el log contiene un dato secreto: {texto[:160]}"


def test_estado_con_cache_que_se_invalida_al_cambiar(hub, reloj, monkeypatch):
    lecturas = []
    original = dos_pasos._leer_estado
    monkeypatch.setattr(dos_pasos, "_leer_estado", lambda: lecturas.append(1) or original())
    dos_pasos.invalidar()
    for _ in range(5):
        hub.get("/api/seguridad/sesion", auth=AUTH)
    assert len(lecturas) == 1, "dentro de los 5 s se lee una sola vez"
    _activar(hub, reloj)
    hub.cookies.clear()
    assert _pide_verificacion(hub.get("/api/seguridad/sesion", auth=AUTH)), \
        "el cambio se ve enseguida: la caché se invalidó"


def test_la_clave_de_firma_no_es_master_enc_key(hub, reloj):
    from app.core.crypto import decrypt
    from app.models.config_models import SeguridadDosPasos
    _activar(hub, reloj)
    with dos_pasos._sesion() as s:
        fila = s.get(SeguridadDosPasos, 1)
        assert fila.clave_firma_enc.startswith("gAAAAAB") and fila.secreto_enc.startswith("gAAAAAB")
        clave = base64.b64decode(decrypt(fila.clave_firma_enc))
    assert len(clave) == 32
    assert clave != os.environ["MASTER_ENC_KEY"].encode()
