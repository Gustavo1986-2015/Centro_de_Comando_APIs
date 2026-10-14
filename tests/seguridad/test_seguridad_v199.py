# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque2.py (reorganización por tema: seguridad).
"""
v1.9.9 · Bloque 2 — Seguridad.

2.1 B-9   Guardián de destinos: falla cerrado y vale para todo destino saliente.
2.2 B-12  Tokens y claves enmascarados en log, salud, consola y errores.
2.3 B-16  Producción sin MASTER_ENC_KEY: arranca, pero no guarda credenciales en claro.
2.4       Credenciales del diccionario cifradas (migración, panel, YAML, ciclo real).
2.5 B-22  Purga de logs: compare_digest y el mismo mínimo que la retención.
2.6 B-29  La clave de Schmitz guardada desde el panel vale al instante.

Todos EJECUTAN el código.
"""
import asyncio
import hashlib
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

RAIZ = Path(__file__).resolve().parents[2]
TOKEN = "TOKEN-SECRETO-PROTRACK-0123456789"


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _cliente(*routers):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


@pytest.fixture
def dns(monkeypatch):
    """Resolución controlada: {host: [ips]}. Lo que no está, no resuelve."""
    tabla = {}

    def _getaddrinfo(host, *a, **k):
        if host not in tabla:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in tabla[host]]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return tabla


@pytest.fixture
def sin_internos(monkeypatch):
    from app.worker import pull_engine
    monkeypatch.setattr(pull_engine, "PERMITIR_DESTINOS_INTERNOS", False)
    return pull_engine


# ═══════════════════════════════════════════════════════════════════════════
# 2.1 B-9 — Guardián de destinos
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", ["http:///x", "ftp://api.protrack365.com/x", "no es una url", "http://[::1"])
def test_b9_lo_que_no_se_puede_analizar_se_bloquea(sin_internos, dns, url):
    with pytest.raises(ValueError):
        sin_internos._verificar_destino_permitido(url)


def test_b9_lo_que_no_resuelve_se_bloquea_aun_con_internos_permitidos(sin_internos, dns, monkeypatch):
    for permitir in (False, True):
        monkeypatch.setattr(sin_internos, "PERMITIR_DESTINOS_INTERNOS", permitir)
        with pytest.raises(ValueError, match="no resuelve"):
            sin_internos._verificar_destino_permitido("http://proveedor-nuevo.example/api")


def test_b9_se_miran_todas_las_direcciones_del_nombre(sin_internos, dns):
    dns["mixto.example"] = ["52.7.161.77", "10.0.0.5"]
    dns["publico.example"] = ["52.7.161.77", "52.45.202.151"]
    with pytest.raises(ValueError, match="interna"):
        sin_internos._verificar_destino_permitido("https://mixto.example/x")
    sin_internos._verificar_destino_permitido("https://publico.example/x")


def test_b9_metadatos_de_aws_bloqueados(sin_internos, dns):
    dns["169.254.169.254"] = ["169.254.169.254"]
    with pytest.raises(ValueError, match="interna"):
        sin_internos._verificar_destino_permitido("http://169.254.169.254/latest/meta-data/")


def _sin_red(monkeypatch):
    """Si alguien intenta salir a la red, el test falla."""
    def _prohibido(*a, **k):
        raise AssertionError("se intentó una conexión saliente que el guardián tenía que frenar")
    monkeypatch.setattr(httpx.AsyncClient, "get", _prohibido)
    monkeypatch.setattr(httpx.AsyncClient, "post", _prohibido)


def test_cableado_b9_pull_diccionario_token_oauth2_y_tive_pasan_por_el_guardian(sin_internos, dns, monkeypatch):
    """Cada destino saliente se frena ANTES de conectar si es interno."""
    from app.core import oauth2
    from app.providers.tive import resolutor
    _sin_red(monkeypatch)
    dns["interno.example"] = ["10.1.2.3"]
    base = "http://interno.example"
    # PULL y diccionario (los dos usan execute_fetch).
    with pytest.raises(ValueError, match="interna"):
        asyncio.run(sin_internos.execute_fetch({"url": base + "/api/track", "auth_type": "none"}))
    # Token de Protrack.
    with pytest.raises(ValueError, match="interna"):
        asyncio.run(sin_internos._get_protrack_token(base, "cuenta", "clave"))
    # Token OAuth2.
    with pytest.raises(ValueError, match="interna"):
        asyncio.run(oauth2.get_oauth2_token(base + "/token", "cliente", "secreto"))
    # Resolutor de Tive.
    cred = {"base_url": base + "/public/v3", "token_url": base + "/token", "client_id": "c",
            "client_secret": "s", "formato": "multipart"}
    with pytest.raises(ValueError, match="interna"):
        asyncio.run(resolutor._get(cred, "prod", "/Devices/X", None))


# ═══════════════════════════════════════════════════════════════════════════
# 2.2 B-12 — Secretos enmascarados
# ═══════════════════════════════════════════════════════════════════════════

def test_b12_formato_dos_primeros_y_dos_ultimos():
    from app.core.secretos import enmascarar, enmascarar_valor
    assert enmascarar_valor("A1B2C3D4E5c02") == "A1••••02"
    assert enmascarar_valor("corto") == "••••"
    assert enmascarar(f"GET http://x/api/track?imeis=1&access_token={TOKEN}") == \
        "GET http://x/api/track?imeis=1&access_token=TO••••89"
    assert TOKEN not in enmascarar(f'{{"client_secret": "{TOKEN}"}}')
    assert TOKEN not in enmascarar(f"Authorization: Bearer {TOKEN}")


def test_b12_un_error_con_la_url_completa_no_deja_el_token_en_log_salud_ni_consola(tmp_path):
    """
    Proceso aparte con el logging REAL del hub (setup_logging, archivo JSON y
    consola): una consulta de httpx con el token en la URL, un 502 con la URL
    completa (traceback incluido) y el reporte a la píldora de salud.
    """
    guion = tmp_path / "forzar_error.py"
    guion.write_text(textwrap.dedent(f'''
        import json, logging, os, sys
        sys.path.insert(0, {str(RAIZ)!r})
        os.environ["LOG_FILE_PATH"] = {str(tmp_path / "app.jsonl")!r}
        import httpx
        from app.core.logging_config import setup_logging
        from app.core import provider_health
        from app.worker.pull_engine import describir_error
        setup_logging()
        url = "http://api.protrack365.com/api/track?imeis=864035052734572&access_token={TOKEN}"
        logging.getLogger("httpx").info(f'HTTP Request: GET {{url}} "HTTP/1.1 502 Bad Gateway"')
        try:
            pedido = httpx.Request("GET", url)
            httpx.Response(502, request=pedido).raise_for_status()
        except httpx.HTTPStatusError as e:
            provider_health.report_fetch_error("protrack", "prod", describir_error(e))
            logging.getLogger("app.worker.pull_engine").error(
                f"[PROTRACK-prod] Error en Sondeo PULL: {{describir_error(e)}}", exc_info=True)
            logging.getLogger("app.worker.pull_engine").error(f"crudo sin describir_error: {{e}}", exc_info=True)
        # Cada handler del hub por separado (archivo y consola): uno solo sin
        # filtro dejaría pasar el secreto aunque el otro lo enmascare.
        import io
        por_handler = {{}}
        for h in logging.getLogger().handlers:
            destino = io.StringIO()
            viejo = h.setStream(destino)
            registro = logging.LogRecord("prueba", logging.ERROR, __file__, 1,
                                         "solo este handler: " + url, None, None)
            h.handle(registro)
            h.flush()
            h.setStream(viejo)
            por_handler[type(h).__name__] = destino.getvalue()
        print("HANDLERS=" + json.dumps(por_handler))
        print("SALUD=" + json.dumps(provider_health._entry("protrack", "prod")))
    '''), encoding="utf-8")
    p = subprocess.run([sys.executable, str(guion)], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(tmp_path), timeout=60)
    assert p.returncode == 0, p.stderr
    crudo = (tmp_path / "app.jsonl").read_text(encoding="utf-8")
    # El archivo es JSON por línea (el "•" va escapado): se lee decodificado.
    archivo = "\n".join(json.dumps(json.loads(l), ensure_ascii=False) for l in crudo.splitlines() if l.strip())
    salud = p.stdout.split("SALUD=")[1]
    por_handler = json.loads(p.stdout.split("HANDLERS=")[1].split("\nSALUD=")[0])
    assert set(por_handler) == {"StreamHandler", "TimedRotatingFileHandler"}, por_handler.keys()
    for nombre, salida in por_handler.items():
        assert "solo este handler" in salida and TOKEN not in salida, f"el handler {nombre} no enmascara"
    for lugar, texto in (("archivo de log", crudo), ("archivo de log", archivo), ("consola", p.stdout),
                         ("píldora de salud", salud)):
        assert TOKEN not in texto, f"el token quedó completo en {lugar}"
    assert "access_token=TO••••89" in archivo and "Traceback" in archivo, "el traceback tiene que seguir, enmascarado"
    assert "TO••••89" in json.loads(salud)["last_fetch_error"]
    # Consola del panel: lee el mismo archivo y lo pasa por el mismo enmascarado.
    from app.core.log_stream import _parse_line
    for linea in archivo.splitlines():
        assert TOKEN not in json.dumps(_parse_line(linea))


def test_b12_la_pildora_enmascara_aunque_le_llegue_el_error_crudo():
    """Defensa propia de la salud: un camino que reporte str(e) sin describir_error."""
    from app.core import provider_health
    provider_health.forget("crudo", "prod")
    try:
        crudo = f"Server error for url 'http://x/api/track?access_token={TOKEN}'"
        provider_health.report_fetch_error("crudo", "prod", crudo)
        provider_health.report_auth_error("crudo", "prod", crudo)
        provider_health.report_dict_error("crudo", "prod", crudo)
        e = provider_health._entry("crudo", "prod")
        for campo in ("last_fetch_error", "auth_last_error", "dict_last_error"):
            assert TOKEN not in e[campo] and "TO••••89" in e[campo], campo
    finally:
        provider_health.forget("crudo", "prod")


async def test_cableado_b12_el_sondeo_real_reporta_el_error_enmascarado(monkeypatch, caplog):
    from app.core import provider_health
    from app.worker import pull_engine

    async def falla(_cfg):
        pedido = httpx.Request("GET", f"http://api.protrack365.com/api/track?access_token={TOKEN}")
        httpx.Response(502, request=pedido).raise_for_status()

    async def cortar(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(pull_engine, "_leer_config_sondeo_sync",
                        lambda p, e: (True, {"url": "http://x/api/track"}, {}, 30, False, False))
    monkeypatch.setattr(pull_engine, "execute_fetch", falla)
    monkeypatch.setattr(pull_engine.asyncio, "sleep", cortar)
    provider_health.forget("sondeob12", "prod")
    try:
        with caplog.at_level(logging.ERROR, logger=pull_engine.logger.name):
            with pytest.raises(asyncio.CancelledError):
                await pull_engine.telemetry_poll_loop("sondeob12", "prod")
        error = provider_health._entry("sondeob12", "prod")["last_fetch_error"]
        assert TOKEN not in error and "TO••••89" in error
        # El mensaje del log ya sale enmascarado (describir_error). El traceback
        # lo enmascara el filtro de los handlers del hub, que el caplog de
        # pytest no tiene: eso lo cubre el test del proceso aparte.
        assert all(TOKEN not in r.getMessage() for r in caplog.records)
    finally:
        provider_health.forget("sondeob12", "prod")


# ═══════════════════════════════════════════════════════════════════════════
# 2.3 B-16 — Producción sin MASTER_ENC_KEY
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def produccion_sin_clave(config_aislada, monkeypatch):
    from app.core import crypto
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          use_mock=True, rc_user="AC_avl_SchmitzCargoBull", rc_password_enc="clave-vieja-en-claro"))
    db.commit()
    fila_id = db.query(ProviderConfig).first().id
    db.close()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("MASTER_ENC_KEY", raising=False)
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)
    yield fila_id
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)


def _fila(fila_id, **cambios):
    base = dict(id=fila_id, is_active=True, rc_user="AC_avl_SchmitzCargoBull", use_mock=True,
                purge_interval_min=5, run_interval_sec=5, queue_backend="sqlite")
    base.update(cambios)
    return base


def test_b16_no_guarda_una_credencial_nueva_sin_cifrar(produccion_sin_clave):
    from app.api.routers import admin_config
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    r = _cliente(admin_config.router).post(
        "/api/config", json=[_fila(produccion_sin_clave, rc_password="nueva-clave-rc", rc_user="OTRO")], auth=_auth())
    assert r.status_code == 400 and "MASTER_ENC_KEY" in r.json()["detail"] and "SIN CIFRAR" in r.json()["detail"]
    db = get_session("system_config", "global")
    conf = db.query(ProviderConfig).first()
    assert conf.rc_password_enc == "clave-vieja-en-claro", "se guardó la credencial"
    assert conf.rc_user == "AC_avl_SchmitzCargoBull", "se guardó el resto de la petición"
    db.close()


def test_b16_sin_credenciales_el_panel_guarda_igual(produccion_sin_clave):
    from app.api.routers import admin_config
    r = _cliente(admin_config.router).post("/api/config", json=[_fila(produccion_sin_clave, run_interval_sec=9)],
                                           auth=_auth())
    assert r.status_code == 200, r.text


def test_b16_lo_ya_guardado_se_sigue_leyendo(produccion_sin_clave):
    from app.core.crypto import decrypt
    assert decrypt("clave-vieja-en-claro") == "clave-vieja-en-claro"


def test_b16_aviso_fuerte_al_arrancar(produccion_sin_clave, caplog, monkeypatch):
    from app.core.auth import verificar_credenciales_al_arrancar
    with caplog.at_level(logging.CRITICAL, logger="app.core.auth"):
        verificar_credenciales_al_arrancar()   # no lanza: el hub arranca
    assert "MASTER_ENC_KEY NO ESTÁ DEFINIDA EN PRODUCCIÓN" in caplog.text


def test_b16_las_credenciales_del_diccionario_tampoco(produccion_sin_clave):
    from app.api.routers import admin_config
    r = _cliente(admin_config.router).post("/api/config/schmitz/prod/enrichment",
                                           json={"url": "http://x", "auth_pass": "clave"}, auth=_auth())
    assert r.json()["status"] == "error" and "MASTER_ENC_KEY" in r.json()["message"]


# ═══════════════════════════════════════════════════════════════════════════
# 2.5 B-22 — Purga de logs
# ═══════════════════════════════════════════════════════════════════════════

def _purga(c, **cambios):
    cuerpo = dict(category="crudos", days=1, confirm_text="PURGAR", admin_password=os.environ["DASHBOARD_PASSWORD"])
    cuerpo.update(cambios)
    return c.post("/api/config/purge-logs", json=cuerpo, auth=_auth())


def test_b22_compare_digest_y_minimo_alineado_con_la_retencion(config_aislada, monkeypatch):
    from app.api.routers import admin_config
    llamadas = []
    real = admin_config.secrets.compare_digest
    monkeypatch.setattr(admin_config.secrets, "compare_digest", lambda a, b: llamadas.append((a, b)) or real(a, b))
    c = _cliente(admin_config.router)
    assert _purga(c, admin_password="mala-de-purga").status_code == 401
    assert any(b"mala-de-purga" in (a, b) for a, b in llamadas), \
        "la contraseña de la purga no se comparó con compare_digest"
    assert _purga(c, days=1).status_code == 200, "1 día es válido para la retención, y ahora también para la purga"
    assert _purga(c, days=0).status_code == 400
    assert c.put("/api/config/retention", json={"audit_retention_days": 0, "processed_retention_days": 1},
                 auth=_auth()).status_code == 400
