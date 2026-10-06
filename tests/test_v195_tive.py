"""
v1.9.5 · Puntos 2, 3 y 5 — Tive. Todo con crudos reales de
tests/fixtures/tive_crudos_2026-10.jsonl (origen "<fecha>:<línea>").

2. Hora de los cierres: RecoveredAlertDate cuando trae fecha real.
   Medido: en el cierre corto 2284be50 ([["Created","Closed"]]) EntryTimeUtc
   es 13:44:12, la hora de la apertura, y la recuperación 13:49:12. El -FIN
   salía con la hora de la apertura.
3. Beacon real del 05/10: F9C1B0DF0E1D, nombre W15481. Hasta ahora solo mandó
   posiciones; las alertas de beacon salen con el nombre del beacon.
5. Interruptor "posiciones_equipos", apagado: con él encendido las posiciones
   de trackers y beacons salen con la patente de su propio DeviceName.
"""
import copy
import json
import logging
import os
from datetime import datetime, timezone

import pytest

from app.providers.tive import estado, modulo
from tests.test_v192_tive import _post, app_tive  # noqa: F401 (fixture)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tive_crudos_2026-10.jsonl")
CRUDOS = {json.loads(l)["origen"]: json.loads(l)["payload"] for l in open(FIXTURE, encoding="utf-8")}

APERTURA_2284 = "2026-10-05:103"     # HumidityMax [["Created","Latest"]]  13:44:12
CIERRE_2284 = "2026-10-05:104"       # HumidityMax [["Created","Closed"]]  recuperada 13:49:12
CIERRE_6AED = "2026-10-05:75"        # LocationPerimeter [["Created"],["Closed"]] 12:54:12 / 12:59:12
CIERRE_SIN_FECHA = "2026-10-02:125"  # Connectivity [["Created"],["Closed"]], RecoveredAlertDate 0001
SHOCK = "2026-10-05:64"              # ShockEvents puntual, sin recuperación
BEACON_POSICION = "2026-10-05:239"   # W15481 / F9C1B0DF0E1D, posición
TRACKER_CON_BEACON = "2026-10-05:229"  # K393478, su envío lleva al beacon en DeviceIds
TRACKER_SIN_ENVIO = "2026-10-01:1"   # K676092, posición sin envío

BEACON_ID, BEACON_NOMBRE = "F9C1B0DF0E1D", "W15481"
TRACKERS = {"alertas_trackers": True}


def _p(origen):
    return copy.deepcopy(CRUDOS[origen])


def _utc(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def estado_aislado(tmp_path, monkeypatch):
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    yield
    estado.cerrar_todo()


_n = 0


def _procesar(payload, opciones=None):
    global _n
    _n += 1
    return modulo.procesar(payload, "prod", opciones or {}, f"iid-{_n}")


def test_lo_medido_esta_en_el_fixture():
    assert CRUDOS[CIERRE_2284]["EntryTimeUtc"] == CRUDOS[APERTURA_2284]["EntryTimeUtc"] == "2026-10-05T13:44:12Z"
    assert CRUDOS[CIERRE_2284]["RecoveredAlertDate"] == "2026-10-05T13:49:12Z"
    assert CRUDOS[CIERRE_SIN_FECHA]["RecoveredAlertDate"].startswith("0001-01-01")
    b = CRUDOS[BEACON_POSICION]
    assert (b["DeviceId"], b["DeviceName"]) == (BEACON_ID, BEACON_NOMBRE)
    assert BEACON_ID in CRUDOS[TRACKER_CON_BEACON]["Shipment"]["DeviceIds"]


# ─── 2. Hora de los cierres ────────────────────────────────────────────────

def test_el_cierre_corto_sale_con_la_hora_de_recuperacion():
    (apertura,) = _procesar(_p(APERTURA_2284), TRACKERS)
    (cierre,) = _procesar(_p(CIERRE_2284), TRACKERS)
    assert (apertura.code, apertura.date) == ("HumidityMax", _utc("2026-10-05T13:44:12Z"))
    assert (cierre.code, cierre.date) == ("HumidityMax-FIN", _utc("2026-10-05T13:49:12Z"))


def test_el_cierre_de_rango_tambien():
    (cierre,) = _procesar(_p(CIERRE_6AED), TRACKERS)
    assert (cierre.code, cierre.date) == ("LocationPerimeter-FIN", _utc("2026-10-05T12:59:12Z"))


def test_sin_fecha_de_recuperacion_queda_entry_time():
    # El único cierre real sin fecha (Connectivity) tampoco trae coordenadas y
    # lo frena el contrato; se usa el 6aed7e20 con el "sin valor" literal de Tive.
    assert modulo._fecha_de_cierre("prod", _p(CIERRE_SIN_FECHA)) is None
    payload = _p(CIERRE_6AED)
    payload["RecoveredAlertDate"] = CRUDOS[CIERRE_SIN_FECHA]["RecoveredAlertDate"]
    (cierre,) = _procesar(payload, TRACKERS)
    assert (cierre.code, cierre.date) == ("LocationPerimeter-FIN", _utc("2026-10-05T12:54:12Z"))


def test_una_recuperacion_ilegible_avisa_y_queda_entry_time(caplog):
    payload = _p(CIERRE_6AED)
    payload["RecoveredAlertDate"] = "ayer a la tarde"
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        (cierre,) = _procesar(payload, TRACKERS)
    assert cierre.date == _utc("2026-10-05T12:54:12Z")
    assert "RecoveredAlertDate ilegible" in caplog.text


def test_la_puntual_y_la_apertura_no_cambian():
    (shock,) = _procesar(_p(SHOCK), TRACKERS)
    assert (shock.code, shock.date) == ("ShockEvents", _utc("2026-10-05T12:52:34Z"))
    # Una apertura con RecoveredAlertDate (no se vio, pero no debe moverla).
    apertura = _p(APERTURA_2284)
    apertura["RecoveredAlertDate"] = "2026-10-05T13:49:12Z"
    (ev,) = _procesar(apertura, TRACKERS)
    assert ev.date == _utc("2026-10-05T13:44:12Z")


def test_cableado_el_fin_llega_a_la_base_con_la_hora_de_recuperacion(app_tive):
    """Por el webhook real, con el módulo, hasta la fila que va a RC."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent
    db = get_session("system_config", "global")
    db.query(ProviderConfig).filter_by(provider_name="tive").one().module_options = TRACKERS
    db.commit()
    db.close()
    assert _post(app_tive, _p(CIERRE_2284)).status_code == 200
    db = get_session("tive", "prod")
    try:
        (fila,) = db.query(NormalizedRCEvent).all()
    finally:
        db.close()
    assert fila.code == "HumidityMax-FIN"
    fecha = fila.date if fila.date.tzinfo else fila.date.replace(tzinfo=timezone.utc)
    assert fecha == _utc("2026-10-05T13:49:12Z")


# ─── 3. Beacon real F9C1B0DF0E1D / W15481 ──────────────────────────────────

def _alerta_del_beacon(sin_nombre=False):
    """
    DERIVADA, no real: hasta el 05/10 el beacon solo mandó posiciones. Es la
    alerta real 2026-10-05:104 con los identificadores reales del beacon en
    lugar de los del tracker.
    """
    payload = _p(CIERRE_2284)
    for nodo in (payload, payload["Alert"]):
        nodo["DeviceId"] = BEACON_ID
        nodo["DeviceName"] = None if sin_nombre else BEACON_NOMBRE
        nodo["EntityName"] = None if sin_nombre else BEACON_NOMBRE
    return payload


def test_la_posicion_real_del_beacon_ensena_su_par_y_no_sale_por_defecto():
    assert _procesar(_p(BEACON_POSICION)) == []
    assert estado.nombre_de("prod", BEACON_ID) == BEACON_NOMBRE
    assert not BEACON_ID.isdigit(), "la regla tracker/beacon se apoya en que no sea numérico"


def test_la_alerta_del_beacon_sale_con_el_nombre_del_beacon():
    # Los interruptores por defecto: alertas de trackers apagadas, de beacons encendidas.
    (ev,) = _procesar(_alerta_del_beacon())
    assert (ev.chassis_number, ev.serial_number, ev.code) == (BEACON_NOMBRE, BEACON_ID, "HumidityMax-FIN")
    assert ev.date == _utc("2026-10-05T13:49:12Z")
    # La misma alerta del tracker, con los mismos interruptores, no sale.
    assert _procesar(_p(CIERRE_2284)) == []


def test_la_alerta_del_beacon_sin_nombre_usa_su_par_y_no_el_del_tracker():
    _procesar(_p(BEACON_POSICION))      # aprende F9C1B0DF0E1D -> W15481
    _procesar(_p(TRACKER_CON_BEACON))   # aprende 865648069052454 -> K393478
    (ev,) = _procesar(_alerta_del_beacon(sin_nombre=True))
    assert (ev.chassis_number, ev.serial_number) == (BEACON_NOMBRE, BEACON_ID)


def test_el_interruptor_de_beacons_la_frena():
    assert _procesar(_alerta_del_beacon(), {"alertas_beacons": False}) == []


# ─── 5. Posiciones de equipos ──────────────────────────────────────────────

EQUIPOS = {"posiciones_equipos": True}


@pytest.mark.parametrize("origen", [BEACON_POSICION, TRACKER_CON_BEACON, TRACKER_SIN_ENVIO])
def test_apagado_se_descartan_como_hoy(origen, monkeypatch):
    from app.core import descartes
    motivos = []
    monkeypatch.setattr(descartes, "registrar", lambda *a, **k: motivos.append(a[3]))
    assert modulo.opciones_efectivas(None)["posiciones_equipos"] is False
    assert _procesar(_p(origen)) == []
    assert motivos == ["posición de tracker, va por RC directo"]


@pytest.mark.parametrize("origen, patente, serie, envio", [
    (BEACON_POSICION, BEACON_NOMBRE, BEACON_ID, "VIAJE DE PRUEBAS E INTGRACION"),
    (TRACKER_CON_BEACON, "K393478", "865648069052454", "VIAJE DE PRUEBAS E INTGRACION"),
    (TRACKER_SIN_ENVIO, "K676092", CRUDOS[TRACKER_SIN_ENVIO]["DeviceId"], None),
])
def test_encendido_salen_con_la_patente_de_su_propio_nombre(origen, patente, serie, envio):
    payload = _p(origen)
    (ev,) = _procesar(payload, EQUIPOS)
    assert (ev.chassis_number, ev.serial_number, ev.code, ev.shipment) == (patente, serie, "1", envio)
    assert (ev.latitude, ev.longitude) == (payload["Location"]["Latitude"], payload["Location"]["Longitude"])
    assert ev.date == _utc(payload["EntryTimeUtc"])


def test_encendido_la_misma_lectura_no_sale_dos_veces():
    assert len(_procesar(_p(BEACON_POSICION), EQUIPOS)) == 1
    assert _procesar(_p(BEACON_POSICION), EQUIPOS) == []


def test_encendido_no_toca_los_tramos_de_terceros():
    """El tramo de contenedor sigue con sus reglas (par aprendido del envío)."""
    tramo = _p("2026-10-02:167")
    assert _procesar(tramo, EQUIPOS) == _procesar(_p("2026-10-02:167"))


def test_cableado_el_interruptor_desde_la_config_hasta_la_base(app_tive):
    """Encendido en la configuración de la integración, por el webhook real."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from tests.test_v192_tive import _filas_tive
    assert _post(app_tive, _p(BEACON_POSICION)).status_code == 200
    assert _filas_tive() == [], "apagado no tiene que salir"
    db = get_session("system_config", "global")
    db.query(ProviderConfig).filter_by(provider_name="tive").one().module_options = EQUIPOS
    db.commit()
    db.close()
    otra = _p(BEACON_POSICION)
    otra["EntryTimeEpoch"] += 60000
    assert _post(app_tive, otra).status_code == 200
    assert _filas_tive() == [(BEACON_NOMBRE, "1", "VIAJE DE PRUEBAS E INTGRACION")]


def test_el_panel_lo_muestra_con_su_descripcion(app_tive):
    tive = next(c for c in app_tive.get("/api/config").json() if c["provider_name"] == "TIVE")
    assert tive["module_options"]["posiciones_equipos"] is False
    assert tive["module_options_labels"]["posiciones_equipos"] == (
        "Posiciones de equipos (duplica RC si no se pidió la baja)")
