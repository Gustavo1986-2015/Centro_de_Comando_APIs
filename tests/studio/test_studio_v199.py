# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque1.py (reorganización por tema: studio).
"""
v1.9.9 · Bloque 1 — Integridad de datos hacia RC.

1.1 B-8   Studio sin patente: se descarta con aviso para el prestador.
1.2 B-15  Respuesta parcial de RC: un evento sin su respuesta se reintenta.
1.3 B-24  Coordenadas 0,0: se envían igual, con aviso en consola.
1.4 B-19  Schmitz: ignición sin IsIgnitionOn = sin dato; GPSSpeed=0 medido es 0.
1.5 B-28  Schmitz: velocidad sin medir vacía en la base, "0" a RC.

Todos EJECUTAN el código (mapeadores, webhooks, worker, cliente de RC, JS
real del panel): ninguno busca texto en el fuente.
"""
import asyncio
import copy
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import descartes

RAIZ = Path(__file__).resolve().parents[2]


@pytest.fixture
def descartes_propios(tmp_path, monkeypatch):
    from app.core import admision
    from app.core import dynamic_mapper
    descartes.esperar_escritura(2.0)
    monkeypatch.setattr(descartes, "DIRECTORIO", str(tmp_path / "descartes"))
    monkeypatch.setattr(dynamic_mapper, "_ultimo_aviso_sin_patente", {})
    descartes.reset()
    admision.reset()
    yield descartes
    descartes.esperar_escritura(2.0)


def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


ESQUEMA_STUDIO = {"base_mapping": {"chassis_number": "placa", "latitude": "lat", "longitude": "lon",
                                   "date": "fecha", "speed": "vel"}}
PAYLOAD_SIN_PLACA = {"imei": "864035052734572", "lat": -34.6, "lon": -58.4, "fecha": "2026-10-10T12:00:00Z"}


# ═══════════════════════════════════════════════════════════════════════════
# 1.1 B-8 — Studio sin patente
# ═══════════════════════════════════════════════════════════════════════════

def test_b8_sin_campo_de_patente_se_descarta_con_aviso_para_el_prestador(descartes_propios, caplog):
    from app.core.dynamic_mapper import DynamicMapper, MOTIVO_SIN_PATENTE
    with caplog.at_level(logging.WARNING, logger="DynamicMapper"):
        eventos = DynamicMapper.map_payload_multi(dict(PAYLOAD_SIN_PLACA), ESQUEMA_STUDIO, "studio", "prod",
                                                  False, False)
    assert eventos == [], "salió un evento sin patente real"
    linea = next(r.getMessage() for r in caplog.records if MOTIVO_SIN_PATENTE in r.getMessage())
    for dato in ("[STUDIO-prod]", "864035052734572", "'placa'", "lat=-34.6, lon=-58.4", "prestador"):
        assert dato in linea, dato
    (u,) = descartes.consultar(10)["ultimos"]
    assert (u["proveedor"], u["origen"], u["motivo"], u["equipo"]) == ("studio", "mapeo", MOTIVO_SIN_PATENTE,
                                                                       "864035052734572")
    assert "'placa'" in u["detalle"] and (u["latitud"], u["longitud"]) == (-34.6, -58.4)


def test_b8_con_patente_no_cambia_nada(descartes_propios):
    from app.core.dynamic_mapper import DynamicMapper
    (ev,) = DynamicMapper.map_payload_multi(dict(PAYLOAD_SIN_PLACA, placa="AB123CD"), ESQUEMA_STUDIO,
                                            "studio", "prod", False, False)
    assert ev.chassis_number == "AB123CD" and ev.serial_number == "864035052734572"
    assert descartes.consultar(10)["ultimos"] == []


def test_b8_el_diccionario_que_traduce_el_identificador_sigue_dando_la_patente(config_aislada, descartes_propios):
    """Sin el campo de patente pero con traducción del IMEI: esa SÍ es la patente real."""
    from app.core.dynamic_mapper import DynamicMapper
    from app.database import get_session
    from app.models.config_models import ProviderDictionary
    db = get_session("system_config", "global")
    db.add(ProviderDictionary(provider_name="studio", env="prod", dict_key="864035052734572", dict_value="PT734572"))
    db.commit()
    db.close()
    (ev,) = DynamicMapper.map_payload_multi(dict(PAYLOAD_SIN_PLACA), ESQUEMA_STUDIO, "studio", "prod", False, True)
    assert ev.chassis_number == "PT734572"
    assert descartes.consultar(10)["ultimos"] == []


def test_b8_la_consola_no_se_inunda_pero_el_registro_cuenta_cada_uno(descartes_propios, caplog):
    from app.core.dynamic_mapper import DynamicMapper, MOTIVO_SIN_PATENTE
    with caplog.at_level(logging.WARNING, logger="DynamicMapper"):
        for _ in range(5):
            DynamicMapper.map_payload_multi(dict(PAYLOAD_SIN_PLACA), ESQUEMA_STUDIO, "studio", "prod", False, False)
        DynamicMapper.map_payload_multi(dict(PAYLOAD_SIN_PLACA, imei="111"), ESQUEMA_STUDIO, "studio", "prod",
                                        False, False)
    assert sum(MOTIVO_SIN_PATENTE in r.getMessage() for r in caplog.records) == 2, "una línea por equipo y minuto"
    por_equipo = {e["equipo"]: e for e in descartes.consultar(10)["por_equipo"]}
    assert por_equipo["864035052734572"]["total"] == 5 and por_equipo["111"]["total"] == 1
    assert (por_equipo["864035052734572"]["latitud"], por_equipo["864035052734572"]["longitud"]) == (-34.6, -58.4)


def test_cableado_b8_webhook_del_studio(config_aislada, descartes_propios, monkeypatch):
    """Camino PUSH real: el webhook acepta, no encola nada y el descarte queda registrado."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="studio", env="prod", provider_type="push", is_active=True, use_mock=True,
                          webhook_auth_secret_enc=encrypt("CLAVE"), mapping_schema=ESQUEMA_STUDIO))
    db.commit()
    db.close()
    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    r = TestClient(app).post("/webhook/dynamic/studio?env=prod", json=PAYLOAD_SIN_PLACA, headers={"x-api-key": "CLAVE"})
    assert r.status_code == 200, r.text
    db = get_session("studio", "prod")
    assert db.query(NormalizedRCEvent).count() == 0
    db.close()
    (u,) = descartes.consultar(10)["ultimos"]
    assert (u["origen"], u["equipo"]) == ("mapeo", "864035052734572")


async def test_cableado_b8_pull_del_studio(config_aislada, descartes_propios, monkeypatch):
    """Camino PULL real (sin diccionario): nada encolado, el descarte registrado."""
    from app.worker import processor, pull_engine
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    await pull_engine.process_and_enqueue("studio", "prod", {"code": 0, "record": [dict(PAYLOAD_SIN_PLACA)]},
                                          ESQUEMA_STUDIO, enable_state_dedup=False)
    assert [u["origen"] for u in descartes.consultar(10)["ultimos"]] == ["mapeo"]


def test_cableado_b8_panel_muestra_los_sin_patente_por_equipo(config_aislada, descartes_propios, tmp_path):
    """Endpoint real → _renderDescartes REAL: la tabla por equipo con su cuenta."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard
    from app.core.dynamic_mapper import DynamicMapper
    for _ in range(3):
        DynamicMapper.map_payload_multi(dict(PAYLOAD_SIN_PLACA), ESQUEMA_STUDIO, "studio", "prod", False, False)
    app = FastAPI()
    app.include_router(dashboard.router)
    d = TestClient(app).get("/api/diagnostico/descartes?limite=50",
                            auth=(os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])).json()
    (html,) = _node({"llamadas": [{"fn": "_renderDescartes", "args": [d]}]}, tmp_path)["resultados"]
    assert "Sin patente real, por equipo" in html
    tabla = html.split("Sin patente real, por equipo")[1].split("</table>")[0]
    assert "864035052734572" in tabla and "STUDIO" in tabla and ">3<" in tabla.replace(" ", "")
    assert "-34.60000, -58.40000" in tabla and "placa" in tabla
