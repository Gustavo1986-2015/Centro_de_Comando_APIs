# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v192.py (reorganización por tema: rc).
"""
v1.9.2 — Mejoras transversales: validación única del contrato, sin UNKNOWN,
datos de vehículo, humedad con decimales, velocidad N/A, panel.

Cada cambio de código compartido se prueba con las configuraciones y payloads
REALES de Protrack y Schmitz:
  · PROTRACK_MAPEO es el esquema de protrack/test de la base local (el de
    protrack/prod está vacío). recuperar_protrack.yaml no está en el repo: sus
    cuatro reglas son las mismas que ya prueba test_v191.py.
  · PROTRACK_ITEM es un registro real (audit/protrack_prod/2026-08/, 07/08).
  · SCHMITZ_REAL es un payload real (audit/schmitz_test/2026-08/, 10/08).
"""
import copy
import logging
import math
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core import admision, contrato
from app.core.dynamic_mapper import DynamicMapper
from app.services.rc_soap import construir_evento_rc

SCHMITZ_REAL = {
    "ChassisNumber": "TEST-027", "Plate": "TEST-027", "CtuId": 11262712,
    "ReferenceUserName": "ASSISTCARGO", "ReceiveTime": "2026-08-10T17:08:54Z",
    "DeviceTime": "2026-08-10T18:08:54.0000000+01:00",
    "Reason": {"Item": True, "ItemElementName": "IgnitionAlarm"},
    "Events": [{"Type": "IgnitionAlarm", "Value": None}],
    "StatusData": [{
        "Position": {"GPSDateTime": "2026-08-10T17:08:54Z", "Latitude": 46.013432, "Longitude": 7.053484,
                     "GPSHeading": "124", "Altitude": 264, "GPSSpeed": {"exists": True, "Value": 63},
                     "GPSMilage": {"exists": True, "Value": 44611.212}},
        "EBS": {"Velocity": "63", "Milage": 44611.212, "Signal": "Off"},
        "SensorStatus": {"IsCoupled": True, "IsDoor1Open": False, "IsIgnitionOn": True,
                         "IsInMotion": {"exists": True, "Value": True},
                         "Battery": {"ExternalPowerSupplyVoltage": 12.8, "ExternalPowerSupplyVoltageSpecified": True},
                         "DoorLocking": {"State": "Closed", "ContactSensor": True, "CmdSource": "Portal"},
                         "AntiTheft": {"AlarmWire": "Closed",
                                       "DoorOpenAlarm": {"IsPresent": False, "EnabledState": "Enabled"}}},
        "Temp": {"Temp1": 19.7, "Temp2": 19.8}, "TCI": {"FuelLevel": {"FuelLevel": "38"}},
        "Tapa": {"Active": True, "TapaVehicleStop": False,
                 "SabotageDetection": {"EbsDisconnect": False, "CisBatteryGuardCanDisconnect": False,
                                       "DoorlockingSystemLinDisconnected": False, "BcuDisconnected": False,
                                       "AlarmSystemDisconnected": False, "CoupledSensorDisconnected": False}},
    }],
    "SystemConfig": {"TrailerType": "TIPPER", "TrailerProducer": "SCHMITZ_CARGOBULL_AG",
                     "TelematicType": "CTU_3", "HasCouplingSensor": True, "HasDoorSensor1": True,
                     "HasIgnitionSignal": True},
}


def _ev(**campos):
    base = dict(chassis_number="AB123", code="1", date=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
                latitude=-34.6, longitude=-58.4, speed=None, course=None, ignition=None,
                altitude=None, battery=None, humidity=None, odometer=None, temperature=None,
                serial_number=None, shipment=None, vehicle_type=None, vehicle_brand=None,
                vehicle_model=None)
    base.update(campos)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def caches_limpios():
    from app.providers.schmitz import mapper
    admision.reset()
    mapper._STATE_CACHE.clear()
    yield
    admision.reset()
    mapper._STATE_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════════
# 3.3 y 3.4 — Datos de vehículo y humedad, contra el esquema REAL de RC
# ═══════════════════════════════════════════════════════════════════════════

def test_los_datos_de_vehiculo_se_envian_si_existen():
    ev = construir_evento_rc(_ev(vehicle_type="TIPPER", vehicle_brand="SCHMITZ_CARGOBULL_AG",
                                 vehicle_model="CTU_3"))
    assert (ev["vehicleType"], ev["vehicleBrand"], ev["vehicleModel"]) == (
        "TIPPER", "SCHMITZ_CARGOBULL_AG", "CTU_3")


def test_sin_datos_de_vehiculo_no_se_mandan_vacios():
    ev = construir_evento_rc(_ev())
    assert not {"vehicleType", "vehicleBrand", "vehicleModel"} & set(ev)


def test_la_humedad_conserva_los_decimales():
    assert construir_evento_rc(_ev(humidity=55.2))["humidity"] == 55.2


def test_el_evento_real_de_schmitz_serializa_contra_el_esquema_real_de_rc():
    """
    Copia del WSDL y los XSD de RCService.svc bajados el 02/10/2026
    (tests/fixtures/rc_wsdl/). El tipo Event declara vehicleType,
    vehicleBrand, vehicleModel y humidity como xs:string. Se serializa el
    lote exactamente como lo manda _send_batch_sync.
    """
    from lxml import etree
    from zeep import Client

    from app.providers.schmitz.mapper import map_schmitz_payload

    wsdl = os.path.join(os.path.dirname(__file__), "..", "fixtures", "rc_wsdl", "wsdl.xml")
    cliente = Client(wsdl)
    eventos = [construir_evento_rc(e) for e in map_schmitz_payload(copy.deepcopy(SCHMITZ_REAL))]
    eventos.append(construir_evento_rc(_ev(humidity=55.2)))
    sobre = cliente.create_message(cliente.service, "GPSAssetTracking", "token", {"Event": eventos})

    # Por namespace y nombre, no por texto: zeep elige el prefijo (ns1, ns6...)
    # según el orden en que cargó los esquemas.
    ns = {"rc": "http://schemas.datacontract.org/2004/07/IronTracking"}
    nodos = sobre.findall(".//rc:Event", ns)
    assert len(nodos) == len(eventos)
    primero = nodos[0]
    assert primero.findtext("rc:vehicleType", namespaces=ns) == "TIPPER"
    assert primero.findtext("rc:vehicleBrand", namespaces=ns) == "SCHMITZ_CARGOBULL_AG"
    assert primero.findtext("rc:vehicleModel", namespaces=ns) == "CTU_3"
    assert nodos[-1].findtext("rc:humidity", namespaces=ns) == "55.2"
    assert etree.tostring(sobre)  # el sobre completo se serializa sin error


# ═══════════════════════════════════════════════════════════════════════════
# 3.6 — Velocidad N/A cuando no se mide
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_velocidad_queda_en_none_y_a_rc_le_llega_cero():
    esquema = {"base_mapping": {"chassis_number": "placa", "latitude": "lat", "longitude": "lon",
                                "date": "fecha", "speed": "velocidad"}}
    (ev,) = DynamicMapper.map_payload_multi(
        {"placa": "AB1", "lat": 1.0, "lon": 2.0, "fecha": "2026-10-02T10:00:00Z"}, esquema)
    assert ev.speed is None
    assert construir_evento_rc(ev)["speed"] == "0"


def test_la_columna_de_velocidad_admite_nulo():
    from app.models.db_models import NormalizedRCEvent
    assert NormalizedRCEvent.__table__.c.speed.nullable
