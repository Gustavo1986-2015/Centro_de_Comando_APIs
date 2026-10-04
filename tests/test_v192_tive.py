"""
v1.9.2 — Tive como integración dedicada.

Todo se prueba contra payloads REALES de Tive (01 y 02/10/2026), recortados
de direcciones y datos de clima: tests/fixtures/tive_crudos_2026-10.jsonl.
Cada registro guarda su origen (archivo:línea de audit/tive_prod/2026-10/).

Lo medido que estos tests fijan:
  · Tramo de contenedor: LocationMethod "container", DeviceName nulo, el
    tracker 867860087520523 nunca vino con nombre.
  · Duplicados de K393478 con AlertId DISTINTO para el mismo hecho.
  · La actualización de J392825 reenviada 9 veces.
  · Reasons distingue apertura, actualización, cierre y puntual.
"""
import copy
import json
import logging
import os
from datetime import datetime, timezone

import pytest

from app.providers.tive import estado, modulo

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tive_crudos_2026-10.jsonl")


def _crudos():
    with open(FIXTURE, encoding="utf-8") as f:
        filas = [json.loads(linea) for linea in f]
    return sorted(filas, key=lambda r: r["timestamp"])


CRUDOS = _crudos()


def _por_origen(origen):
    return copy.deepcopy(next(r["payload"] for r in CRUDOS if r["origen"] == origen))


# Casos reales, por su línea en los crudos.
TRAMO_CONTENEDOR = "2026-10-02:167"          # el UNKNOWN del 02/10 15:44
SHOCK_A, SHOCK_B = "2026-10-02:123", "2026-10-02:124"        # mismo golpe, AlertId distinto
PERIMETRO_A, PERIMETRO_B = "2026-10-02:120", "2026-10-02:121"
TEMP_MIN_APERTURA = "2026-10-02:16"          # K1153407
TEMP_MIN_CIERRE = "2026-10-02:77"            # K1153407, mismo AlertId
J392825_ACTUALIZACIONES = ["2026-10-01:90", "2026-10-01:685", "2026-10-01:997", "2026-10-01:998",
                           "2026-10-01:1004", "2026-10-01:1006", "2026-10-01:1009",
                           "2026-10-01:1011", "2026-10-01:1020"]
CONNECTIVITY = "2026-10-02:104"              # sin coordenadas
MUESTRA_PRUEBA = "2026-10-01:49"             # AccountId -1
POSICION_TRACKER = "2026-10-01:1"

TRACKERS_ON = {"alertas_trackers": True}


@pytest.fixture(autouse=True)
def estado_aislado(tmp_path, monkeypatch):
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado_tive"))
    modulo._metodos_avisados.clear()
    yield
    estado.cerrar_todo()
    modulo._metodos_avisados.clear()


_contador = [0]


def _procesar(payload, opciones=None, ingest_id=None):
    _contador[0] += 1
    return modulo.procesar(payload, "prod", opciones or {}, ingest_id or f"iid-{_contador[0]}")


# ═══════════════════════════════════════════════════════════════════════════
# Los crudos completos, de punta a punta
# ═══════════════════════════════════════════════════════════════════════════

def test_con_los_valores_por_defecto_no_sale_nada_de_estos_crudos():
    """
    Por defecto: posiciones de terceros SÍ, alertas de beacons SÍ, alertas de
    trackers NO. En estos crudos no hay alertas de beacons, y el único tramo de
    contenedor es de un tracker que nunca vino con nombre.
    """
    salida = [e for i, r in enumerate(CRUDOS) for e in _procesar(r["payload"], ingest_id=f"r{i}")]
    assert salida == []


def test_con_alertas_de_trackers_salen_las_20_que_mide_el_informe():
    """
    41 alertas reales → 22 únicas (doble clave) → 20 con coordenadas.
    Las dos Connectivity de K1153407 no traen posición (decisión 6).
    """
    salida = [e for i, r in enumerate(CRUDOS)
              for e in _procesar(r["payload"], TRACKERS_ON, ingest_id=f"r{i}")]
    assert len(salida) == 20
    codigos = sorted((e.chassis_number, e.code) for e in salida)
    assert ("K393478", "ShockEvents") in codigos
    assert codigos.count(("K393478", "ShockEvents")) == 2, "dos golpes distintos, no cuatro"
    assert not any(c.startswith("Connectivity") for _, c in codigos)
    assert sum(1 for p, c in codigos if p == "J392825") == 1, "9 actualizaciones → 1 apertura"


# ═══════════════════════════════════════════════════════════════════════════
# Tramos de terceros y patentes (2.3)
# ═══════════════════════════════════════════════════════════════════════════

def test_el_tramo_real_sin_par_no_se_envia_y_avisa_todo(caplog):
    """El caso real: 867860087520523 nunca vino con nombre. Antes salía UNKNOWN."""
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        assert _procesar(_por_origen(TRAMO_CONTENEDOR)) == []
    aviso = caplog.text
    assert "sin patente resoluble" in aviso
    for dato in ("ID 612780-CONTEN TLLU5171893- MSC- DESTINO BOLIVIA", "TLLU5171893",
                 "867860087520523", "container"):
        assert dato in aviso, f"El aviso no dice {dato}"
    assert "UNKNOWN" not in aviso


def test_el_tramo_usa_el_par_aprendido_de_shipment_deviceid():
    # Cuando el tracker reporte con nombre (por ejemplo, la ráfaga al llegar a
    # puerto), su par queda aprendido aunque esa posición no se envíe.
    posicion = _por_origen(POSICION_TRACKER)
    posicion.update(DeviceId="867860087520523", DeviceName="K1234567")
    assert _procesar(posicion) == [], "la posición del tracker va por RC directo"

    eventos = _procesar(_por_origen(TRAMO_CONTENEDOR))
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev.chassis_number == "K1234567"
    assert ev.code == "1"
    assert ev.shipment == "ID 612780-CONTEN TLLU5171893- MSC- DESTINO BOLIVIA"
    assert ev.serial_number == "867860087520523"
    assert (ev.latitude, ev.longitude) == (3.3092916667, -78.6166416667)
    assert ev.date == datetime(2026, 10, 2, 15, 44, 3, tzinfo=timezone.utc)
    assert ev.speed is None and ev.ignition is None


def test_si_shipment_deviceid_no_tiene_par_usa_los_deviceids_que_si():
    tramo = _por_origen(TRAMO_CONTENEDOR)
    tramo["Shipment"]["DeviceId"] = "000000000000000"
    tramo["Shipment"]["DeviceIds"] = ["000000000000000", "A2A2A20173D4", "867860087520523"]
    estado.aprender_par("prod", "867860087520523", "K1234567")
    assert [e.chassis_number for e in _procesar(tramo)] == ["K1234567"]


def test_un_beacon_nunca_queda_como_patente():
    tramo = _por_origen(TRAMO_CONTENEDOR)
    tramo["Shipment"]["DeviceId"] = "A2A2A20173D4"
    tramo["Shipment"]["DeviceIds"] = ["A2A2A20173D4"]
    assert _procesar(tramo) == []


def test_con_varios_trackers_con_par_sale_uno_por_cada_uno():
    """'Cada elemento de DeviceIds que SÍ tenga par': el contenedor lleva a los dos."""
    tramo = _por_origen(TRAMO_CONTENEDOR)
    tramo["Shipment"]["DeviceId"] = None
    tramo["Shipment"]["DeviceIds"] = ["111111111111111", "A2A2A20173D4", "222222222222222"]
    estado.aprender_par("prod", "111111111111111", "K111")
    estado.aprender_par("prod", "222222222222222", "K222")
    assert sorted(e.chassis_number for e in _procesar(tramo)) == ["K111", "K222"]


def test_un_tramo_aereo_sin_literal_conocido_se_trata_igual_y_se_avisa(caplog):
    """Regla aceptada: DeviceName nulo + coordenadas en Location = tramo de tercero."""
    tramo = _por_origen(TRAMO_CONTENEDOR)
    tramo["Location"]["LocationMethod"] = "air"
    estado.aprender_par("prod", "867860087520523", "K1234567")
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        eventos = _procesar(tramo)
    assert [e.chassis_number for e in eventos] == ["K1234567"]
    assert "LocationMethod nunca visto: 'air'" in caplog.text


def test_los_locationmethod_medidos_no_se_avisan(caplog):
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        for r in CRUDOS:
            _procesar(r["payload"])
    assert "LocationMethod nunca visto" not in caplog.text


def test_la_posicion_de_un_tracker_no_se_envia_pero_ensena_el_par():
    payload = _por_origen(POSICION_TRACKER)
    assert _procesar(payload) == []
    assert estado.nombre_de("prod", payload["DeviceId"]) == payload["DeviceName"]


def test_la_muestra_del_boton_de_prueba_se_descarta_y_no_ensena_nada():
    payload = _por_origen(MUESTRA_PRUEBA)
    assert payload["AccountId"] == -1
    assert _procesar(payload, TRACKERS_ON) == []
    assert estado.nombre_de("prod", "Sample DeviceId") is None


def test_los_pares_sobreviven_a_un_reinicio():
    estado.aprender_par("prod", "867860087520523", "K1234567")
    estado.cerrar_todo()
    assert estado.nombre_de("prod", "867860087520523") == "K1234567"


# ═══════════════════════════════════════════════════════════════════════════
# Alertas (2.5)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("origen, forma", [
    (TEMP_MIN_APERTURA, "apertura"),
    (TEMP_MIN_CIERRE, "cierre"),
    (SHOCK_A, "puntual"),
    ("2026-10-02:55", "puntual"),            # LightChanges
    (J392825_ACTUALIZACIONES[1], "actualizacion"),
])
def test_reasons_distingue_las_cuatro_formas_reales(origen, forma):
    assert modulo.estado_alerta(_por_origen(origen)) == forma


def test_apertura_y_cierre_de_rango():
    apertura = _procesar(_por_origen(TEMP_MIN_APERTURA), TRACKERS_ON)
    cierre = _procesar(_por_origen(TEMP_MIN_CIERRE), TRACKERS_ON)
    assert [e.code for e in apertura] == ["TemperatureMin"]
    assert [e.code for e in cierre] == ["TemperatureMin-FIN"]


def test_la_puntual_nunca_lleva_fin_aunque_venga_cerrada():
    payload = _por_origen(SHOCK_A)
    assert payload["IsClosed"] is True
    assert [e.code for e in _procesar(payload, TRACKERS_ON)] == ["ShockEvents"]


def test_una_forma_de_reasons_desconocida_no_se_adivina(caplog):
    payload = _por_origen(TEMP_MIN_CIERRE)
    for d in payload["Alert"]["Details"]:
        d["Reasons"] = ["Algo"]
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        assert _procesar(payload, TRACKERS_ON) == []
    assert "forma de alerta no reconocida" in caplog.text


def test_la_fecha_es_entrytimeutc_y_no_alertdate():
    """J392825: AlertDate es de 2024; lo que se envía es la lectura de hoy."""
    payload = _por_origen(J392825_ACTUALIZACIONES[0])
    assert payload["AlertDate"].startswith("2024")
    (ev,) = _procesar(payload, TRACKERS_ON)
    assert ev.date == datetime(2026, 10, 1, 15, 27, 25, tzinfo=timezone.utc)


def test_el_envio_es_el_shipmentid_de_la_raiz():
    payload = _por_origen(TEMP_MIN_APERTURA)
    assert payload["Alert"]["ShipmentId"] == "1Q27QDCJWT"
    (ev,) = _procesar(payload, TRACKERS_ON)
    assert ev.shipment == "Pruebas de funcionamiento"


def test_alert_shipmentid_solo_como_ultimo_recurso_y_avisando(caplog):
    payload = _por_origen(TEMP_MIN_APERTURA)
    payload["ShipmentId"] = None
    payload["Shipment"] = None
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        (ev,) = _procesar(payload, TRACKERS_ON)
    assert ev.shipment == "1Q27QDCJWT"
    assert "código público de Tive" in caplog.text


def test_la_alerta_sin_posicion_no_se_envia():
    """Decisión 6: Connectivity de K1153407, sin coordenadas."""
    assert _procesar(_por_origen(CONNECTIVITY), TRACKERS_ON) == []


def test_alerta_con_datos_de_sensores():
    (ev,) = _procesar(_por_origen(TEMP_MIN_APERTURA), TRACKERS_ON)
    assert ev.chassis_number == "K1153407"
    assert ev.temperature is not None and ev.humidity is not None
    assert ev.serial_number == "868977084374560"


# ═══════════════════════════════════════════════════════════════════════════
# Interruptores (2.4)
# ═══════════════════════════════════════════════════════════════════════════

def test_los_valores_por_defecto():
    assert modulo.opciones_efectivas(None) == {
        "posiciones_terceros": True, "alertas_beacons": True, "alertas_trackers": False,
    }


def test_alertas_de_tracker_apagadas_por_defecto():
    assert _procesar(_por_origen(TEMP_MIN_APERTURA)) == []


def _alerta_de_beacon():
    """
    Nunca se vio una alerta de beacon real (pendiente 5). Se arma desde una
    alerta real cambiando el equipo por el beacon medido, con el par del
    tracker del envío aprendido.
    """
    payload = _por_origen(TEMP_MIN_APERTURA)
    payload["DeviceId"] = "A2A2A20173D4"
    payload["DeviceName"] = None
    payload["Alert"]["DeviceId"] = "A2A2A20173D4"
    payload["Alert"]["DeviceName"] = None
    payload["Shipment"] = {"Id": payload["ShipmentId"], "DeviceId": "868977084374560",
                           "DeviceIds": ["868977084374560", "A2A2A20173D4"]}
    estado.aprender_par("prod", "868977084374560", "K1153407")
    return payload


def test_la_alerta_de_beacon_sale_por_defecto_con_la_patente_del_tracker():
    (ev,) = _procesar(_alerta_de_beacon())
    assert ev.chassis_number == "K1153407"
    assert ev.code == "TemperatureMin"


def test_el_interruptor_de_beacons_la_apaga():
    assert _procesar(_alerta_de_beacon(), {"alertas_beacons": False}) == []


def test_el_interruptor_de_posiciones_de_terceros_las_apaga():
    estado.aprender_par("prod", "867860087520523", "K1234567")
    assert _procesar(_por_origen(TRAMO_CONTENEDOR), {"posiciones_terceros": False}) == []
    assert len(_procesar(_por_origen(TRAMO_CONTENEDOR))) == 1


def test_un_interruptor_que_no_es_booleano_no_cambia_el_valor_por_defecto():
    assert modulo.opciones_efectivas({"alertas_trackers": "si"})["alertas_trackers"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Duplicados (2.6)
# ═══════════════════════════════════════════════════════════════════════════

def test_los_pares_reales_de_k393478_tienen_alertid_distinto_y_mismo_envio():
    a, b = _por_origen(SHOCK_A), _por_origen(SHOCK_B)
    assert a["Alert"]["AlertId"] != b["Alert"]["AlertId"]
    assert a["ShipmentId"] == b["ShipmentId"] == "VIAJE DE PRUEBAS DE ALERTAS"
    assert a["EntryTimeEpoch"] == b["EntryTimeEpoch"]


@pytest.mark.parametrize("par", [(SHOCK_A, SHOCK_B), (PERIMETRO_A, PERIMETRO_B)])
def test_el_mismo_hecho_con_alertid_distinto_sale_una_sola_vez(par):
    """Los dos casos que llegaron duplicados a RC el 02/10."""
    primero = _procesar(_por_origen(par[0]), TRACKERS_ON)
    segundo = _procesar(_por_origen(par[1]), TRACKERS_ON)
    assert len(primero) == 1 and segundo == []


def test_la_misma_alerta_reenviada_sale_una_sola_vez():
    assert len(_procesar(_por_origen(SHOCK_A), TRACKERS_ON)) == 1
    assert _procesar(_por_origen(SHOCK_A), TRACKERS_ON) == []


def test_las_actualizaciones_de_j392825_salen_una_vez_como_apertura(caplog):
    """Decisión 2: la primera, sin apertura registrada, se envía como apertura."""
    salida = []
    with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
        for origen in J392825_ACTUALIZACIONES:
            salida += _procesar(_por_origen(origen), TRACKERS_ON)
    assert [e.code for e in salida] == ["TemperatureMax"]
    assert "se envía como apertura" in caplog.text
    assert caplog.text.count("actualización de una alerta abierta que ya se envió") == 8


def test_la_actualizacion_de_una_apertura_ya_enviada_se_descarta():
    apertura = _por_origen(J392825_ACTUALIZACIONES[0])
    for d in apertura["Alert"]["Details"]:
        d["Reasons"] = ["Created", "Latest"]
    apertura["Alert"]["Details"] = apertura["Alert"]["Details"][:1]
    assert len(_procesar(apertura, TRACKERS_ON)) == 1
    assert _procesar(_por_origen(J392825_ACTUALIZACIONES[1]), TRACKERS_ON) == []


def test_el_cierre_que_llega_antes_que_la_apertura_no_la_tapa():
    """Sin asumir orden de llegada."""
    cierre = _procesar(_por_origen(TEMP_MIN_CIERRE), TRACKERS_ON)
    apertura = _procesar(_por_origen(TEMP_MIN_APERTURA), TRACKERS_ON)
    assert [e.code for e in cierre] == ["TemperatureMin-FIN"]
    assert [e.code for e in apertura] == ["TemperatureMin"]


def test_la_posicion_duplicada_del_tramo_sale_una_vez():
    estado.aprender_par("prod", "867860087520523", "K1234567")
    assert len(_procesar(_por_origen(TRAMO_CONTENEDOR))) == 1
    assert _procesar(_por_origen(TRAMO_CONTENEDOR)) == []


def test_la_deduplicacion_sobrevive_a_un_reinicio():
    assert len(_procesar(_por_origen(SHOCK_A), TRACKERS_ON)) == 1
    estado.cerrar_todo()
    assert _procesar(_por_origen(SHOCK_B), TRACKERS_ON) == []


def test_reprocesar_la_misma_recepcion_no_es_un_duplicado():
    """
    La red de seguridad reprocesa con el MISMO ingest_id. Si eso contara como
    duplicado, un evento cuyo INSERT falló se perdería para siempre.
    """
    assert len(_procesar(_por_origen(SHOCK_A), TRACKERS_ON, ingest_id="iid-X")) == 1
    assert len(_procesar(_por_origen(SHOCK_A), TRACKERS_ON, ingest_id="iid-X")) == 1
    assert _procesar(_por_origen(SHOCK_A), TRACKERS_ON, ingest_id="iid-Y") == []


def test_el_reintento_de_un_duplicado_sigue_siendo_duplicado():
    assert len(_procesar(_por_origen(SHOCK_A), TRACKERS_ON, ingest_id="iid-A")) == 1
    assert _procesar(_por_origen(SHOCK_B), TRACKERS_ON, ingest_id="iid-B") == []
    assert _procesar(_por_origen(SHOCK_B), TRACKERS_ON, ingest_id="iid-B") == []


def test_un_evento_descartado_por_el_contrato_no_cuenta_como_visto():
    """Validación antes que deduplicación: el descarte no consume la clave."""
    payload = _por_origen(SHOCK_A)
    sin_fecha = copy.deepcopy(payload)
    sin_fecha["EntryTimeUtc"] = None
    sin_fecha["EntryTimeEpoch"] = payload["EntryTimeEpoch"]
    sin_fecha["Location"]["Latitude"] = None
    assert _procesar(sin_fecha, TRACKERS_ON) == []
    assert len(_procesar(payload, TRACKERS_ON)) == 1


def test_cada_descarte_dice_motivo_e_identidad(caplog):
    with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
        for r in CRUDOS:
            _procesar(r["payload"])
    lineas = [l for l in caplog.text.splitlines() if "Descartado" in l]
    assert lineas
    for linea in lineas:
        assert "NO se envía a RC:" in linea and "| equipo=" in linea, linea


# ═══════════════════════════════════════════════════════════════════════════
# Cableado: endpoint, red de seguridad, registro, panel
# ═══════════════════════════════════════════════════════════════════════════

SECRETO = "secreto-de-tive"


def _firmar(cuerpo: bytes) -> dict:
    import base64
    import hashlib
    import hmac
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    digest = hmac.new(SECRETO.encode(), f"{ts}.".encode() + cuerpo, hashlib.sha256).digest()
    return {"content-type": "application/json",
            "x-tive-signature": f"t={ts},v1={base64.b64encode(digest).decode()}"}


@pytest.fixture
def app_tive(tmp_path, monkeypatch):
    """
    El router real del webhook dinámico con la integración 'tive' configurada
    como en la base local: HMAC, sin esquema del Studio útil.
    """
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import database
    from app.core import crypto, rate_limit, safety_net

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASTER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()
    monkeypatch.setattr(safety_net, "DIRECTORIO_BASE", str(tmp_path / "red"))
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()

    from app.api.routers import admin_config, dynamic_webhook
    from app.core.auth import verify_dashboard_auth
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent

    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="tive", env="prod", provider_type="push", is_active=True,
        use_mock=True, webhook_auth_secret_enc=encrypt(SECRETO),
        webhook_auth_header="x-tive-signature",
        webhook_auth_config={"modo": "hmac", "preset": "tive"},
        # El esquema viejo del Studio queda guardado y no tiene ningún efecto.
        mapping_schema={"base_mapping": {"chassis_number": "DeviceName || EntityName"}},
        rc_user="u", rc_password_enc=encrypt("p"), enable_state_dedup=False,
    ))
    db.add(ProviderConfig(provider_name="protrack", env="test", provider_type="pull",
                          mapping_schema={"base_mapping": {}}))
    db.commit()
    db.close()
    NormalizedRCEvent.metadata.create_all(bind=database.get_engine("tive", "prod"))
    database.check_and_migrate_provider_db("tive", "prod")

    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    app.include_router(admin_config.router)
    app.dependency_overrides[verify_dashboard_auth] = lambda: None
    yield TestClient(app)

    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()


def _filas_tive():
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("tive", "prod")
    try:
        return [(f.chassis_number, f.code, f.shipment) for f in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()


def _post(cliente, payload):
    cuerpo = json.dumps(payload).encode()
    return cliente.post("/webhook/dynamic/tive?env=prod", content=cuerpo, headers=_firmar(cuerpo))


def test_la_url_de_siempre_deriva_al_modulo(app_tive):
    """Cableado: el endpoint genérico usa el módulo, no el esquema del Studio."""
    r = _post(app_tive, _por_origen(POSICION_TRACKER))
    assert r.status_code == 200, r.text
    assert _filas_tive() == [], "Una posición de tracker entró por el esquema viejo del Studio"

    posicion = _por_origen(POSICION_TRACKER)
    posicion.update(DeviceId="867860087520523", DeviceName="K1234567")
    _post(app_tive, posicion)
    r = _post(app_tive, _por_origen(TRAMO_CONTENEDOR))
    assert r.status_code == 200 and r.json()["events_count"] == 1
    assert _filas_tive() == [("K1234567", "1", "ID 612780-CONTEN TLLU5171893- MSC- DESTINO BOLIVIA")]


def test_dos_trackers_con_par_dejan_dos_filas_con_ingest_id_distintos(app_tive):
    """
    Por el camino real de guardado, no llamando al módulo directo: endpoint,
    _save_dynamic_events y la base. Un tramo con dos trackers con par sale
    como dos eventos, y cada uno necesita su propio ingest_id: el índice único
    sobre esa columna haría fallar el INSERT entero si se repitiera, y el
    evento terminaría en la red de seguridad en vez de en la cola.
    """
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent

    # Los pares se aprenden también por el endpoint: dos posiciones de tracker.
    for device_id, nombre in (("111111111111111", "K111"), ("222222222222222", "K222")):
        posicion = _por_origen(POSICION_TRACKER)
        posicion.update(DeviceId=device_id, DeviceName=nombre)
        assert _post(app_tive, posicion).json()["events_count"] == 0

    tramo = _por_origen(TRAMO_CONTENEDOR)
    tramo["Shipment"]["DeviceId"] = None
    tramo["Shipment"]["DeviceIds"] = ["111111111111111", "A2A2A20173D4", "222222222222222"]
    r = _post(app_tive, tramo)
    assert r.status_code == 200 and r.json()["events_count"] == 2, r.text

    db = get_session("tive", "prod")
    try:
        filas = [(f.chassis_number, f.ingest_id) for f in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()
    assert sorted(p for p, _ in filas) == ["K111", "K222"], filas
    ingest_ids = [i for _, i in filas]
    assert all(ingest_ids) and len(set(ingest_ids)) == 2, f"ingest_id repetidos o vacíos: {ingest_ids}"


def test_el_endpoint_usa_los_interruptores_guardados(app_tive):
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    assert _post(app_tive, _por_origen(SHOCK_A)).json()["events_count"] == 0
    db = get_session("system_config", "global")
    conf = db.query(ProviderConfig).filter_by(provider_name="tive").first()
    conf.module_options = {"alertas_trackers": True}
    db.commit()
    db.close()
    assert _post(app_tive, _por_origen(SHOCK_B)).json()["events_count"] == 1
    assert _filas_tive() == [("K393478", "ShockEvents", "VIAJE DE PRUEBAS DE ALERTAS")]


def test_un_fallo_del_modulo_manda_el_payload_a_la_red_de_seguridad(app_tive, monkeypatch):
    import time as _t

    from app.core import safety_net

    def _falla(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(modulo, "procesar", _falla)
    r = _post(app_tive, _por_origen(SHOCK_A))
    assert r.status_code == 200
    assert r.json()["note"] == "resguardado para reintento"
    _t.sleep(0.4)
    assert len(safety_net.pendientes_reales("tive", "prod")) == 1


def test_el_reintentador_reprocesa_con_el_modulo_sin_duplicar(app_tive, monkeypatch):
    """
    Un INSERT que falla deja el payload en la red de seguridad con su
    ingest_id. El reintentador lo pasa por el módulo con ese mismo id: no se
    toma como duplicado de sí mismo, y reintentar dos veces no duplica.
    """
    import asyncio
    import time as _t

    from app.api.routers import dynamic_webhook
    from app.api.routers.schmitz import _persistir_cualquier_integracion
    from app.core import safety_net
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.query(ProviderConfig).filter_by(provider_name="tive").first().module_options = {"alertas_trackers": True}
    db.commit()
    db.close()

    original = dynamic_webhook._save_dynamic_events

    def _falla(*a, **k):
        raise Exception("(sqlite3.OperationalError) database is locked")

    monkeypatch.setattr(dynamic_webhook, "_save_dynamic_events", _falla)
    r = _post(app_tive, _por_origen(SHOCK_A))
    assert r.json()["note"] == "resguardado para reintento"
    monkeypatch.setattr(dynamic_webhook, "_save_dynamic_events", original)
    _t.sleep(0.4)
    (pendiente,) = safety_net.pendientes_reales("tive", "prod")

    asyncio.run(safety_net.reintentar_pendientes("tive", "prod", _persistir_cualquier_integracion))
    assert _filas_tive() == [("K393478", "ShockEvents", "VIAJE DE PRUEBAS DE ALERTAS")], (
        "El reintentador descartó el evento como duplicado de sí mismo"
    )
    assert safety_net.estado("tive", "prod", usar_cache=False)["pendientes"] == 0

    # Idempotente: reintentar lo mismo no duplica.
    asyncio.run(dynamic_webhook.persistir_desde_red_de_seguridad(
        "tive", "prod", [(pendiente["payload"], pendiente["ingest_id"])]))
    assert len(_filas_tive()) == 1, "El reintento duplicó el evento"


def test_el_studio_no_lista_a_tive_y_no_deja_guardarle_un_esquema(app_tive):
    proveedores = app_tive.get("/api/config/providers").json()
    tive = next(p for p in proveedores if p["provider_name"] == "tive")
    otro = next(p for p in proveedores if p["provider_name"] == "protrack")
    assert tive["modulo_dedicado"] is True and otro["modulo_dedicado"] is False

    r = app_tive.post("/api/config/tive/prod/mapping", json={"mapping": {"base_mapping": {}}})
    assert r.json()["status"] == "error"


def test_los_interruptores_se_ven_y_se_guardan_desde_el_panel(app_tive):
    configs = app_tive.get("/api/config").json()
    tive = next(c for c in configs if c["provider_name"] == "TIVE")
    assert tive["modulo_dedicado"] is True
    assert tive["module_options"] == {"posiciones_terceros": True, "alertas_beacons": True,
                                      "alertas_trackers": False}
    assert set(tive["module_options_labels"]) == set(tive["module_options"])
    protrack = next(c for c in configs if c["provider_name"] == "PROTRACK")
    assert protrack["modulo_dedicado"] is False and "module_options" not in protrack

    def _actualizacion(c, opciones):
        return {"id": c["id"], "is_active": c["is_active"], "rc_user": c["rc_user"] or "",
                "use_mock": c["use_mock"], "purge_interval_min": 15, "run_interval_sec": 5,
                "queue_backend": "sqlite", "module_options": opciones}

    r = app_tive.post("/api/config", json=[_actualizacion(tive, {
        "posiciones_terceros": True, "alertas_beacons": False, "alertas_trackers": True})])
    assert r.status_code == 200, r.text
    tive = next(c for c in app_tive.get("/api/config").json() if c["provider_name"] == "TIVE")
    assert tive["module_options"] == {"posiciones_terceros": True, "alertas_beacons": False,
                                      "alertas_trackers": True}

    r = app_tive.post("/api/config", json=[_actualizacion(tive, {"inventado": True})])
    assert r.status_code == 400
    r = app_tive.post("/api/config", json=[_actualizacion(tive, {"alertas_trackers": "si"})])
    assert r.status_code == 400
    r = app_tive.post("/api/config", json=[_actualizacion(protrack, {"alertas_trackers": True})])
    assert r.status_code == 400


def test_el_registro_solo_conoce_a_tive():
    from app.providers import registry
    assert registry.modulo_dedicado("TIVE") is modulo
    assert registry.modulo_dedicado("protrack") is None
    assert registry.modulo_dedicado("schmitz") is None
