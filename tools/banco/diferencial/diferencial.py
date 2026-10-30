"""
Prueba diferencial: pasa payloads reales por el camino REAL de una versión del
hub y escribe, por cada entrada, lo que quedó en su base, lo que su cliente
SOAP le entregaría a zeep y el sobre SOAP serializado contra el esquema real
de RC (tests/fixtures/rc_wsdl/).

  python tools/banco/diferencial/diferencial.py <raíz del código> [<salida.jsonl>]

Toma BANCO_TRABAJO/diferencial/entradas.jsonl y diccionario.json (ver
armar_entradas.py) y la configuración de Protrack y Tive del YAML del servidor
(ver comun.py). La raíz del código puede ser otra versión (un checkout del tag
anterior): así se comparan dos versiones sobre las mismas entradas.

  OPCIONES_TIVE   JSON con interruptores de Tive que pisan los del YAML.
  CEROS           JSON {"schmitz": "filtrar", ...}: opción de coordenadas 0,0
                  por integración (desde v1.9.11; ignorado por versiones viejas).

Nada sale de la máquina: el token y el cliente zeep del envío se reemplazan.
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import comun  # noqa: E402

RAIZ = os.path.abspath(sys.argv[1])
TRABAJO = comun.trabajo("diferencial")
ENTRADAS = os.path.join(TRABAJO, "entradas.jsonl")
SALIDA = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(TRABAJO, "salida.jsonl")
WSDL = os.path.join(comun.RAIZ_REPO, "tests", "fixtures", "rc_wsdl", "wsdl.xml")
YAML_CONFIG = comun.yaml_servidor()
DICCIONARIO = os.path.join(TRABAJO, "diccionario.json")

tmp = tempfile.mkdtemp(prefix="diferencial_")
os.chdir(tmp)
from cryptography.fernet import Fernet  # noqa: E402
os.environ.update(APP_ENV="development", RC_USE_MOCK="True", DASHBOARD_USER="u", DASHBOARD_PASSWORD="p",
                  MASTER_ENC_KEY=Fernet.generate_key().decode(), RC_TOKEN_ENC_KEY=Fernet.generate_key().decode(),
                  LOG_FILE_PATH=os.path.join(tmp, "app.jsonl"))
sys.path.insert(0, RAIZ)

import logging  # noqa: E402
import yaml  # noqa: E402
from lxml import etree  # noqa: E402
from zeep import Client  # noqa: E402

from app.database import get_session  # noqa: E402
from app.models.config_models import ProviderConfig, ProviderDictionary  # noqa: E402
from app.models.db_models import NormalizedRCEvent  # noqa: E402
from app.schemas.canonical import RCCanonicalModel  # noqa: E402
from app.services import rc_soap  # noqa: E402
from app.worker import pull_engine, processor  # noqa: E402
from app.api.routers import schmitz as schmitz_router  # noqa: E402
from app.api.routers import dynamic_webhook  # noqa: E402
from app.providers.tive import modulo as tive  # noqa: E402

logging.getLogger().handlers.clear()
manejador = logging.FileHandler(os.path.join(tmp, "avisos.log"), encoding="utf-8")
manejador.setLevel(logging.WARNING)
logging.getLogger().addHandler(manejador)
logging.getLogger().setLevel(logging.WARNING)
processor.trigger_worker = lambda *a, **k: None

config_yaml = yaml.safe_load(open(YAML_CONFIG, encoding="utf-8"))
def _del_yaml(nombre):
    return next((p for p in config_yaml["proveedores"] if (p["nombre"], p["entorno"]) == (nombre, "prod")), {})
protrack = _del_yaml("protrack")
MAPEO = protrack["mapeo"]
DEDUP = bool(protrack["deduplicacion"])
USA_DICCIONARIO = bool((protrack.get("diccionario") or {}).get("enabled"))
OPCIONES_TIVE = dict(_del_yaml("tive").get("opciones_modulo") or {}, **json.loads(os.environ.get("OPCIONES_TIVE") or "{}"))
CEROS = json.loads(os.environ.get("CEROS") or "{}")

db = get_session("system_config", "global")
columnas_config = {c.name for c in ProviderConfig.__table__.columns}
for nombre, tipo, extra in (("protrack", "pull", dict(mapping_schema=MAPEO, enable_state_dedup=DEDUP,
                                                    enrichment_config=protrack.get("diccionario"))),
                            ("schmitz", "push", {}), ("tive", "push", dict(module_options=OPCIONES_TIVE))):
    if "coordenadas_cero" in columnas_config and nombre in CEROS:
        extra["coordenadas_cero"] = CEROS[nombre]
    db.add(ProviderConfig(provider_name=nombre, env="prod", provider_type=tipo, is_active=True, use_mock=False, **extra))
for imei, patente in json.load(open(DICCIONARIO, encoding="utf-8")).items():
    db.add(ProviderDictionary(provider_name="protrack", env="prod", dict_key=imei, dict_value=patente))
db.commit()
db.close()

capturado = []
conteo_rc_payload = {"identico": 0, "distinto": 0, "sin_rc_payload": 0}


class _ServicioFalso:
    def GPSAssetTracking(self, token, datos):
        capturado.append(datos)
        return []


class _ZeepFalso:
    service = _ServicioFalso()


cliente_rc = rc_soap.RCSOAPClient(username="auditoria", password="x", use_mock=False)
cliente_rc._get_token_sync = lambda: "TOKEN"
rc_soap.RCSOAPClient._get_zeep_client = classmethod(lambda cls, endpoint: _ZeepFalso())
zeep_real = Client(WSDL)
NS = {"rc": "http://schemas.datacontract.org/2004/07/IronTracking"}


def _json(v):
    return v.isoformat() if isinstance(v, (datetime, date)) else v


COLUMNAS = [c.name for c in NormalizedRCEvent.__table__.columns
            if c.name not in ("id", "created_at", "updated_at", "raw_data", "ingest_id", "status",
                              "rc_response", "job_id", "rc_latency_sec", "retry_count", "next_retry_at",
                              "rc_payload")]


def _a_rc(filas):
    if not filas:
        return [], None, None
    canonicos = [RCCanonicalModel(
        chassis_number=f.chassis_number, latitude=f.latitude, longitude=f.longitude, speed=f.speed,
        code=f.code, date=f.date.replace(tzinfo=timezone.utc) if f.date else None,
        altitude=f.altitude, battery=f.battery, course=f.course, humidity=f.humidity,
        ignition=f.ignition, odometer=f.odometer, temperature=f.temperature,
        serial_number=f.serial_number, shipment=f.shipment, vehicle_type=f.vehicle_type,
        vehicle_brand=f.vehicle_brand, vehicle_model=f.vehicle_model) for f in filas]
    capturado.clear()
    cliente_rc._send_batch_sync(canonicos)
    eventos = capturado[0]["Event"]
    # v1.9.11: rc_payload tiene que ser, byte a byte, el JSON de lo que recibió
    # zeep. Una versión sin rc_payload (v1.9.10) queda como "sin_rc_payload".
    if not any(hasattr(c, "rc_payload") and c.rc_payload for c in canonicos):
        conteo_rc_payload["sin_rc_payload"] += len(canonicos)
    else:
        for c, ev in zip(canonicos, eventos):
            conteo_rc_payload["identico" if c.rc_payload == json.dumps(ev, ensure_ascii=False) else "distinto"] += 1
    try:
        sobre = zeep_real.create_message(zeep_real.service, "GPSAssetTracking", "TOKEN", {"Event": eventos})
        xml = [etree.tostring(n, encoding="unicode") for n in sobre.findall(".//rc:Event", NS)]
        error = None
    except Exception as e:
        xml, error = None, f"{type(e).__name__}: {e}"
    return eventos, xml, error


def _avisos_nuevos(estado={"pos": 0}):
    manejador.flush()
    with open(os.path.join(tmp, "avisos.log"), encoding="utf-8") as f:
        f.seek(estado["pos"])
        texto = f.read()
        estado["pos"] = f.tell()
    return [l for l in texto.splitlines() if l.strip()][-3:]


def _por_ingest(prov, iid):
    s = get_session(prov, "prod")
    return s, s.query(NormalizedRCEvent).filter(NormalizedRCEvent.ingest_id.like(f"{iid}-%")) \
               .order_by(NormalizedRCEvent.id).all()


ultimo_id = {"protrack": 0}
n = -1
with open(ENTRADAS, encoding="utf-8") as fe, open(SALIDA, "w", encoding="utf-8") as fs:
    for n, linea in enumerate(fe):
        e = json.loads(linea)
        prov, payload = e["proveedor"], e["payload"]
        iid = f"in{n}"
        try:
            if prov == "schmitz":
                schmitz_router._persist_batch([(payload, "prod", iid)])
                s, filas = _por_ingest("schmitz", iid)
            elif prov == "tive":
                eventos = tive.procesar(payload, "prod", OPCIONES_TIVE, iid)
                if eventos:
                    dynamic_webhook._save_dynamic_events("tive", "prod", eventos, payload, iid)
                s, filas = _por_ingest("tive", iid)
            else:
                asyncio.run(pull_engine.process_and_enqueue("protrack", "prod", payload, MAPEO,
                                                           enable_state_dedup=DEDUP,
                                                           require_dict_match=USA_DICCIONARIO))
                s = get_session("protrack", "prod")
                filas = s.query(NormalizedRCEvent).filter(NormalizedRCEvent.id > ultimo_id["protrack"]) \
                         .order_by(NormalizedRCEvent.id).all()
                if filas:
                    ultimo_id["protrack"] = filas[-1].id
            base = [{c: _json(getattr(f, c)) for c in COLUMNAS} for f in filas]
            rc, xml, error_xml = _a_rc(filas)
            s.close()
            salida = {"id": e["id"], "proveedor": prov, "filas": base, "rc": rc, "xml": xml,
                      "error_xml": error_xml, "error": None}
        except Exception as ex:
            salida = {"id": e["id"], "proveedor": prov, "filas": [], "rc": [], "xml": None,
                      "error_xml": None, "error": f"{type(ex).__name__}: {ex}"}
        salida["avisos"] = _avisos_nuevos() if not salida["filas"] else []
        fs.write(json.dumps(salida, ensure_ascii=False, default=str) + "\n")
print("listo", n + 1, "entradas ->", SALIDA)
print("rc_payload contra lo entregado a zeep:", conteo_rc_payload)
