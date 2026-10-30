"""
v1.9.11 · Bloque 2a — Tive sin posición.

Tive manda la ubicación vacía cuando no puede ubicar el equipo, con datos
válidos. Con "enviar_sin_posicion" (ENCENDIDO por defecto, decisión del
usuario) el evento se acepta: a RC van latitude "0" y longitude "0", en la
cola quedan NULL. Apagado, como en v1.9.10: se descarta (sin_coordenadas).

Casos reales del fixture (tests/fixtures/tive_crudos_2026-10.jsonl):
  · 2026-10-02:104 — Connectivity de K1153407, apertura, Location vacía.
  · 2026-10-02:125 — su cierre, también sin posición.

Todo se EJECUTA: módulo, endpoint, base, worker, cliente SOAP del hub y el
sobre SOAP serializado contra el WSDL real de RC; el panel con su JS real.
"""
import copy
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import descartes
from app.providers.tive import estado, modulo
from tests.proveedores.tive.test_v192_tive import (  # noqa: F401
    CONNECTIVITY, POSICION_TRACKER, TRAMO_CONTENEDOR, _por_origen, _post, app_tive)
from tests.proveedores.tive.test_velocidad_v1910 import _opciones_tive
from tests.rc import rc_falso

RAIZ = Path(__file__).resolve().parents[3]
CONNECTIVITY_CIERRE = "2026-10-02:125"
ALERTAS = {"alertas_trackers": True}
ALERTAS_V1910 = {"alertas_trackers": True, "enviar_sin_posicion": False}
TEXTO = "Sin posición (Tive no pudo ubicar)"


@pytest.fixture(autouse=True)
def estado_aislado(tmp_path, monkeypatch):
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado_tive"))
    modulo._metodos_avisados.clear()
    yield
    estado.cerrar_todo()
    modulo._metodos_avisados.clear()


_iid = [0]


def _procesar(payload, opciones):
    _iid[0] += 1
    return modulo.procesar(copy.deepcopy(payload), "prod", opciones, f"sp-{_iid[0]}")


def _cola():
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("tive", "prod")
    try:
        return [dict(patente=f.chassis_number, codigo=f.code, lat=f.latitude, lon=f.longitude,
                     estado=f.status, speed=f.speed)
                for f in db.query(NormalizedRCEvent).order_by(NormalizedRCEvent.id)]
    finally:
        db.close()


def _descartes_de(categoria):
    descartes.esperar_escritura(2.0)
    return [d for d in descartes.consultar(1000)["ultimos"] if d["categoria"] == categoria]


def _sin_ubicacion(payload):
    payload["Location"]["Latitude"] = None
    payload["Location"]["Longitude"] = None
    return payload


# ─── Módulo ──────────────────────────────────────────────────────────────────

def test_por_defecto_el_interruptor_esta_encendido():
    assert modulo.opciones_efectivas(None)["enviar_sin_posicion"] is True


def test_la_alerta_real_sin_posicion_sale_sin_coordenadas_inventadas(caplog):
    with caplog.at_level(logging.INFO, logger="app.core.contrato"):
        (ev,) = _procesar(_por_origen(CONNECTIVITY), ALERTAS)
    assert (ev.chassis_number, ev.code) == ("K1153407", "Connectivity")
    assert ev.latitude is None and ev.longitude is None, "no se inventa ni se reutiliza coordenada"
    assert ev.sin_posicion is True
    lineas = [r for r in caplog.records if "sin posición" in r.getMessage()]
    assert len(lineas) == 1 and lineas[0].levelno == logging.INFO
    assert "patente=K1153407" in lineas[0].getMessage() and "código=Connectivity" in lineas[0].getMessage()
    assert "Coordenadas 0,0" not in caplog.text, "no es el aviso de 0,0"


def test_no_reutiliza_la_ultima_posicion_conocida_del_equipo():
    """Una posición previa del mismo equipo no se copia al evento sin posición."""
    posicion = _por_origen(POSICION_TRACKER)
    posicion.update(DeviceName="K1153407", DeviceId="868977084374560")
    _procesar(posicion, {"posiciones_equipos": True, "velocidad_equipos": True})
    (ev,) = _procesar(_por_origen(CONNECTIVITY), {**ALERTAS, "velocidad_equipos": True})
    assert ev.latitude is None and ev.longitude is None


def test_apagado_se_descarta_como_en_v1910():
    antes = len(_descartes_de("sin_coordenadas"))
    assert _procesar(_por_origen(CONNECTIVITY), ALERTAS_V1910) == []
    nuevos = _descartes_de("sin_coordenadas")[: len(_descartes_de("sin_coordenadas")) - antes]
    assert nuevos and nuevos[0]["equipo"] == "K1153407"
    assert nuevos[0]["motivo"] == "falta latitud, longitud"


def test_sin_posicion_y_sin_fecha_se_descarta():
    alerta = _por_origen(CONNECTIVITY)
    alerta.pop("EntryTimeUtc", None)
    alerta.pop("EntryTimeEpoch", None)
    assert _procesar(alerta, ALERTAS) == []
    assert any(d["equipo"] == "K1153407" for d in _descartes_de("sin_fecha"))


def test_sin_posicion_y_sin_patente_se_descarta():
    alerta = _por_origen(CONNECTIVITY)
    alerta["Alert"]["DeviceName"] = None
    alerta["Alert"]["DeviceId"] = "999999999999999"
    alerta.pop("DeviceName", None)
    alerta.pop("DeviceId", None)
    assert _procesar(alerta, ALERTAS) == []


def test_posicion_de_equipo_sin_posicion_sale():
    posicion = _sin_ubicacion(_por_origen(POSICION_TRACKER))
    posicion["DeviceName"] = "K7654321"
    (ev,) = _procesar(posicion, {"posiciones_equipos": True})
    assert ev.chassis_number == "K7654321" and ev.sin_posicion and ev.latitude is None
    posicion = _sin_ubicacion(_por_origen(POSICION_TRACKER))
    posicion["DeviceName"] = "K7654322"
    assert _procesar(posicion, {"posiciones_equipos": True, "enviar_sin_posicion": False}) == []


def test_tramo_de_contenedor_sin_posicion_sale_con_la_patente_aprendida():
    aprendida = _por_origen(POSICION_TRACKER)
    aprendida.update(DeviceId="867860087520523", DeviceName="K1234567")
    _procesar(aprendida, {})
    (ev,) = _procesar(_sin_ubicacion(_por_origen(TRAMO_CONTENEDOR)), {})
    assert ev.chassis_number == "K1234567" and ev.sin_posicion and ev.latitude is None


def test_tramo_sin_posicion_con_el_interruptor_apagado_es_sin_coordenadas_como_en_v1910():
    aprendida = _por_origen(POSICION_TRACKER)
    aprendida.update(DeviceId="867860087520523", DeviceName="K1234567")
    _procesar(aprendida, {})
    antes = len(_descartes_de("sin_coordenadas"))
    assert _procesar(_sin_ubicacion(_por_origen(TRAMO_CONTENEDOR)), {"enviar_sin_posicion": False}) == []
    assert len(_descartes_de("sin_coordenadas")) == antes + 1
    # El mismo descarte que v1.9.10: lo hace el módulo, con su motivo (no el contrato).
    assert _descartes_de("sin_coordenadas")[0]["motivo"].startswith(
        "evento sin nombre de equipo y sin coordenadas en Location")


def test_velocidad_1_sin_posicion_sin_tocar_el_historial():
    posicion = _por_origen(POSICION_TRACKER)
    posicion.update(DeviceName="K1153407")
    _procesar(posicion, {"velocidad_equipos": True})
    historial = estado.historial_velocidad("prod", "K1153407")
    assert len(historial) == 1
    (ev,) = _procesar(_por_origen(CONNECTIVITY), {**ALERTAS, "velocidad_equipos": True})
    assert ev.speed == 1.0
    assert ev.detalle_velocidad["motivo"] == "sin_posicion"
    assert ev.detalle_velocidad["texto"] == "sin posición"
    assert estado.historial_velocidad("prod", "K1153407") == historial, "entró al historial"


def test_con_filtrar_un_tive_sin_posicion_sigue_saliendo(monkeypatch):
    """La opción de 0,0 no alcanza a un evento que Tive no pudo ubicar."""
    from app.core import contrato
    monkeypatch.setattr(contrato, "opcion_coordenadas_cero", lambda p, e: contrato.CEROS_FILTRAR)
    (ev,) = _procesar(_por_origen(CONNECTIVITY), ALERTAS)
    assert ev.sin_posicion
    cero = _por_origen(CONNECTIVITY_CIERRE)
    cero["Location"].update(Latitude=0, Longitude=0)
    assert _procesar(cero, ALERTAS) == [], "un 0,0 informado por Tive sí se filtra"


# ─── Endpoint, base, worker, cliente RC y sobre SOAP ─────────────────────────

def test_cableado_alerta_real_hasta_el_sobre_soap(app_tive, monkeypatch):
    _opciones_tive(ALERTAS, use_mock=False)
    assert _post(app_tive, _por_origen(CONNECTIVITY)).status_code == 200
    (fila,) = _cola()
    assert (fila["patente"], fila["codigo"]) == ("K1153407", "Connectivity")
    assert fila["lat"] is None and fila["lon"] is None, "en la cola quedan NULL, no 0"

    falso = rc_falso.instalar(monkeypatch)
    rc_falso.procesar_cola(monkeypatch, "tive", "prod")
    (enviado,) = falso.eventos()
    assert enviado["latitude"] == "0" and enviado["longitude"] == "0"
    (nodo,) = falso.nodos()
    assert nodo["latitude"] == "0" and nodo["longitude"] == "0" and nodo["asset"] == "K1153407"
    assert _cola()[0]["estado"] == "sent"
    assert _cola()[0]["lat"] is None, "el envío no escribe el 0 en la cola"


def test_cableado_apagado_no_entra_a_la_cola(app_tive):
    _opciones_tive(ALERTAS_V1910)
    r = _post(app_tive, _por_origen(CONNECTIVITY))
    assert r.status_code == 200 and r.json()["events_count"] == 0
    assert _cola() == []


def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


def test_cableado_el_panel_muestra_sin_posicion_js_real(app_tive, tmp_path):
    """Filas REALES de /api/stats → renderRecentTable REAL."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard
    from app.core.auth import verify_dashboard_auth
    _opciones_tive(ALERTAS)
    _post(app_tive, _por_origen(CONNECTIVITY))
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[verify_dashboard_auth] = lambda: None
    (fila,) = TestClient(app).get("/api/stats").json()["recent"]
    assert fila["coords"] == TEXTO and fila["sin_posicion"] is True
    r = _node({"globales": {"allRecentEvents": [fila], "currentStatusFilter": "all", "currentProviderFilter": "all",
                            "currentLatencyFilter": "all", "currentSoloEventos": False, "expandedRows": []},
               "llamadas": [{"fn": "renderRecentTable"}]}, tmp_path)
    html = " ".join(r["elementos"]["recent-table-body"]["hijos"])
    assert TEXTO in html
    assert "Sin GPS" not in html
