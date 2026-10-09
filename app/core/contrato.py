"""
Validación única de los campos obligatorios del contrato con Recurso Confiable.

POR QUÉ EXISTE

Un evento sin patente real, sin fecha o sin coordenadas no describe nada que
RC pueda ubicar. Antes llegaba igual, con datos inventados en el camino:

  - sin patente, el mapeador dinámico ponía "UNKNOWN" (evento del 02/10 15:44,
    un contenedor en 3.309, -78.617);
  - sin fecha, rc_soap le ponía la hora del envío;
  - sin coordenadas, rc_soap mandaba "0", y el evento aparecía frente a África.

Desde la v1.9.2 esos eventos se descartan AL INGRESAR, antes de entrar a la
cola, con un aviso en consola que dice qué proveedor, qué equipo y qué campo
faltó. Descartar al despachar no sirve: el evento ya estaría en la cola y no
puede terminar marcado como "enviado".

QUIÉN LA USA

Todos los caminos de entrada, con esta misma función:
  - Schmitz (app/providers/schmitz/mapper.py)
  - webhook dinámico, PULL y red de seguridad (DynamicMapper.map_payload_multi)
  - módulo de Tive (app/providers/tive/modulo.py)

QUÉ NO VALIDA

La velocidad: el contrato pide "0" cuando no hay dato, y eso lo resuelve
rc_soap al armar el envío. Una coordenada que vale exactamente 0 es un dato
real y pasa: vacío es None, no 0.0.
"""
import logging
import math

logger = logging.getLogger(__name__)

# Valor que el mapeador dinámico usaba como relleno cuando no encontraba
# identificador. Ya no lo genera, pero una configuración o un payload que lo
# traiga literal sigue sin ser una patente real.
_PATENTES_FALSAS = {"UNKNOWN"}


def _vacio_numerico(valor) -> bool:
    if valor is None:
        return True
    try:
        return math.isnan(float(valor))
    except (TypeError, ValueError):
        # No es un número: no es una coordenada utilizable.
        return True


def faltantes(evento) -> list[str]:
    """
    Los campos obligatorios que le faltan al evento, en el orden del contrato.

    Lista vacía = el evento se puede enviar. Acepta el modelo canónico o
    cualquier objeto con los mismos atributos.
    """
    falta = []
    patente = getattr(evento, "chassis_number", None)
    if patente is None or not str(patente).strip() or str(patente).strip().upper() in _PATENTES_FALSAS:
        falta.append("patente")
    if getattr(evento, "date", None) is None:
        falta.append("fecha")
    if _vacio_numerico(getattr(evento, "latitude", None)):
        falta.append("latitud")
    if _vacio_numerico(getattr(evento, "longitude", None)):
        falta.append("longitud")
    return falta


# ─── Valores imposibles (v1.9.8, auditoría B-3) ─────────────────────────────
#
# Protrack manda battery=-1 y odometer=-1 para decir "sin dato", y llegaban a
# RC como medición en el 100% de sus eventos. No es exclusivo de Protrack:
# cualquier proveedor puede usar un centinela así. Una batería fuera de 0-100
# o un odómetro negativo no son una medición: el campo se omite, como
# cualquier dato ausente, y el evento sigue su camino.
#
# La batería de Schmitz es el voltaje de alimentación externa (12-28 V):
# también cae en 0-100, así que no cambia.

def _fuera_de_rango(valor, minimo, maximo=None) -> bool:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return True
    if math.isnan(numero) or numero < minimo:
        return True
    return maximo is not None and numero > maximo


def sanear_mediciones(evento):
    """
    Omite (None) los valores que no pueden ser una medición. Devuelve la lista
    de campos que se omitieron. Modifica el evento en el lugar.
    """
    omitidos = []
    bateria = getattr(evento, "battery", None)
    if bateria is not None and _fuera_de_rango(bateria, 0, 100):
        evento.battery = None
        omitidos.append("battery")
    odometro = getattr(evento, "odometer", None)
    if odometro is not None and _fuera_de_rango(odometro, 0):
        evento.odometer = None
        omitidos.append("odometer")
    return omitidos


def filtrar_validos(eventos, proveedor: str | None, env: str | None) -> list:
    """
    Devuelve solo los eventos que cumplen el contrato. Cada descarte queda en
    consola con proveedor, equipo y campo faltante: nunca desaparece en
    silencio.
    """
    validos = []
    for ev in eventos or []:
        falta = faltantes(ev)
        if not falta:
            # Mismo punto de paso para todos los caminos de ingreso: acá se
            # omiten también los valores imposibles (B-3). Sin aviso por
            # evento: con Protrack sería una línea por cada uno.
            sanear_mediciones(ev)
            validos.append(ev)
            continue
        patente = getattr(ev, "chassis_number", None)
        serie = getattr(ev, "serial_number", None)
        from app.core import descartes
        latitud, longitud = getattr(ev, "latitude", None), getattr(ev, "longitude", None)
        logger.warning(
            f"[{(proveedor or '?').upper()}-{env or '?'}] Evento descartado, NO se envía a RC: "
            f"falta {', '.join(falta)} | patente={patente or 'sin patente'}"
            f"{f' serie={serie}' if serie else ''} código={getattr(ev, 'code', None)}"
            f"{descartes.texto_coordenadas(latitud, longitud)}"
        )
        # Además de la consola, al registro persistente que muestra el panel.
        descartes.registrar(
            proveedor, env, "contrato", f"falta {', '.join(falta)}",
            equipo=patente or (f"serie {serie}" if serie else None),
            envio=getattr(ev, "shipment", None),
            detalle=f"código={getattr(ev, 'code', None)}",
            latitud=latitud, longitud=longitud,
        )
    return validos
