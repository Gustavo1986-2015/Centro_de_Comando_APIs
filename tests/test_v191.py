"""
v1.9.1 — Reglas anidadas, filtro de admisión e integridad del payload a RC.

Todo sale de la prueba real de Tive del 01/10/2026:

  · El webhook trajo 171 eventos y solo 6 tenían envío: el filtro de admisión
    deja entrar solo lo que la integración necesita.
  · Dos eventos eran los ejemplos del botón de prueba de Tive (cuenta -1,
    equipo SAMPLEDEVICEID): sin filtro, llegarían a RC como un camión real.
  · El envío de las alertas viene anidado en Alert.ShipmentId, y las reglas
    solo leían el primer nivel (deuda B1).
  · El hub mandaba a RC "ignition: false" para trackers que no miden
    ignición. El contrato D-TI-15 v14 la define OPCIONAL y pide "en blanco o
    0" cuando no hay dato: "false" afirma algo que nadie midió.
  · El botón "JSON RC" del panel no mostraba lo que se envía de verdad.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import admision
from app.core.dynamic_mapper import DynamicMapper
from app.services.rc_soap import construir_evento_rc

# ═══════════════════════════════════════════════════════════════════════════
# Reglas con campos anidados (deuda B1)
# ═══════════════════════════════════════════════════════════════════════════

ALERTA_DE_ENVIO = {
    "AccountId": 10471, "DeviceName": "Q92951", "AlertType": "TemperatureMax",
    "ShipmentId": None,
    "Alert": {"ShipmentId": "4600410383", "AlertType": "TemperatureMax"},
}


def test_una_regla_lee_un_campo_anidado():
    assert DynamicMapper._evaluate_rule(ALERTA_DE_ENVIO, "Alert.ShipmentId", "exists", "")
    assert DynamicMapper._evaluate_rule(ALERTA_DE_ENVIO, "Alert.ShipmentId", "eq", "4600410383")


def test_una_regla_acepta_alternativas():
    """El envío puede venir en la raíz, en Shipment o dentro de la alerta."""
    ruta = "ShipmentId || Shipment.Id || Alert.ShipmentId"
    assert DynamicMapper._evaluate_rule(ALERTA_DE_ENVIO, ruta, "exists", "")
    sin_envio = {"AccountId": 1, "ShipmentId": None, "Alert": {"ShipmentId": None}}
    assert not DynamicMapper._evaluate_rule(sin_envio, ruta, "exists", "")


def test_antes_una_condicion_anidada_fallaba_en_silencio():
    """
    Documenta el defecto: con la lectura de primer nivel, 'Alert.ShipmentId'
    se buscaba como clave literal de la raíz, no existía, y la condición daba
    falso aunque el dato estuviera.
    """
    assert ALERTA_DE_ENVIO.get("Alert.ShipmentId") is None
    assert DynamicMapper._evaluate_rule(ALERTA_DE_ENVIO, "Alert.ShipmentId", "exists", "")


@pytest.mark.parametrize("payload, campo, valor, esperado", [
    # Las cuatro reglas reales de Protrack en producción (recuperar_protrack.yaml)
    ({"doorstatus": "1", "accstatus": "0"}, "doorstatus", "1", True),
    ({"doorstatus": "0", "accstatus": "1"}, "accstatus", "1", True),
    ({"doorstatus": "0", "accstatus": "0"}, "accstatus", "0", True),
    ({"doorstatus": "0", "accstatus": "1"}, "doorstatus", "0", True),
    ({"doorstatus": "1", "accstatus": "1"}, "doorstatus", "0", False),
    ({"accstatus": "1"}, "doorstatus", "1", False),
])
def test_las_reglas_de_protrack_dan_lo_mismo_que_siempre(payload, campo, valor, esperado):
    """Producción: Protrack usa claves de la raíz, que se leen igual que antes."""
    assert DynamicMapper._evaluate_rule(payload, campo, "eq", valor) is esperado


def test_una_clave_de_la_raiz_con_punto_se_sigue_leyendo_literal():
    """
    Compatibilidad: si existe TAL CUAL en la raíz, gana sobre la ruta. Un
    proveedor con una clave llamada 'gps.speed' no puede cambiar de resultado.
    """
    payload = {"gps.speed": "80", "gps": {"speed": "10"}}
    assert DynamicMapper._evaluate_rule(payload, "gps.speed", "eq", "80")


# ═══════════════════════════════════════════════════════════════════════════
# Filtro de admisión
# ═══════════════════════════════════════════════════════════════════════════

FILTRO_TIVE = {"admision": [
    {"field": "ShipmentId || Shipment.Id || Alert.ShipmentId", "operator": "exists",
     "label": "solo eventos con envío"},
    {"field": "AccountId", "operator": "neq", "value": "-1",
     "label": "descartar muestras del botón de prueba"},
]}


@pytest.fixture(autouse=True)
def contadores_limpios():
    admision.reset()
    yield
    admision.reset()


def test_sin_filtro_entra_todo():
    """Compatibilidad: Protrack, Schmitz y cualquier integración sin filtro."""
    assert admision.evaluar({"lo": "que sea"}, {}) is None
    assert admision.evaluar({"lo": "que sea"}, None) is None
    assert admision.evaluar({"lo": "que sea"}, {"base_mapping": {}}) is None


@pytest.mark.parametrize("nombre, payload, motivo", [
    ("posición en envío (raíz)",
     {"AccountId": 10471, "ShipmentId": "4600410383"}, None),
    ("posición en envío (objeto Shipment)",
     {"AccountId": 10471, "ShipmentId": None, "Shipment": {"Id": "NWMX-085"}}, None),
    ("alerta de envío (anidada)", ALERTA_DE_ENVIO, None),
    ("equipo sin envío",
     {"AccountId": 9831, "DeviceName": "Q48548", "ShipmentId": None, "Shipment": None},
     "solo eventos con envío"),
    ("alerta de equipo sin envío (caso real J392825)",
     {"AccountId": 1218, "DeviceName": "J392825", "AlertType": "TemperatureMax",
      "ShipmentId": None, "Alert": {"ShipmentId": None}},
     "solo eventos con envío"),
    ("muestra del botón de prueba (caso real)",
     {"AccountId": -1, "DeviceName": "SAMPLEDEVICEID", "ShipmentId": "Shipment Id"},
     "descartar muestras del botón de prueba"),
])
def test_el_filtro_de_tive_con_casos_reales(nombre, payload, motivo):
    assert admision.evaluar(payload, FILTRO_TIVE) == motivo, nombre


@pytest.mark.parametrize("condicion, fragmento", [
    ({"field": "", "operator": "exists"}, "falta el campo"),
    ({"field": "X", "operator": "contiene"}, "operador 'contiene' desconocido"),
    ("no es un objeto", "no es un objeto"),
])
def test_una_condicion_rota_descarta_y_dice_por_que(condicion, fragmento):
    """
    Dejar pasar todo ante una configuración inválida sería el fallo silencioso
    que el filtro existe para evitar.
    """
    motivo = admision.evaluar({"X": 1}, {"admision": [condicion]})
    assert motivo and fragmento in motivo


def test_cada_descarte_queda_contado():
    for _ in range(5):
        admision.registrar_descarte("tive", "prod", "solo eventos con envío")
    admision.registrar_descarte("tive", "prod", "descartar muestras del botón de prueba")

    totales = {r["motivo"]: r["total"] for r in admision.resumen()}
    assert totales == {"solo eventos con envío": 5, "descartar muestras del botón de prueba": 1}


def test_el_primer_descarte_se_registra_enseguida(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="app.core.admision"):
        admision.registrar_descarte("tive", "prod", "solo eventos con envío")
    assert "solo eventos con envío" in caplog.text


# ─── Cableado: que el filtro se aplique de verdad en PUSH y PULL ────────────

@pytest.fixture
def webhook_tive(tmp_path, monkeypatch):
    """
    Router del webhook dinámico con una integración del Studio filtrada, sin firma.

    Desde la v1.9.2 Tive tiene módulo dedicado; el filtro de admisión sigue
    siendo una capacidad genérica del Integration Studio, así que se prueba con
    un proveedor genérico configurado como estaba Tive.
    """
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import database
    from app.core import crypto, rate_limit

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASTER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()

    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent

    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="studio", env="prod", provider_type="push", is_active=True,
        use_mock=True, webhook_auth_secret_enc=encrypt("CLAVE"),
        mapping_schema={
            "base_mapping": {"chassis_number": "DeviceName", "latitude": "Location.Latitude",
                             "longitude": "Location.Longitude", "date": "EntryTimeUtc",
                             "shipment": "ShipmentId || Shipment.Id || Alert.ShipmentId"},
            **FILTRO_TIVE,
        },
        rc_user="u", rc_password_enc=encrypt("p"),
    ))
    db.commit()
    db.close()
    NormalizedRCEvent.metadata.create_all(bind=database.get_engine("studio", "prod"))
    database.check_and_migrate_provider_db("studio", "prod")

    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    yield TestClient(app)

    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()


def _filas_tive():
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("studio", "prod")
    try:
        return [(f.chassis_number, f.shipment) for f in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()


BASE = {"EntryTimeUtc": "2026-10-01T15:29:39Z", "Location": {"Latitude": 20.6, "Longitude": -100.1}}


def test_el_webhook_aplica_el_filtro(webhook_tive):
    h = {"x-api-key": "CLAVE"}
    con_envio = {**BASE, "AccountId": 10471, "DeviceName": "Q92951", "ShipmentId": "4600410383"}
    sin_envio = {**BASE, "AccountId": 9831, "DeviceName": "Q48548", "ShipmentId": None}
    muestra = {**BASE, "AccountId": -1, "DeviceName": "SAMPLEDEVICEID", "ShipmentId": "Shipment Id"}

    r_ok = webhook_tive.post("/webhook/dynamic/studio", json=con_envio, headers=h)
    r_sin = webhook_tive.post("/webhook/dynamic/studio", json=sin_envio, headers=h)
    r_muestra = webhook_tive.post("/webhook/dynamic/studio", json=muestra, headers=h)

    # Los descartes responden éxito: el proveedor no tiene que reintentarlos.
    assert r_ok.status_code == r_sin.status_code == r_muestra.status_code == 200
    assert r_sin.json()["motivo"] == "solo eventos con envío"
    assert r_muestra.json()["motivo"] == "descartar muestras del botón de prueba"

    assert _filas_tive() == [("Q92951", "4600410383")], (
        "Entró algo que el filtro tenía que descartar"
    )


def test_el_pull_aplica_el_filtro(tmp_path, monkeypatch):
    """Transversal: el mismo filtro corta también en el camino PULL."""
    import asyncio

    from app import database
    from app.models.db_models import NormalizedRCEvent
    from app.worker import pull_engine

    monkeypatch.chdir(tmp_path)
    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    try:
        NormalizedRCEvent.metadata.create_all(bind=database.get_engine("prov", "test"))
        database.check_and_migrate_provider_db("prov", "test")

        esquema = {"base_mapping": {"chassis_number": "placa", "latitude": "lat",
                                    "longitude": "lon", "date": "fecha"},
                   "admision": [{"field": "placa", "operator": "neq", "value": "PRUEBA",
                                 "label": "sin equipos de prueba"}]}
        items = [{"placa": "AB123", "lat": -34.6, "lon": -58.4, "fecha": "2026-10-01T15:00:00Z"},
                 {"placa": "PRUEBA", "lat": -34.6, "lon": -58.4, "fecha": "2026-10-01T15:00:00Z"}]

        asyncio.run(pull_engine.process_and_enqueue("prov", "test", items, esquema,
                                                     enable_state_dedup=False))

        db = database.get_session("prov", "test")
        placas = [f.chassis_number for f in db.query(NormalizedRCEvent).all()]
        db.close()
        assert placas == ["AB123"]
        assert {r["motivo"] for r in admision.resumen()} == {"sin equipos de prueba"}
    finally:
        database._engines.clear()
        database._sessions.clear()
        database._engines.update(engines)
        database._sessions.update(sessions)


# ═══════════════════════════════════════════════════════════════════════════
# Integridad del payload a RC — contrato D-TI-15 v14
# ═══════════════════════════════════════════════════════════════════════════

def _evento(**campos):
    base = dict(chassis_number="Q92951", code="1", date=datetime(2026, 10, 1, 15, 29, 39),
                course=None, ignition=None, latitude=20.6, longitude=-100.1, speed=None,
                altitude=None, battery=None, humidity=None, odometer=None,
                temperature=None, serial_number=None, shipment=None)
    base.update(campos)
    return SimpleNamespace(**base)


def test_la_ignicion_desconocida_no_se_envia():
    """
    El caso de Tive: no mide ignición. Antes salía "false", que afirma que el
    vehículo está apagado.
    """
    assert "ignition" not in construir_evento_rc(_evento(ignition=None))


@pytest.mark.parametrize("valor, esperado", [(True, "true"), (False, "false")])
def test_la_ignicion_conocida_se_envia_igual_que_siempre(valor, esperado):
    """Producción: Schmitz y Protrack reportan ignición y no cambia nada."""
    assert construir_evento_rc(_evento(ignition=valor))["ignition"] == esperado


def test_los_obligatorios_siguen_yendo_con_cero_si_faltan():
    """
    El contrato marca obligatorios speed, latitude y longitude, y pide "en
    blanco o 0" cuando no hay dato. Omitirlos haría que RC rechace el evento.
    """
    ev = construir_evento_rc(_evento(speed=None, latitude=None, longitude=None))
    assert ev["speed"] == "0"
    assert ev["latitude"] == "0"
    assert ev["longitude"] == "0"


def test_los_obligatorios_estan_siempre():
    ev = construir_evento_rc(_evento())
    for campo in ("asset", "code", "date", "latitude", "longitude", "speed"):
        assert campo in ev, f"Falta el obligatorio {campo}"


def test_la_fecha_va_sin_z():
    """El contrato pide YYYY-MM-DDTHH:MM:SS; RC rechaza el lote si no la lee."""
    assert construir_evento_rc(_evento())["date"] == "2026-10-01T15:29:39"


def test_el_json_rc_del_panel_es_lo_que_se_envia():
    """
    Antes el panel armaba su propia versión: odómetro, altitud y humedad en 0
    cuando se omitían, fecha con Z, ignición "false" y campos de vehículo que
    nunca se mandan. Ahora delega en la misma función que el envío.
    """
    from app.api.routers.dashboard import _formato_rc
    from app.models.db_models import NormalizedRCEvent

    fila = NormalizedRCEvent(
        chassis_number="Q92951", code="1", date=datetime(2026, 10, 1, 15, 29, 39),
        latitude=20.6, longitude=-100.1, speed=0.0, ignition=None, battery=92,
        temperature=20.69, serial_number="869267076302806", shipment="4600410383",
    )
    assert _formato_rc(fila) == construir_evento_rc(fila)

    panel = _formato_rc(fila)
    for inventado in ("ignition", "odometer", "altitude", "humidity",
                      "vehicleType", "vehicleBrand", "vehicleModel"):
        assert inventado not in panel, f"El panel muestra {inventado}, que no se envía"
    assert not panel["date"].endswith("Z")


def test_el_endpoint_del_panel_usa_la_funcion_del_envio(webhook_tive):
    """
    Cableado, no solo lógica. El test de arriba prueba _formato_rc, pero no
    que el panel la llame: con el panel armando su propio JSON a mano, ese
    test seguía en verde. Este pide los datos al endpoint real del panel y
    compara contra lo que se enviaría a RC.
    """
    import asyncio

    from app.api.routers.dashboard import get_stats_data
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent

    webhook_tive.post("/webhook/dynamic/studio", headers={"x-api-key": "CLAVE"},
                      json={**BASE, "AccountId": 10471, "DeviceName": "Q92951",
                            "ShipmentId": "4600410383"})

    datos = asyncio.run(get_stats_data())
    evento_panel = next(e for e in datos["recent"] if e.get("rc_format", {}).get("asset") == "Q92951")

    db = get_session("studio", "prod")
    fila = db.query(NormalizedRCEvent).filter_by(chassis_number="Q92951").first()
    db.close()

    assert evento_panel["rc_format"] == construir_evento_rc(fila), (
        "El JSON RC del panel no es lo que se envía a RC"
    )
    assert "ignition" not in evento_panel["rc_format"]
    assert evento_panel["ignition"] == "N/A"


def test_el_envio_real_usa_la_funcion_unica():
    """Cableado: que _send_batch_sync no tenga su propia copia."""
    import inspect

    from app.services import rc_soap
    # El método exacto que manda a RC, no el módulo entero: buscar en todo el
    # archivo daba verde aunque el método tuviera su propia copia, porque la
    # función compartida aparece igual en otra parte.
    fuente = inspect.getsource(rc_soap.RCSOAPClient._send_batch_sync)
    assert "construir_evento_rc(event)" in fuente
    assert "'ignition': \"true\" if event.ignition else \"false\"" not in fuente


# ═══════════════════════════════════════════════════════════════════════════
# Rastro de los 404
# ═══════════════════════════════════════════════════════════════════════════

def test_un_proveedor_desconocido_deja_rastro(webhook_tive, monkeypatch):
    """
    En la prueba de Tive, los 404 solo se veían como líneas de acceso y hubo
    que adivinar la URL. Ahora queda registrado el nombre exacto que llegó.
    """
    from app.api.routers import dynamic_webhook

    vistos = []
    monkeypatch.setattr(dynamic_webhook, "registrar_rechazo",
                        lambda p, e, motivo, detalle="": vistos.append(motivo))
    r = webhook_tive.post("/webhook/dynamic/inexistente", json={"a": 1})
    assert r.status_code == 404
    assert vistos and "inexistente" in vistos[0]


# ═══════════════════════════════════════════════════════════════════════════
# Códigos al estilo Schmitz: un evento por pulso, el literal como código
#
# RC recibe el código tal cual y quien lo recibe le da significado; el hub no
# traduce. Schmitz manda "DoorAlarm" como código de una alarma, sin una
# posición repetida al lado. Tive tiene que hacer lo mismo con su AlertType.
# ═══════════════════════════════════════════════════════════════════════════

def _esquema_tive(fire_when):
    return {"base_mapping": {"chassis_number": "DeviceName", "latitude": "Location.Latitude",
                             "longitude": "Location.Longitude", "date": "EntryTimeUtc"},
            "trigger_rules": [{"id": "r1", "field": "AlertType || Alert.AlertType",
                               "operator": "exists", "value": "",
                               "rc_code": "=AlertType || Alert.AlertType", "enabled": True,
                               "event_type": "momentary"}],
            "default_rule": {"enabled": True, "rc_code": "1", "fire_when": fire_when}}


TELEMETRIA = {**BASE, "DeviceName": "Q92951"}
ALERTA = {**BASE, "DeviceName": "Q92951", "AlertType": "TemperatureMax",
          "Alert": {"AlertType": "TemperatureMax"}}


def _codigos(payload, esquema):
    return [e.code for e in DynamicMapper.map_payload_multi(payload, esquema, "tive", "prod",
                                                            False, False)]


def test_una_alerta_sale_con_su_literal_y_sin_posicion_repetida():
    assert _codigos(ALERTA, _esquema_tive("no_rule_matched")) == ["TemperatureMax"]


def test_la_telemetria_sale_como_posicion():
    assert _codigos(TELEMETRIA, _esquema_tive("no_rule_matched")) == ["1"]


def test_un_tipo_de_alerta_nuevo_pasa_sin_tocar_la_configuracion():
    """Tive agrega tipos nuevos: el literal viaja solo, no hace falta una regla por tipo."""
    nuevo = {**ALERTA, "AlertType": "TipoQueNoExisteHoy"}
    assert _codigos(nuevo, _esquema_tive("no_rule_matched")) == ["TipoQueNoExisteHoy"]


def test_el_literal_se_busca_tambien_dentro_de_la_alerta():
    solo_anidado = {**BASE, "DeviceName": "Q92951", "Alert": {"AlertType": "ShockEvents"}}
    assert _codigos(solo_anidado, _esquema_tive("no_rule_matched")) == ["ShockEvents"]


def test_always_mantiene_el_comportamiento_historico():
    """Producción: Protrack usa 'always' y sigue emitiendo base + reglas."""
    assert _codigos(ALERTA, _esquema_tive("always")) == ["1", "TemperatureMax"]


def test_un_fire_when_desconocido_se_comporta_como_siempre():
    """Ante un valor raro, el comportamiento que ya existía: no se pierden eventos."""
    assert _codigos(ALERTA, _esquema_tive("cualquier_cosa")) == ["1", "TemperatureMax"]


@pytest.mark.parametrize("codigo, esperado", [
    ("10", "10"), (" 34 ", "34"), ("=AlertType", "TemperatureMax"),
    ("=Alert.AlertType", "TemperatureMax"), ("=NoExiste", None),
])
def test_resolucion_del_codigo(codigo, esperado):
    assert DynamicMapper._resolver_codigo(ALERTA, codigo) == esperado


def test_un_codigo_desde_un_campo_vacio_no_emite_la_regla():
    """Sin literal no hay código que mandar: sale la posición, no un código vacío."""
    vacio = {**BASE, "DeviceName": "Q92951", "AlertType": "", "Alert": {"AlertType": ""}}
    esquema = _esquema_tive("no_rule_matched")
    esquema["trigger_rules"][0]["field"] = "DeviceName"     # la regla coincide igual
    assert _codigos(vacio, esquema) == ["1"]
