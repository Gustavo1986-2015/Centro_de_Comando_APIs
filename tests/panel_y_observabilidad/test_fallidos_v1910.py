"""
v1.9.10 — Bloque 2: la tarjeta "Fallidos".

Antes contaba los fallos acumulados del día y no bajaba al recuperarlos con el
reintento manual. Ahora el número grande es lo que está en 'failed' AHORA en
las bases de cola; debajo, chico, "hoy: N · recuperados: M". El botón
"↻ Reintentar fallidos…" aparece solo si el número grande es mayor que 0 (y
con el filtro "Fallidos", como en v1.9.9).
"""
import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]


def _auth():
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def _cliente():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard, reintentos
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(reintentos.router)
    return TestClient(app)


def _preparar(monkeypatch):
    """schmitz/prod con 3 fallidos (2 de ayer, 1 de hoy), 1 enviado y el acumulado de hoy en 5 fallos."""
    from app.api.routers import schmitz
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import DailyStat, ProviderConfig
    from app.models.db_models import NormalizedRCEvent
    from app.worker import processor
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                          use_mock=False, rc_user="u", rc_password_enc=encrypt("x")))
    db.add(DailyStat(date=datetime.now().date(), provider="schmitz", env="prod", sent_count=10, failed_count=5))
    db.commit()
    db.close()
    nombres = ["F1", "F2", "F3", "OK"]
    schmitz._persist_batch([({"ChassisNumber": n, "DeviceTime": "2026-10-23T12:00:00Z",
                              "StatusData": [{"Position": {"Latitude": 1.0, "Longitude": 2.0}}]}, "prod", f"t-{n}")
                            for n in nombres])
    ayer = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    db = get_session("schmitz", "prod")
    ids = {}
    for ev in db.query(NormalizedRCEvent).all():
        ids[ev.chassis_number] = ev.id
        if ev.chassis_number == "OK":
            ev.status, ev.job_id = "sent", "123"
        else:
            ev.status, ev.job_id, ev.rc_response = "failed", "rc_business_1", "Rechazo de negocio RC: x"
            if ev.chassis_number in ("F1", "F2"):
                ev.created_at = ayer
    db.commit()
    db.close()
    return ids


def test_el_numero_grande_es_lo_que_esta_en_failed_ahora(config_aislada, monkeypatch):
    _preparar(monkeypatch)
    d = _cliente().get("/api/stats", auth=_auth()).json()
    assert (d["fallidos_ahora"], d["fallidos_hoy"], d["recuperados_hoy"]) == (3, 5, 0)
    assert d["failed"] == 5, "la clave de siempre no cambia"


def test_al_recuperarlos_baja_y_se_cuentan_como_recuperados(config_aislada, monkeypatch):
    ids = _preparar(monkeypatch)
    c = _cliente()
    r = c.post("/api/reintentos/reintentar", auth=_auth(),
               json={"provider": "schmitz", "env": "prod", "ids": [ids["F1"], ids["F3"]], "cantidad_confirmada": 2})
    assert r.status_code == 200
    d = c.get("/api/stats", auth=_auth()).json()
    assert (d["fallidos_ahora"], d["fallidos_hoy"], d["recuperados_hoy"]) == (1, 5, 2)

    # El worker los envía: suman en Enviados (salieron hacia RC) y siguen como recuperados.
    from app.services import rc_soap
    from app.worker import processor
    sumas = []
    monkeypatch.setattr(processor, "increment_daily_stats", lambda p, e, s, f: sumas.append((p, e, s, f)))
    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync",
                        lambda self, ev: [{"exception": None, "idJob": 900 + i} for i in range(len(ev))])

    async def ciclo():
        await processor.process_provider_events("schmitz", "prod")
        await asyncio.sleep(0.2)   # el acumulado diario va en una tarea aparte
    asyncio.run(ciclo())
    assert sumas == [("schmitz", "prod", 2, 0)], "los recuperados que salieron suman en Enviados"
    d = c.get("/api/stats", auth=_auth()).json()
    assert (d["fallidos_ahora"], d["recuperados_hoy"]) == (1, 2)


def test_con_filtro_de_integracion(config_aislada, monkeypatch):
    _preparar(monkeypatch)
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="protrack", env="prod", provider_type="pull"))
    db.commit()
    db.close()
    c = _cliente()
    assert c.get("/api/stats", params={"provider": "protrack"}, auth=_auth()).json()["fallidos_ahora"] == 0
    assert c.get("/api/stats", params={"provider": "schmitz"}, auth=_auth()).json()["fallidos_ahora"] == 3


def test_migracion_agrega_recovered_count_a_una_base_vieja(config_aislada):
    """Una base de v1.9.9 (sin la columna) la recibe con 0, sin tocar lo que había. Idempotente."""
    import sqlite3
    from app import database
    database.get_session("system_config", "global").close()      # el esquema completo
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    con.execute("ALTER TABLE daily_stats DROP COLUMN recovered_count")   # como una base de v1.9.9
    con.execute("INSERT INTO daily_stats (date, provider, env, sent_count, failed_count)"
                " VALUES ('2026-10-22', 'schmitz', 'prod', 10, 2)")
    con.commit()
    con.close()
    database.check_and_migrate_db()
    database.check_and_migrate_db()   # idempotente
    con = sqlite3.connect(os.path.join("db", "system_config_global.db"))
    assert con.execute("SELECT sent_count, failed_count, recovered_count FROM daily_stats").fetchone() == (10, 2, 0)
    con.close()


# ─── JS real del panel ───────────────────────────────────────────────────────

def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


def _render(datos, filtro, tmp_path):
    return _node({
        "globales": {"currentStatusFilter": filtro, "currentProviderFilter": "all", "currentLatencyFilter": "all",
                     "currentSoloEventos": False, "allRecentEvents": [], "expandedRows": []},
        "stubs": ["fetchFilteredEvents", "renderRecentTable", "updateSparkline"],
        "llamadas": [{"fn": "renderStats", "args": [datos]}],
    }, tmp_path)["elementos"]


def test_cableado_la_tarjeta_con_los_datos_reales_js_real(config_aislada, monkeypatch, tmp_path):
    """Respuesta REAL de /api/stats (después de un reintento real) → renderStats REAL."""
    ids = _preparar(monkeypatch)
    c = _cliente()
    c.post("/api/reintentos/reintentar", auth=_auth(),
           json={"provider": "schmitz", "env": "prod", "ids": [ids["F3"]], "cantidad_confirmada": 1})
    datos = c.get("/api/stats", auth=_auth()).json()
    el = _render(datos, "failed", tmp_path)
    assert el["val-failed"]["textContent"] == "2", "el número grande no es lo que está en failed ahora"
    assert el["val-failed-detalle"]["textContent"] == "hoy: 5 · recuperados: 1"
    assert el["btn-reintentar-fallidos"]["style"]["display"] == ""


def test_cableado_el_boton_solo_con_fallidos_sin_resolver_js_real(tmp_path):
    base = {"pending": 0, "sent": 0, "failed": 4, "retries": 0, "fallidos_hoy": 4, "recuperados_hoy": 4,
            "recent": [], "push_per_provider": {}, "push_stats": {}, "provider_health": [], "mock_providers": []}
    sin = _render(dict(base, fallidos_ahora=0), "failed", tmp_path)
    assert sin["val-failed"]["textContent"] == "0" and sin["btn-reintentar-fallidos"]["style"]["display"] == "none"
    con = _render(dict(base, fallidos_ahora=3), "all", tmp_path)
    assert con["btn-reintentar-fallidos"]["style"]["display"] == "none", "sin el filtro Fallidos no se ofrece"
    con = _render(dict(base, fallidos_ahora=3), "failed", tmp_path)
    assert con["btn-reintentar-fallidos"]["style"]["display"] == ""
