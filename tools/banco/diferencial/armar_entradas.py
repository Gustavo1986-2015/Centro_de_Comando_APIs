"""
Arma entradas.jsonl y diccionario.json para la prueba diferencial.

  python tools/banco/diferencial/armar_entradas.py

Entradas: fixtures de los tests, payloads sintéticos derivados de un payload
real de Schmitz (cada uno rompe una regla del contrato) y TODOS los crudos de
BANCO_DATOS/audit/{schmitz,protrack,tive}_*. El payload real de base es el de
BANCO_SCHMITZ_REAL (un JSON) si se indica; si no, el fixture SCHMITZ_REAL.
Sale en BANCO_TRABAJO/diferencial/.
"""
import ast
import copy
import glob
import json
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import comun  # noqa: E402

DATOS, REPO, SALIDA = comun.datos(), comun.RAIZ_REPO, comun.trabajo("diferencial")
entradas = []


def agregar(id_, proveedor, payload):
    entradas.append({"id": id_, "proveedor": proveedor, "payload": payload})


def literal(texto, nombre):
    for nodo in ast.walk(ast.parse(texto)):
        if isinstance(nodo, ast.Assign) and any(getattr(t, "id", None) == nombre for t in nodo.targets):
            return ast.literal_eval(nodo.value)
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            for sub in ast.walk(nodo):
                if isinstance(sub, ast.Return):
                    return ast.literal_eval(sub.value)
    raise KeyError(nombre)


leer = lambda r: open(os.path.join(REPO, r), encoding="utf-8").read()
fixture_real = literal(leer("tests/proveedores/schmitz/test_schmitz_v192.py"), "SCHMITZ_REAL")
if os.environ.get("BANCO_SCHMITZ_REAL"):
    real = json.load(open(os.environ["BANCO_SCHMITZ_REAL"], encoding="utf-8"))
    agregar("schmitz:real:BANCO_SCHMITZ_REAL", "schmitz", real)
else:
    real = fixture_real
agregar("schmitz:fixture:SCHMITZ_REAL", "schmitz", fixture_real)
agregar("schmitz:fixture:sample_schmitz_payload", "schmitz", literal(leer("tests/conftest.py"), "sample_schmitz_payload"))
v186 = subprocess.run(["git", "-C", REPO, "show", "v1.8.6:tests/conftest.py"], capture_output=True, text=True,
                      encoding="utf-8").stdout
if v186:
    agregar("schmitz:fixture:sample_schmitz_payload(v1.8.6)", "schmitz", literal(v186, "sample_schmitz_payload"))

sd = lambda p: p["StatusData"][0]


def derivado(nombre, cambio):
    p = copy.deepcopy(real)
    cambio(p)
    agregar(f"schmitz:sintetico:{nombre}", "schmitz", p)


derivado("sin_patente_ni_chasis", lambda p: [p.pop(k, None) for k in ("Plate", "Name", "ChassisNumber")])
derivado("solo_chasis_sin_patente", lambda p: [p.pop(k, None) for k in ("Plate", "Name")])
derivado("sin_DeviceTime", lambda p: p.pop("DeviceTime"))
derivado("sin_lat_lon", lambda p: [sd(p)["Position"].pop(k) for k in ("Latitude", "Longitude")])
derivado("lat_lon_cero", lambda p: sd(p)["Position"].update(Latitude=0, Longitude=0))
derivado("solo_lat_cero", lambda p: sd(p)["Position"].update(Latitude=0))
derivado("ignicion_ausente_en_movimiento", lambda p: sd(p)["SensorStatus"].pop("IsIgnitionOn"))
derivado("ignicion_y_movimiento_ausentes", lambda p: (sd(p)["SensorStatus"].pop("IsIgnitionOn"),
                                                      sd(p)["SensorStatus"].pop("IsInMotion")))
derivado("velocidad_sin_medir", lambda p: (sd(p)["Position"].update(GPSSpeed={"exists": False, "Value": 0}),
                                           sd(p)["EBS"].pop("Velocity")))
derivado("humedad_decimal", lambda p: sd(p).update(TCI={"Humidity": 55.7}))
derivado("temperatura", lambda p: sd(p).update(Temp={"Temp1": 4.5, "Temp2": 5.0}))
derivado("con_alarma", lambda p: (p.update(Reason={"Item": True, "ItemElementName": "IgnitionAlarm"}),
                                  p.update(Events=[{"Type": "IgnitionAlarm"}, {"Type": "Standard"}])))
derivado("voltaje_negativo", lambda p: sd(p)["SensorStatus"].update(
    Battery={"ExternalPowerSupplyVoltage": -1, "ExternalPowerSupplyVoltageSpecified": True}))
derivado("odometro_negativo", lambda p: sd(p)["EBS"].update(Milage=-1))
derivado("gps_cero_con_ebs", lambda p: (sd(p)["Position"].update(GPSSpeed={"exists": True, "Value": 0}),
                                        sd(p)["EBS"].update(Velocity="50")))

for ruta in comun.crudos("schmitz_prod") + comun.crudos("schmitz_test"):
    entorno = ruta.split(os.sep)[-3]
    for i, p in enumerate(comun.payloads(ruta)):
        agregar(f"schmitz:crudo:{entorno}:{os.path.basename(ruta)}:{i+1}", "schmitz", p)

imeis = []
for ruta in comun.crudos("protrack_prod") + comun.crudos("protrack_test"):
    entorno = ruta.split(os.sep)[-3]
    for i, p in enumerate(comun.payloads(ruta)):
        if isinstance(p, dict) and "imei" in p:
            imeis.append(str(p["imei"]))
            agregar(f"protrack:crudo:{entorno}:{os.path.basename(ruta)}:{i+1}", "protrack", {"code": 0, "record": [p]})
        else:
            agregar(f"protrack:crudo:{entorno}:{os.path.basename(ruta)}:{i+1}:respuesta", "protrack", p)
# Sintéticos de Protrack para la opción de 0,0 (v1.9.11).
if imeis:
    base_pt = next(e["payload"]["record"][0] for e in entradas if e["proveedor"] == "protrack" and "record" in e["payload"]
                   and isinstance(e["payload"]["record"], list) and e["payload"]["record"])
    for nombre, cambio in (("lat_lon_cero", {"latitude": 0, "longitude": 0}), ("solo_lon_cero", {"longitude": 0})):
        agregar(f"protrack:sintetico:{nombre}", "protrack", {"code": 0, "record": [dict(copy.deepcopy(base_pt), **cambio)]})

for ruta in comun.crudos("tive_prod"):
    for i, p in enumerate(comun.payloads(ruta)):
        agregar(f"tive:crudo:{os.path.basename(ruta)}:{i+1}", "tive", p)
for linea in open(os.path.join(REPO, "tests", "fixtures", "tive_crudos_2026-10.jsonl"), encoding="utf-8"):
    d = json.loads(linea)
    agregar(f"tive:fixture:{d['origen']}", "tive", d["payload"])

unicos = sorted(set(imeis))
json.dump({imei: f"PT{imei[-6:]}" for imei in unicos[2:]},
          open(os.path.join(SALIDA, "diccionario.json"), "w"), indent=0)
with open(os.path.join(SALIDA, "entradas.jsonl"), "w", encoding="utf-8") as f:
    for e in entradas:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
print(len(entradas), "entradas en", SALIDA, "|", Counter(":".join(e["id"].split(":")[:2]) for e in entradas))
