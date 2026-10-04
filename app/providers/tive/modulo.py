"""
Tive como integración dedicada.

Recibe por la misma URL de siempre, /webhook/dynamic/tive, para no
reconfigurar nada del lado de Tive. El endpoint genérico no sabe nada de
Tive: consulta el registro de módulos dedicados (app/providers/registry.py) y
le entrega el payload a este módulo. La firma HMAC, la red de seguridad, la
cola y el envío a RC son los comunes.

QUÉ SE ENVÍA

Tive ya está integrado con Recurso Confiable por otro webhook, con los datos
de los trackers. El hub solo manda lo que RC no tiene:

  - posiciones de tramos de terceros (contenedor; aéreo cuando aparezca)
  - alertas de beacons
  - alertas de trackers, SOLO si se activa su interruptor (hoy duplicaría RC)

Las posiciones de los trackers no se envían: van por RC directo.

TODO SE MIDIÓ CONTRA LOS CRUDOS REALES (audit/tive_prod/2026-10/)

  - Tramo de tercero: Location.LocationMethod "container", DeviceId,
    DeviceName y sensores en null, Shipment.ContainerId poblado. Regla
    aceptada: DeviceName nulo + coordenadas en Location. El literal del tramo
    aéreo no se vio: cualquier LocationMethod nuevo se avisa en consola.
  - Tracker vs beacon: los 1235 DeviceId de trackers medidos son IMEI de 15
    dígitos; el único beacon visto, A2A2A20173D4, no es numérico. Una alerta
    cuyo DeviceId no es numérico se trata como de beacon. Nunca se vio una
    alerta de beacon real: queda pendiente de confirmar con un JSON real.
  - Apertura, cierre y puntual se leen de Alert.Details[].Reasons:
        [["Created","Latest"]]        apertura de rango
        [["Created"],["Latest"]]      actualización de una alerta abierta
        [["Created"],["Closed"]]      cierre de rango        -> código -FIN
        [["Created","Closed"]]        puntual (golpe, luz)   -> nunca -FIN
    IsClosed solo no alcanza: la puntual también viene cerrada.
  - Duplicados: Tive manda dos alertas con AlertId DISTINTO para el mismo
    hecho (11 pares de K393478 el 02/10, mismo trigger, misma lectura, mismo
    ShipmentId raíz). Se deduplica por dos claves y alcanza con que coincida
    una: AlertId + estado, o equipo + trigger + EntryTimeEpoch + estado.
"""
import logging
from datetime import datetime, timezone

import dateutil.parser

from app.core.contrato import filtrar_validos
from app.providers.tive import estado
from app.schemas.canonical import RCCanonicalModel

logger = logging.getLogger(__name__)

PROVEEDOR = "tive"

# Interruptores por integración, persistidos en provider_config.module_options
# y editables desde el panel. Los valores son los por defecto.
INTERRUPTORES = {
    "posiciones_terceros": True,
    "alertas_beacons": True,
    # Apagado: RC ya recibe las alertas de los trackers por su propio webhook.
    # Encenderlo sin pedir antes la baja en RC las duplicaría.
    "alertas_trackers": False,
}

DESCRIPCION_INTERRUPTORES = {
    "posiciones_terceros": "Posiciones de contenedores y aéreas",
    "alertas_beacons": "Alertas de beacons",
    "alertas_trackers": "Alertas de trackers (duplica RC si no se pidió la baja)",
}

# LocationMethod vistos en los crudos del 01 y 02/10. Cualquier otro se avisa:
# es la forma de detectar el primer tramo aéreo sin inventar su literal.
METODOS_CONOCIDOS = frozenset({"wifi", "cell", "gps", "container"})
_metodos_avisados: set[str] = set()

CODIGO_POSICION = "1"


def opciones_efectivas(module_options) -> dict:
    """Los interruptores guardados, con los valores por defecto donde falten."""
    efectivas = dict(INTERRUPTORES)
    for clave in INTERRUPTORES:
        valor = (module_options or {}).get(clave) if isinstance(module_options, dict) else None
        if isinstance(valor, bool):
            efectivas[clave] = valor
    return efectivas


# ── Lectura del payload ──────────────────────────────────────────────────────

def _g(dato, *claves):
    for clave in claves:
        if not isinstance(dato, dict):
            return None
        dato = dato.get(clave)
    return dato


def _texto(valor) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _numero(valor) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _fecha(payload) -> datetime | None:
    """EntryTimeUtc: la lectura que disparó el evento (decisión 3 del informe)."""
    texto = _texto(payload.get("EntryTimeUtc"))
    if texto:
        try:
            dt = dateutil.parser.isoparse(texto)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            logger.warning(f"[TIVE] EntryTimeUtc ilegible: {texto!r}. Se intenta con EntryTimeEpoch.")
    epoch = _numero(payload.get("EntryTimeEpoch"))
    if epoch is not None:
        return datetime.fromtimestamp(epoch / 1000.0, tz=timezone.utc)
    return None


def _es_alerta(payload) -> bool:
    return isinstance(payload.get("Alert"), dict)


def _tipo_alerta(payload) -> str | None:
    return _texto(payload.get("AlertType")) or _texto(_g(payload, "Alert", "AlertType"))


def _id_equipo(payload) -> str | None:
    return _texto(payload.get("DeviceId")) or _texto(_g(payload, "Alert", "DeviceId"))


def _identidad(payload) -> str:
    """Quién era, para cada aviso de consola."""
    partes = [
        f"equipo={_texto(payload.get('DeviceName')) or _texto(_g(payload, 'Alert', 'DeviceName')) or '-'}",
        f"id={_id_equipo(payload) or '-'}",
        f"envío={_texto(payload.get('ShipmentId')) or '-'}",
    ]
    contenedor = _texto(_g(payload, "Shipment", "ContainerId"))
    if contenedor:
        partes.append(f"contenedor={contenedor}")
    if _es_alerta(payload):
        partes.append(f"alerta={_tipo_alerta(payload) or '-'}")
        alert_id = _texto(_g(payload, "Alert", "AlertId"))
        if alert_id:
            partes.append(f"AlertId={alert_id}")
    return " ".join(partes)


def _descartar(env: str, motivo: str, payload, nivel=logging.INFO) -> None:
    logger.log(nivel, f"[TIVE-{env}] Descartado, NO se envía a RC: {motivo} | {_identidad(payload)}")


def _envio(env: str, payload) -> str | None:
    """
    El número del viaje que cargó el usuario: ShipmentId de la raíz.

    Alert.ShipmentId es el código público de Tive (ej. 1Q27QDCJWT) y solo se
    usa como último recurso, avisando.
    """
    for valor in (payload.get("ShipmentId"), _g(payload, "Shipment", "Id")):
        if _texto(valor):
            return _texto(valor)
    publico = _texto(_g(payload, "Alert", "ShipmentId"))
    if publico:
        logger.warning(
            f"[TIVE-{env}] Sin ShipmentId en la raíz: se usa Alert.ShipmentId {publico}, "
            f"que es el código público de Tive y no el número del viaje | {_identidad(payload)}"
        )
    return publico


def estado_alerta(payload) -> str | None:
    """
    'apertura', 'actualizacion', 'cierre', 'puntual', o None si la forma de
    Reasons no es ninguna de las cuatro medidas.
    """
    detalles = _g(payload, "Alert", "Details") or []
    conjuntos = [set(d.get("Reasons") or []) for d in detalles if isinstance(d, dict)]
    if any({"Created", "Closed"} <= c for c in conjuntos):
        return "puntual"
    if any("Closed" in c for c in conjuntos):
        return "cierre"
    if any({"Created", "Latest"} <= c for c in conjuntos):
        return "apertura"
    if any("Created" in c for c in conjuntos) and any("Latest" in c for c in conjuntos):
        return "actualizacion"
    return None


def resolver_patentes(env: str, payload) -> list[tuple[str, str | None]]:
    """
    Patentes del evento, con el número de serie de cada una.

    En orden, gana el primer paso que resuelva:
      1. DeviceName, si viniera.
      2. Shipment.DeviceId traducido con el par aprendido.
      3. Cada elemento de Shipment.DeviceIds que SÍ tenga par aprendido.

    Un identificador sin par (un beacon, un tracker que nunca reportó con
    nombre) no cuenta. Nunca se usa el número de serie, el contenedor ni un
    relleno como patente: sin resolución, la lista sale vacía.
    """
    nombre = _texto(payload.get("DeviceName")) or _texto(_g(payload, "Alert", "DeviceName"))
    if nombre:
        return [(nombre, _id_equipo(payload))]

    envio = payload.get("Shipment") if isinstance(payload.get("Shipment"), dict) else {}
    principal = _texto(envio.get("DeviceId"))
    if principal:
        par = estado.nombre_de(env, principal)
        if par:
            return [(par, principal)]

    patentes = []
    for device_id in envio.get("DeviceIds") or []:
        device_id = _texto(device_id)
        par = estado.nombre_de(env, device_id) if device_id else None
        if par and par not in [p for p, _ in patentes]:
            patentes.append((par, device_id))
    return patentes


def _evento(env: str, payload, patente: str, serie: str | None, codigo: str) -> RCCanonicalModel:
    return RCCanonicalModel(
        chassis_number=patente,
        latitude=_numero(_g(payload, "Location", "Latitude")),
        longitude=_numero(_g(payload, "Location", "Longitude")),
        # Tive no mide velocidad: None, y el panel muestra N/A. A RC le llega
        # "0", como pide el contrato.
        speed=None,
        code=codigo,
        date=_fecha(payload),
        temperature=_numero(_g(payload, "Temperature", "Celsius")),
        humidity=_numero(_g(payload, "Humidity", "Percentage")),
        battery=_numero(_g(payload, "Battery", "Percentage")),
        # Tive no mide ignición: no se envía.
        ignition=None,
        serial_number=serie,
        shipment=_envio(env, payload),
    )


# ── Aprendizaje y vigilancia ─────────────────────────────────────────────────

def _aprender(env: str, payload) -> None:
    estado.aprender_par(env, payload.get("DeviceId"), payload.get("DeviceName"))
    alerta = payload.get("Alert")
    if isinstance(alerta, dict):
        estado.aprender_par(env, alerta.get("DeviceId"), alerta.get("DeviceName"))


def _vigilar_metodo(env: str, payload) -> None:
    metodo = _texto(_g(payload, "Location", "LocationMethod"))
    if not metodo or metodo.lower() in METODOS_CONOCIDOS:
        return
    primero = metodo not in _metodos_avisados
    _metodos_avisados.add(metodo)
    logger.log(
        logging.WARNING if primero else logging.INFO,
        f"[TIVE-{env}] LocationMethod nunca visto: {metodo!r}"
        f"{' (primer aviso: puede ser el primer tramo aéreo, guardar este JSON)' if primero else ''}"
        f" | {_identidad(payload)}",
    )


# ── Clasificación ────────────────────────────────────────────────────────────

def _candidatos_alerta(env: str, payload, opciones: dict) -> list[tuple]:
    tipo = _tipo_alerta(payload)
    if not tipo:
        _descartar(env, "alerta sin AlertType", payload, logging.WARNING)
        return []

    forma = estado_alerta(payload)
    if forma is None:
        reasons = [d.get("Reasons") for d in (_g(payload, "Alert", "Details") or []) if isinstance(d, dict)]
        _descartar(env, f"forma de alerta no reconocida (Reasons={reasons}): no se puede saber "
                        f"si es apertura, cierre o puntual", payload, logging.WARNING)
        return []

    cerrada = payload.get("IsClosed")
    if cerrada is None:
        cerrada = _g(payload, "Alert", "IsClosed")
    if isinstance(cerrada, bool) and cerrada != (forma in ("cierre", "puntual")):
        logger.warning(
            f"[TIVE-{env}] IsClosed={cerrada} no coincide con Reasons ({forma}). "
            f"Se usa Reasons | {_identidad(payload)}"
        )

    device_id = _id_equipo(payload)
    if not device_id:
        _descartar(env, "alerta sin DeviceId: no se puede saber si es de tracker o de beacon",
                   payload, logging.WARNING)
        return []
    clase = "tracker" if device_id.isdigit() else "beacon"
    if not opciones[f"alertas_{clase}s"]:
        _descartar(env, f"alerta de {clase}, interruptor apagado", payload)
        return []
    if clase == "beacon":
        logger.info(
            f"[TIVE-{env}] Alerta tratada como de beacon porque su DeviceId no es un IMEI "
            f"(regla inferida: aún no se vio una alerta de beacon real) | {_identidad(payload)}"
        )

    patentes = resolver_patentes(env, payload)
    if not patentes:
        _descartar(env, "alerta sin patente resoluble", payload, logging.WARNING)
        return []

    codigo = f"{tipo}-FIN" if forma == "cierre" else tipo
    # La actualización de una alerta abierta comparte clave con la apertura:
    # si la apertura ya se envió, es un duplicado; si nunca se registró, esta
    # primera vez se envía como apertura (decisión 2 del informe).
    clave_estado = "apertura" if forma in ("apertura", "actualizacion") else forma
    alert_id = _texto(_g(payload, "Alert", "AlertId"))
    trigger = _texto(_g(payload, "Alert", "AlertTriggerId"))
    lectura = _texto(payload.get("EntryTimeEpoch"))

    candidatos = []
    for patente, serie in patentes:
        claves = []
        if alert_id:
            claves.append(f"alerta|{alert_id}|{clave_estado}|{patente}")
        if trigger and lectura:
            claves.append(f"alerta|{device_id}|{trigger}|{lectura}|{clave_estado}|{patente}")
        if forma == "actualizacion":
            motivo_dup = "actualización de una alerta abierta que ya se envió"
            nota = "actualización de una alerta cuya apertura nunca se registró: se envía como apertura"
        else:
            motivo_dup = f"duplicado de {codigo} ({forma}) ya recibido"
            nota = None
        candidatos.append((_evento(env, payload, patente, serie, codigo), claves, motivo_dup, nota))
    return candidatos


def _candidatos_posicion(env: str, payload, opciones: dict) -> list[tuple]:
    if _texto(payload.get("DeviceName")):
        _descartar(env, "posición de tracker, va por RC directo", payload)
        return []

    lat = _numero(_g(payload, "Location", "Latitude"))
    lon = _numero(_g(payload, "Location", "Longitude"))
    if lat is None or lon is None:
        _descartar(env, "evento sin nombre de equipo y sin coordenadas en Location: no es un "
                        "tramo de tercero utilizable", payload, logging.WARNING)
        return []

    # Tramo de tercero: DeviceName nulo + coordenadas en Location.
    if not opciones["posiciones_terceros"]:
        _descartar(env, "posición de tramo de tercero, interruptor apagado", payload)
        return []

    patentes = resolver_patentes(env, payload)
    if not patentes:
        envio = payload.get("Shipment") if isinstance(payload.get("Shipment"), dict) else {}
        _descartar(
            env,
            f"tramo de tercero sin patente resoluble: ningún equipo del envío tiene par "
            f"aprendido (envío={_texto(payload.get('ShipmentId')) or '-'}, "
            f"contenedor={_texto(envio.get('ContainerId')) or '-'}, "
            f"serie={_texto(envio.get('DeviceId')) or '-'}, "
            f"equipos={envio.get('DeviceIds') or []}, "
            f"método={_texto(_g(payload, 'Location', 'LocationMethod')) or '-'})",
            payload, logging.WARNING,
        )
        return []

    lectura = _texto(payload.get("EntryTimeEpoch")) or _texto(payload.get("EntryTimeUtc"))
    envio_id = _texto(payload.get("ShipmentId")) or _texto(_g(payload, "Shipment", "Id")) or "-"
    candidatos = []
    for patente, serie in patentes:
        claves = [f"pos|{envio_id}|{patente}|{lectura}"] if lectura else []
        candidatos.append((
            _evento(env, payload, patente, serie, CODIGO_POSICION),
            claves, "posición duplicada (mismo envío, equipo y lectura)", None,
        ))
    return candidatos


# ── Punto de entrada ─────────────────────────────────────────────────────────

def procesar(payload, env: str, module_options, ingest_id: str) -> list[RCCanonicalModel]:
    """
    Los eventos a encolar para un payload de Tive. Lista vacía si no hay nada
    que enviar; cada descarte queda en consola con motivo e identidad.

    El ingest_id es el de la recepción: hace que reprocesar el mismo payload
    desde la red de seguridad no se tome como duplicado de sí mismo.
    """
    if not isinstance(payload, dict):
        logger.warning(f"[TIVE-{env}] Payload que no es un objeto JSON, descartado: {str(payload)[:200]}")
        return []

    if _texto(payload.get("AccountId")) == "-1":
        _descartar(env, "muestra del botón de prueba de Tive (AccountId -1)", payload)
        return []

    # Antes de cualquier filtro: hasta una posición de tracker que no se envía
    # sirve para resolver después la patente de un tramo de contenedor.
    _aprender(env, payload)
    _vigilar_metodo(env, payload)

    opciones = opciones_efectivas(module_options)
    if _es_alerta(payload):
        candidatos = _candidatos_alerta(env, payload, opciones)
    else:
        candidatos = _candidatos_posicion(env, payload, opciones)
    if not candidatos:
        return []

    # Validación del contrato ANTES de registrar las claves: un evento
    # descartado por falta de datos no tiene que contar como "ya visto".
    validos = filtrar_validos([c[0] for c in candidatos], PROVEEDOR, env)

    salida = []
    for evento, claves, motivo_dup, nota in candidatos:
        if not any(evento is v for v in validos):
            continue
        if estado.registrar_o_duplicado(env, claves, ingest_id):
            _descartar(env, motivo_dup, payload)
            continue
        if nota:
            logger.info(f"[TIVE-{env}] {nota} | {_identidad(payload)}")
        salida.append(evento)
    return salida
