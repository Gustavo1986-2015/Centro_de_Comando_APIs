"""
v1.9.6 · Punto 1 — Salud, latencia y aceptación PUSH del webhook genérico.

Medido antes de cambiar nada, con la app real y su middleware: una petición
de Tive aceptada y descartada entera dejaba la píldora en "Esperando datos",
no aparecía en el Diagnóstico de Latencia, y la Aceptación PUSH la contaba
bajo la clave "webhook:prod" junto con los rechazos por firma y las URL a
integraciones inexistentes.

Ahora, para cualquier integración de /webhook/dynamic/ (módulo dedicado o
Integration Studio):
  - una petición ACEPTADA cuenta como tráfico, se guarde o se descarte;
  - se mide por tramos y entra en la Aceptación PUSH con su propio nombre;
  - una rechazada por autenticación no cuenta para nada.
Schmitz registra exactamente lo mismo que antes.
"""
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import latencia, provider_health

RAIZ = Path(__file__).resolve().parent.parent
FIXTURE = RAIZ / "tests" / "fixtures" / "tive_crudos_2026-10.jsonl"
CRUDOS = {json.loads(l)["origen"]: json.loads(l)["payload"] for l in open(FIXTURE, encoding="utf-8")}
POSICION_TRACKER = "2026-10-01:1"    # posición de tracker: el módulo la descarta entera
SECRETO = "secreto-hmac-de-prueba"
CLAVE_STUDIO = "clave-studio-de-prueba"
CLAVE_SCHMITZ = "clave-schmitz-de-prueba"
ESQUEMA_STUDIO = {"base_mapping": {"chassis_number": "plate", "latitude": "lat",
                                   "longitude": "lon", "date": "ts"}}


@pytest.fixture
def hub(config_aislada, tmp_path, monkeypatch):
    """La app REAL (main.app, con su middleware), sin lifespan, con tres integraciones."""
    from app.api.routers import dashboard, schmitz
    from app.core import safety_net
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.providers.tive import estado

    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    monkeypatch.setattr(safety_net, "DIRECTORIO_BASE", str(tmp_path / "red"))
    db = get_session("system_config", "global")
    db.add_all([
        ProviderConfig(provider_name="tive", env="prod", provider_type="push", is_active=True, use_mock=True,
                       webhook_auth_secret_enc=encrypt(SECRETO), webhook_auth_header="x-tive-signature",
                       webhook_auth_config={"modo": "hmac", "preset": "tive"}, enable_state_dedup=False),
        ProviderConfig(provider_name="acme", env="prod", provider_type="push", is_active=True, use_mock=True,
                       webhook_auth_secret_enc=encrypt(CLAVE_STUDIO), webhook_auth_header="x-api-key",
                       mapping_schema=ESQUEMA_STUDIO, enable_state_dedup=False),
        ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True, use_mock=True,
                       webhook_auth_secret_enc=encrypt(CLAVE_SCHMITZ), enable_state_dedup=False),
    ])
    db.commit()
    db.close()
    provider_health.reset()
    latencia.limpiar()
    dashboard.reset_push_stats()
    schmitz.invalidar_cache_auth()
    import asyncio
    monkeypatch.setattr(schmitz, "_webhook_queue", asyncio.Queue(maxsize=10))
    import main
    # El watchdog del worker marca el modo de cada integración al arrancar.
    for nombre in ("tive", "acme", "schmitz"):
        provider_health.set_mode(nombre, "prod", "push")
    yield TestClient(main.app)
    estado.cerrar_todo()
    provider_health.reset()
    latencia.limpiar()
    dashboard.reset_push_stats()
    schmitz.invalidar_cache_auth()


def _firmar(cuerpo: bytes, secreto=SECRETO) -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    d = hmac.new(secreto.encode(), f"{ts}.".encode() + cuerpo, hashlib.sha256).digest()
    return {"content-type": "application/json", "x-tive-signature": f"t={ts},v1={base64.b64encode(d).decode()}"}


def _tive(cliente, payload=None, secreto=SECRETO):
    cuerpo = json.dumps(payload or CRUDOS[POSICION_TRACKER]).encode()
    return cliente.post("/webhook/dynamic/tive?env=prod", content=cuerpo, headers=_firmar(cuerpo, secreto))


def _studio(cliente, payload, clave=CLAVE_STUDIO):
    return cliente.post("/webhook/dynamic/acme?env=prod", json=payload, headers={"x-api-key": clave})


def _salud(proveedor):
    return next((h for h in provider_health.get_health_snapshot() if h["provider"] == proveedor), None)


def _aceptacion():
    from app.api.routers import dashboard
    return {k: v["count"] for k, v in dashboard.push_latency_store.items()}


# ─── Tive (módulo dedicado) ────────────────────────────────────────────────

def test_aceptada_y_descartada_entera_cuenta_como_trafico(hub):
    r = _tive(hub)
    assert r.status_code == 200 and r.json()["note"] == "sin eventos para enviar"
    salud = _salud("tive")
    assert (salud["status"], salud["detail"]) == ("ok", "Operativo")
    assert salud["peticiones"] == 1 and salud["request_age_sec"] is not None
    assert salud["event_age_sec"] is None, "no se guardó nada: no es un evento"
    assert _aceptacion() == {"tive:prod": 1}
    assert latencia.integraciones_medidas() == ["tive:prod"]


def test_los_tramos_del_webhook_generico_suman_el_total(hub):
    _tive(hub)
    d = latencia.desglose("tive", "prod")
    tramos = {t["tramo"]: t["promedio_ms"] for t in d["tramos"]}
    assert {"auth", "parseo", "rate_limit", "procesamiento", "total_handler"} <= set(tramos)
    assert "guardado" not in tramos, "un descarte no llega a guardar"
    parciales = sum(v for k, v in tramos.items() if k != "total_handler")
    assert abs(parciales - tramos["total_handler"]) < 0.5


def test_una_alerta_guardada_mide_el_guardado(hub):
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.query(ProviderConfig).filter_by(provider_name="tive").one().module_options = {"alertas_trackers": True}
    db.commit()
    db.close()
    from app.models.db_models import NormalizedRCEvent
    from app.database import get_engine
    NormalizedRCEvent.metadata.create_all(bind=get_engine("tive", "prod"))
    r = _tive(hub, CRUDOS["2026-10-05:104"])
    assert r.json()["events_count"] == 1
    tramos = {t["tramo"] for t in latencia.desglose("tive", "prod")["tramos"]}
    assert "guardado" in tramos
    salud = _salud("tive")
    assert salud["event_age_sec"] is not None and salud["peticiones"] == 1


def test_rechazada_por_firma_no_cuenta(hub):
    r = _tive(hub, secreto="otro-secreto")
    assert r.status_code == 401
    salud = _salud("tive")
    assert salud["status"] == "idle" and salud["peticiones"] == 0
    assert _aceptacion() == {}, "antes contaba como 'webhook:prod'"
    assert latencia.integraciones_medidas() == []


def test_una_integracion_inexistente_no_crea_nada(hub):
    r = hub.post("/webhook/dynamic/inventada?env=prod", json={})
    assert r.status_code == 404
    assert _aceptacion() == {} and latencia.integraciones_medidas() == []
    assert _salud("inventada") is None


# ─── Integration Studio ────────────────────────────────────────────────────

def test_studio_aceptada_sin_eventos_validos_cuenta(hub):
    r = _studio(hub, {"plate": "AB123CD"})    # sin coordenadas ni fecha: no sale nada
    assert r.status_code == 200 and r.json()["events_count"] == 0
    assert _salud("acme")["status"] == "ok"
    assert _aceptacion() == {"acme:prod": 1}
    assert latencia.integraciones_medidas() == ["acme:prod"]


def test_studio_con_clave_invalida_no_cuenta(hub):
    assert _studio(hub, {"plate": "AB123CD"}, clave="incorrecta").status_code == 401
    assert _salud("acme")["status"] == "idle"
    assert _aceptacion() == {} and latencia.integraciones_medidas() == []


# ─── Schmitz no cambia ─────────────────────────────────────────────────────

def test_schmitz_registra_lo_mismo_que_antes(hub):
    payload = {"ChassisNumber": "R1", "DeviceTime": "2026-10-06T10:00:00Z"}
    r = hub.post("/Json/Data?env=prod", json=payload,
                 headers={"x-api-key": CLAVE_SCHMITZ, "X-Data-Type": "Status"})
    assert r.status_code == 202
    salud = _salud("schmitz")
    # Modo push y recepción correcta, como siempre; las peticiones NO cuentan.
    assert salud["mode"] == "push" and salud["fetch_age_sec"] is not None
    assert salud["peticiones"] == 0 and salud["request_age_sec"] is None
    assert salud["status"] == "idle", "la píldora de Schmitz sigue dependiendo de eventos guardados"
    tramos = {t["tramo"] for t in latencia.desglose("schmitz", "prod")["tramos"]}
    assert tramos == {"auth", "parseo", "rate_limit", "encolado", "total_handler"}
    # La Aceptación PUSH de Schmitz cuenta toda petición, también un rechazo:
    # es lo de antes y no se cambia.
    hub.post("/Json/Data?env=prod", json=payload, headers={"x-api-key": "mala"})
    assert _aceptacion() == {"schmitz:prod": 2}


# ─── Salud: reglas de la píldora ───────────────────────────────────────────

def test_sin_peticiones_el_umbral_y_el_estado_son_los_de_siempre():
    provider_health.reset()
    provider_health.set_mode("x", "prod", "push")
    e = provider_health._entry("x", "prod")
    e["intervalo_tipico_seg"] = 300
    assert provider_health._umbral_silencio(e) == 1800
    provider_health.report_events_in("x", "prod", 1)
    assert provider_health.get_health_snapshot()[0]["status"] == "ok"
    provider_health.reset()


def test_el_silencio_de_peticiones_vuelve_a_idle(monkeypatch):
    provider_health.reset()
    provider_health.report_request_in("x", "prod")
    assert provider_health.get_health_snapshot()[0]["status"] == "ok"
    ahora = time.time()
    monkeypatch.setattr(provider_health.time, "time", lambda: ahora + 3 * 3600)
    assert provider_health.get_health_snapshot()[0]["status"] == "idle"
    provider_health.reset()


def test_el_umbral_se_adapta_al_ritmo_de_las_peticiones(monkeypatch):
    """Una integración que manda cada 40 min no se apaga a los 10 min de silencio."""
    provider_health.reset()
    reloj = [1_000_000.0]
    monkeypatch.setattr(provider_health.time, "time", lambda: reloj[0])
    for _ in range(3):
        provider_health.report_request_in("x", "prod")
        reloj[0] += 2400
    reloj[0] -= 2400 - 3600   # una hora después de la última petición
    assert provider_health.get_health_snapshot()[0]["status"] == "ok"
    provider_health.reset()


def test_la_medicion_no_agrega_latencia_apreciable():
    """Lo que se suma a cada petición es memoria pura: microsegundos."""
    from app.api.routers.dashboard import record_push_latency, reset_push_stats
    provider_health.reset()
    n = 20_000
    inicio = time.perf_counter()
    for _ in range(n):
        crono = latencia.Cronometro("bench", "prod")
        for tramo in ("auth", "parseo", "auth", "parseo", "rate_limit", "procesamiento"):
            crono.marca(tramo)
        provider_health.report_push_recibido("bench", "prod", cuenta_como_trafico=True)
        crono.cerrar()
        record_push_latency("bench:prod", 0.001)
    por_peticion_us = (time.perf_counter() - inicio) / n * 1e6
    provider_health.reset()
    latencia.limpiar()
    reset_push_stats()
    assert por_peticion_us < 200, f"{por_peticion_us:.1f} µs por petición"


# ─── Cableado: la píldora del panel real ───────────────────────────────────

def _pildoras(hub, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    r = hub.get("/api/stats", auth=(os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"]))
    assert r.status_code == 200, r.text
    entrada, salida = tmp_path / "stats.json", tmp_path / "pildoras.json"
    entrada.write_text(r.text, encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "verificar_panel_v196.js"), str(entrada), str(salida)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return {(x["proveedor"], x["entorno"]): x for x in json.loads(salida.read_text(encoding="utf-8"))}


def test_cableado_la_pildora_del_panel_pasa_a_con_trafico(hub, tmp_path):
    antes = _pildoras(hub, tmp_path)[("TIVE", "PROD")]
    assert (antes["clase"], antes["texto"]) == ("idle", "Esperando datos")
    assert _tive(hub).json()["note"] == "sin eventos para enviar"
    despues = _pildoras(hub, tmp_path)[("TIVE", "PROD")]
    assert despues["clase"] == "ok"
    assert despues["texto"] == "con trafico · sin eventos para RC"
    assert "Ultima peticion aceptada" in despues["tooltip"]


def test_cableado_un_rechazo_no_la_enciende(hub, tmp_path):
    _tive(hub, secreto="otro-secreto")
    assert _pildoras(hub, tmp_path)[("TIVE", "PROD")]["clase"] == "idle"


def test_cableado_la_pildora_de_schmitz_no_cambia(hub, tmp_path):
    provider_health.report_events_in("schmitz", "prod", 3)
    pildora = _pildoras(hub, tmp_path)[("SCHMITZ", "PROD")]
    assert (pildora["clase"], pildora["texto"]) == ("ok", "sin trafico")
    assert "Ultima peticion aceptada" not in pildora["tooltip"]


def test_cableado_el_diagnostico_lista_la_integracion(hub):
    _tive(hub)
    _studio(hub, {"plate": "AB123CD"})
    r = hub.get("/api/diagnostico/latencia", auth=(os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"]))
    assert r.status_code == 200
    assert r.json()["integraciones"] == ["acme:prod", "tive:prod"]
    assert r.json()["muestras"] == 2
