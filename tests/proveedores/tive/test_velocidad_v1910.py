"""
v1.9.10 — Bloque 1: velocidad estimada de equipos de Tive.

Los tests EJECUTAN el módulo de Tive (modulo.procesar), el endpoint real del
webhook, la base de estado y el worker. Las posiciones salen de los crudos
reales de Tive del repo (tests/fixtures/tive_crudos_2026-10.jsonl), movidas en
el espacio y el tiempo para cada caso.
"""
import asyncio
import copy
import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.providers.tive import estado, modulo, velocidad
from tests.proveedores.tive.test_v192_tive import CRUDOS, TRAMO_CONTENEDOR, _por_origen, _post, app_tive  # noqa: F401

RAIZ = Path(__file__).resolve().parents[3]
POSICION_REAL = "2026-10-01:1"          # K676092, wifi, 25 m de precisión
CIERRE_DETENIDO_REAL = "2026-10-01:793"  # ShipmentStopped de Q96665, cierre [["Created"],["Closed"]]
METROS_POR_GRADO = 2 * math.pi * velocidad.RADIO_TIERRA_METROS / 360
T0 = datetime(2026, 10, 1, 15, 24, 52, tzinfo=timezone.utc)
ENCENDIDO = {"velocidad_equipos": True, "posiciones_equipos": True, "alertas_trackers": True}


@pytest.fixture(autouse=True)
def estado_aislado(tmp_path, monkeypatch):
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado_tive"))
    modulo._metodos_avisados.clear()
    yield
    estado.cerrar_todo()
    modulo._metodos_avisados.clear()


_iid = [0]


def _procesar(payload, opciones=ENCENDIDO):
    _iid[0] += 1
    return modulo.procesar(payload, "prod", opciones, f"vel-{_iid[0]}")


def _posicion(segundos=0, norte_m=0.0, metodo="wifi", precision=25, nombre="K676092", lat=None, lon=None):
    """La posición real de K676092, desplazada `norte_m` metros al norte y `segundos` en el tiempo."""
    p = _por_origen(POSICION_REAL)
    momento = T0 + timedelta(seconds=segundos)
    p["DeviceName"] = nombre
    p["EntryTimeUtc"] = momento.strftime("%Y-%m-%dT%H:%M:%SZ")
    p["EntryTimeEpoch"] = int(momento.timestamp() * 1000)
    p["Location"]["Latitude"] = (lat if lat is not None else -34.809466) + norte_m / METROS_POR_GRADO
    p["Location"]["Longitude"] = lon if lon is not None else -58.540894
    p["Location"]["LocationMethod"] = metodo
    p["Location"]["Accuracy"]["Meters"] = precision
    return p


def _velocidad(payload, opciones=ENCENDIDO):
    (ev,) = _procesar(payload, opciones)
    return int(ev.speed), ev.detalle_velocidad


def _historial(equipo="K676092"):
    return estado.historial_velocidad("prod", equipo)


# ─── Reglas del cálculo, ejecutando el módulo de Tive ───────────────────────

def test_primera_posicion_es_1_sin_posicion_previa():
    valor, d = _velocidad(_posicion())
    assert (valor, d["motivo"]) == (1, "sin_posicion_previa")
    assert d["anterior"] is None and d["actual"]["metodo"] == "wifi" and d["actual"]["precision_m"] == 25


def test_quieto_con_variacion_menor_a_5_m_es_0():
    _velocidad(_posicion())
    valor, d = _velocidad(_posicion(segundos=300, norte_m=3))
    assert (valor, d["motivo"]) == (0, "detenido_umbral")
    assert d["distancia_m"] < velocidad.UMBRAL_METROS


def test_variacion_de_6_m_se_estima():
    _velocidad(_posicion())
    valor, d = _velocidad(_posicion(segundos=1, norte_m=6))
    assert d["motivo"] == "estimada" and d["distancia_m"] >= velocidad.UMBRAL_METROS
    assert valor == round(d["distancia_m"] / 1 * 3.6)


def test_camion_a_60_km_h_da_60_con_los_metodos_de_las_dos_lecturas():
    _velocidad(_posicion(metodo="cell", precision=1500))
    valor, d = _velocidad(_posicion(segundos=60, norte_m=1000, metodo="gps", precision=8))
    assert valor == 60 and d["motivo"] == "estimada"
    assert (d["anterior"]["metodo"], d["anterior"]["precision_m"]) == ("cell", 1500)
    assert (d["actual"]["metodo"], d["actual"]["precision_m"]) == ("gps", 8)
    assert d["segundos"] == 60 and 999 < d["distancia_m"] < 1001


def test_salto_de_500_km_en_1_minuto_se_informa_tal_cual():
    """Decisión de dirección: sin tope ni filtro de saltos."""
    _velocidad(_posicion())
    valor, d = _velocidad(_posicion(segundos=60, norte_m=500_000))
    assert d["motivo"] == "estimada" and 29_900 < valor < 30_100


def test_movimiento_real_que_redondea_a_0_o_1_se_informa_2():
    _velocidad(_posicion())
    valor, d = _velocidad(_posicion(segundos=3600, norte_m=6))   # 6 m en 1 hora: 0,006 km/h
    assert (valor, d["motivo"]) == (2, "estimada")
    valor, d = _velocidad(_posicion(segundos=7200, norte_m=6 + 1000))   # ~1 km/h
    assert (valor, d["motivo"]) == (2, "estimada")


def test_posicion_desordenada_se_calcula_contra_la_anterior_en_el_tiempo():
    _velocidad(_posicion())                                   # t=0, km 0
    _velocidad(_posicion(segundos=600, norte_m=10_000))       # t=10 min, km 10
    valor, d = _velocidad(_posicion(segundos=300, norte_m=5_000))   # llega tarde: t=5 min, km 5
    assert d["anterior"]["momento"] == T0.isoformat(), "comparó contra la última recibida"
    assert valor == 60


def test_lectura_repetida_mismo_valor_y_el_historial_no_cambia():
    _velocidad(_posicion())
    primero = _velocidad(_posicion(segundos=60, norte_m=1000))
    antes = [(l.momento, l.lat, l.lon, l.valor) for l in _historial()]
    # La misma lectura otra vez (una alerta de esa misma lectura, o Tive reenviando):
    r = modulo._calcular_velocidad("prod", _posicion(segundos=60, norte_m=1000))
    assert (r.valor, r.repetida) == (primero[0], True)
    assert r.detalle()["motivo"] == "estimada" and r.detalle()["lectura_repetida"] is True
    assert [(l.momento, l.lat, l.lon, l.valor) for l in _historial()] == antes


def test_misma_hora_con_coordenadas_distintas_es_1():
    _velocidad(_posicion())
    _velocidad(_posicion(segundos=60, norte_m=1000))
    r = modulo._calcular_velocidad("prod", _posicion(segundos=60, norte_m=2000))
    assert (r.valor, r.motivo) == (1, "misma_hora_coordenadas_distintas")
    assert len(_historial()) == 2, "la lectura en conflicto no entra al historial"


def test_coordenadas_redondeadas_a_5_decimales():
    _velocidad(_posicion(lat=-34.8094661234, lon=-58.5408944321))
    (lectura,) = _historial()
    assert (lectura.lat, lectura.lon) == (-34.80947, -58.54089)
    # Un corrimiento que no cambia el quinto decimal es la misma posición.
    valor, d = _velocidad(_posicion(segundos=60, lat=-34.8094699, lon=-58.5408901))
    assert (valor, d["distancia_m"]) == (0, 0.0)


def test_shipment_stopped_es_0_y_su_cierre_se_calcula():
    cierre = _por_origen(CIERRE_DETENIDO_REAL)                # Q96665, real
    apertura = copy.deepcopy(cierre)
    apertura["Alert"]["Details"] = [dict(apertura["Alert"]["Details"][0], Reasons=["Created", "Latest"])]
    assert modulo.estado_alerta(apertura, "prod") == "apertura"
    momento = datetime.fromisoformat(cierre["EntryTimeUtc"].replace("Z", "+00:00"))
    anterior = _posicion(nombre="Q96665", lat=cierre["Location"]["Latitude"] - 0.05,
                         lon=cierre["Location"]["Longitude"])
    anterior["EntryTimeUtc"] = (momento - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _procesar(anterior)

    r = modulo._calcular_velocidad("prod", apertura)
    assert (r.valor, r.motivo) == (0, "detenido_informado"), "Tive afirma que está detenido"
    # El -FIN NO confirma movimiento (Tive): se calcula como una posición. Va a
    # otra hora para que no sea la misma lectura de la apertura.
    cierre["EntryTimeUtc"] = (momento + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = modulo._calcular_velocidad("prod", cierre)
    assert r.motivo in ("estimada", "detenido_umbral") and r.anterior.momento == momento
    (ev,) = _procesar(cierre)
    assert ev.code == "ShipmentStopped-FIN" and ev.detalle_velocidad is not None


def test_maritimo_container_es_0_y_no_entra_al_historial():
    posicion = _por_origen("2026-10-01:1")
    posicion.update(DeviceId="867860087520523", DeviceName="K1234567")
    _procesar(posicion)                                       # aprende el par del tracker
    antes = len(_historial("K1234567"))
    (ev,) = _procesar(_por_origen(TRAMO_CONTENEDOR))
    assert (ev.speed, ev.detalle_velocidad["motivo"]) == (0.0, "maritimo")
    assert len(_historial("K1234567")) == antes


def test_el_historial_se_alimenta_aunque_la_posicion_no_se_envie():
    """Con posiciones_equipos apagado la posición no sale, pero el historial la guarda."""
    solo_velocidad = {"velocidad_equipos": True}
    assert _procesar(_posicion(), solo_velocidad) == []
    assert len(_historial()) == 1
    valor, d = _velocidad(_posicion(segundos=60, norte_m=1000))
    assert valor == 60


def test_sin_coordenadas_o_en_0_0_no_entra_al_historial():
    p = _posicion()
    p["Location"]["Latitude"] = p["Location"]["Longitude"] = 0
    modulo._calcular_velocidad("prod", p)
    assert _historial() == []


def test_el_historial_sobrevive_a_un_reinicio():
    _velocidad(_posicion())
    estado.cerrar_todo()                                      # el proceso se reinicia
    valor, d = _velocidad(_posicion(segundos=60, norte_m=1000))
    assert (valor, d["motivo"]) == (60, "estimada")


def test_el_historial_guarda_las_ultimas_20_en_el_tiempo():
    # Llegan desordenadas: primero las 20 más recientes, después 5 más viejas.
    for i in list(range(5, 25)) + list(range(5)):
        _velocidad(_posicion(segundos=60 * i, norte_m=1000 * i))
    historial = _historial()
    assert len(historial) == estado.HISTORIAL_POR_EQUIPO == 20
    assert historial[0].momento == T0 + timedelta(minutes=5), "se quedó con las últimas en LLEGAR"


def test_el_metodo_nunca_visto_se_sigue_avisando(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        _velocidad(_posicion(metodo="satelite"))
    assert "LocationMethod nunca visto: 'satelite'" in caplog.text


# ─── Interruptor ─────────────────────────────────────────────────────────────

def test_interruptor_apagado_es_identico_a_v199_con_los_crudos_reales():
    """Todos los crudos reales del repo con el interruptor apagado: speed None y sin historial."""
    sin = {"posiciones_equipos": True, "alertas_trackers": True}
    for r in CRUDOS:
        for ev in modulo.procesar(copy.deepcopy(r["payload"]), "prod", sin, f"off-{r['origen']}"):
            assert ev.speed is None and ev.detalle_velocidad is None, r["origen"]
    import sqlite3
    con = sqlite3.connect(estado._ruta("prod"))
    assert con.execute("SELECT COUNT(*) FROM velocidad_historial").fetchone()[0] == 0
    con.close()


def test_interruptor_encendido_con_los_crudos_reales():
    """Cada evento de equipo sale con 0, 1 o >= 2 y su detalle; los marítimos con 0."""
    vistos = set()
    for r in CRUDOS:
        for ev in modulo.procesar(copy.deepcopy(r["payload"]), "prod", ENCENDIDO, f"on-{r['origen']}"):
            d = ev.detalle_velocidad
            assert d is not None and ev.speed == d["valor"], r["origen"]
            assert d["valor"] in (0, 1) or d["valor"] >= 2
            assert d["motivo"] in velocidad.MOTIVOS
            vistos.add(d["motivo"])
    # Los tramos marítimos del fixture no tienen par aprendido (se descartan):
    # el marítimo se prueba aparte, con el par aprendido.
    assert "sin_posicion_previa" in vistos


# ─── Por el endpoint real, la base y el worker ──────────────────────────────

def _opciones_tive(opciones, use_mock=True):
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    cfg = db.query(ProviderConfig).filter_by(provider_name="tive", env="prod").one()
    cfg.module_options = opciones
    cfg.use_mock = use_mock
    db.commit()
    db.close()
    from app.core import config_cache
    config_cache.invalidate()


def _filas():
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("tive", "prod")
    try:
        return [(f.chassis_number, f.speed, json.loads(f.velocidad_detalle) if f.velocidad_detalle else None)
                for f in db.query(NormalizedRCEvent).order_by(NormalizedRCEvent.id)]
    finally:
        db.close()


def _ciclo_worker(monkeypatch):
    from app.services import rc_soap
    from app.worker import processor
    enviados = []

    def rc(self, eventos):
        enviados.extend(rc_soap.construir_evento_rc(e) for e in eventos)
        return [{"exception": None, "idJob": 70 + i} for i in range(len(eventos))]

    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_send_batch_sync", rc)
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    asyncio.run(processor.process_provider_events("tive", "prod"))
    return enviados


def test_cableado_endpoint_base_y_worker(app_tive, monkeypatch):
    _opciones_tive(ENCENDIDO, use_mock=False)
    for p in (_posicion(metodo="wifi"), _posicion(segundos=60, norte_m=1000, metodo="gps", precision=8)):
        assert _post(app_tive, p).status_code == 200
    filas = _filas()
    assert [(f[1], f[2]["motivo"]) for f in filas] == [(1.0, "sin_posicion_previa"), (60.0, "estimada")]
    assert filas[1][2]["anterior"]["metodo"] == "wifi" and filas[1][2]["actual"]["metodo"] == "gps"
    enviados = _ciclo_worker(monkeypatch)
    assert [e["speed"] for e in enviados] == ["1.0", "60.0"]
    assert all("_velocidad_estimada" not in e and "velocidad_detalle" not in e for e in enviados), \
        "el detalle no viaja a RC"


def test_cableado_interruptor_apagado_a_rc_llega_0_como_en_v199(app_tive, monkeypatch):
    _opciones_tive({"posiciones_equipos": True}, use_mock=False)
    for p in (_posicion(), _posicion(segundos=60, norte_m=1000)):
        _post(app_tive, p)
    assert [(f[1], f[2]) for f in _filas()] == [(None, None), (None, None)]
    assert [e["speed"] for e in _ciclo_worker(monkeypatch)] == ["0", "0"]


def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


def test_cableado_el_panel_muestra_la_velocidad_con_el_metodo_js_real(app_tive, tmp_path):
    """Filas REALES de /api/stats → renderRecentTable REAL: valor, motivo y método de las dos lecturas."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard
    _opciones_tive(ENCENDIDO)
    for p in (_posicion(metodo="cell", precision=1500),
              _posicion(segundos=600, norte_m=20_000, metodo="gps", precision=8),
              _posicion(segundos=1200, norte_m=20_002, metodo="wifi", precision=25)):
        _post(app_tive, p)
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[__import__("app.core.auth", fromlist=["x"]).verify_dashboard_auth] = lambda: None
    filas = TestClient(app).get("/api/stats").json()["recent"]
    assert sum(1 for f in filas if f.get("velocidad_detalle")) == 3
    r = _node({"globales": {"allRecentEvents": filas, "currentStatusFilter": "all", "currentProviderFilter": "all",
                            "currentLatencyFilter": "all", "currentSoloEventos": False, "expandedRows": []},
               "llamadas": [{"fn": "renderRecentTable"}]}, tmp_path)
    html = " ".join(r["elementos"]["recent-table-body"]["hijos"])
    assert "120 km/h (estimada · cell→gps)" in html
    assert "0 (detenido · gps→wifi)" in html
    assert "1 (no calculada: sin posición previa)" in html
    assert "precisión 1500 m" in html and "precisión 8 m" in html, "falta la precisión de las lecturas"
    # JSON RC: el detalle completo, marcado con "_" porque no viaja a RC.
    assert "_velocidad_estimada" in html


def test_textos_de_cada_motivo_js_real(tmp_path):
    casos = [
        {"valor": 34, "motivo": "estimada", "anterior": {"metodo": "gps"}, "actual": {"metodo": "gps"}},
        {"valor": 120, "motivo": "estimada", "anterior": {"metodo": "cell"}, "actual": {"metodo": "gps"}},
        {"valor": 0, "motivo": "detenido_umbral", "anterior": {"metodo": "wifi"}, "actual": {"metodo": "wifi"}},
        {"valor": 1, "motivo": "sin_posicion_previa", "anterior": None, "actual": {"metodo": "wifi"}},
        {"valor": 1, "motivo": "misma_hora_coordenadas_distintas", "anterior": {}, "actual": {}},
        {"valor": 0, "motivo": "detenido_informado", "anterior": None, "actual": {"metodo": "gps"}},
        {"valor": 0, "motivo": "maritimo", "anterior": None, "actual": None},
    ]
    r = _node({"llamadas": [{"fn": "_textoVelocidadEstimada", "args": [c]} for c in casos]}, tmp_path)
    assert r["resultados"] == [
        "34 km/h (estimada · gps→gps)", "120 km/h (estimada · cell→gps)", "0 (detenido · wifi→wifi)",
        "1 (no calculada: sin posición previa)", "1 (no calculada: misma hora con coordenadas distintas)",
        "0 (detenido · ShipmentStopped)", "0 (marítimo · container)",
    ]


# ─── Schmitz y Protrack no cambian ──────────────────────────────────────────

def test_schmitz_y_protrack_mandan_la_misma_velocidad_que_en_v199():
    """El speed a RC de Schmitz y Protrack sale de su propio dato, igual que en v1.9.9."""
    from app.core.dynamic_mapper import DynamicMapper
    from app.providers.schmitz.mapper import map_schmitz_payload
    from app.services.rc_soap import construir_evento_rc
    from tests.proveedores.protrack.test_protrack_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
    from tests.proveedores.schmitz.test_schmitz_v192 import SCHMITZ_REAL
    schmitz = map_schmitz_payload(copy.deepcopy(SCHMITZ_REAL), env="prod")[0]
    assert construir_evento_rc(schmitz)["speed"] == "63.0"
    assert getattr(schmitz, "detalle_velocidad", None) is None
    protrack = DynamicMapper.map_payload_multi(dict(PROTRACK_ITEM, speed=47), PROTRACK_MAPEO, "protrack", "prod")
    assert [construir_evento_rc(e)["speed"] for e in protrack] == ["47.0"] * len(protrack)
    assert all(getattr(e, "detalle_velocidad", None) is None for e in protrack)


def test_una_linea_debug_por_calculo(caplog):
    import logging
    with caplog.at_level(logging.DEBUG, logger="app.providers.tive.modulo"):
        _velocidad(_posicion(metodo="cell"))
        _velocidad(_posicion(segundos=60, norte_m=1000, metodo="gps"))
    lineas = [r for r in caplog.records if "Velocidad K676092" in r.getMessage()]
    assert [r.levelname for r in lineas] == ["DEBUG", "DEBUG"]
    assert "60 (estimada) cell->gps 1000.8 m en 60 s" in lineas[1].getMessage()   # 1 km + el redondeo a 5 decimales
