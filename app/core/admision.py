"""
Filtro de admisión: qué eventos de un proveedor entran al hub y cuáles no.

POR QUÉ EXISTE

Un proveedor puede mandar más de lo que se necesita. El caso que lo motivó:
el webhook de Tive se configuró para traer los envíos, pero también trae
alertas de equipos que no están en ningún envío, y además los ejemplos del
botón de prueba de Tive (cuenta -1, equipo SAMPLEDEVICEID). Esos ejemplos,
sin filtro, llegarían a RC como si fueran un camión real.

El filtro es transversal: se configura por integración, en el esquema de
mapeo, sin código específico de ningún proveedor.

FORMATO, dentro del esquema de mapeo:

    "admision": [
      {"field": "ShipmentId || Shipment.Id || Alert.ShipmentId",
       "operator": "exists", "label": "solo eventos con envío"},
      {"field": "AccountId", "operator": "neq", "value": "-1",
       "label": "descartar muestras del botón de prueba"}
    ]

Un evento entra solo si cumple TODAS las condiciones. Los campos aceptan
rutas anidadas y alternativas con '||', igual que el mapeo base. Los
operadores son los mismos de las reglas de disparo.

UN DESCARTE NUNCA ES SILENCIOSO

Cada evento descartado queda contado, con el motivo: el primero de cada
motivo se registra apenas ocurre, y los siguientes se resumen cada minuto.
Un filtro mal configurado que descarta todo se ve en la consola, no
desaparece el tráfico sin explicación.

Se registra como INFO y no como advertencia: descartar es el comportamiento
pedido, no una falla.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

OPERADORES = ("exists", "not_exists", "eq", "neq", "gt", "lt", "gte", "lte")

# Cada cuánto se resume la cantidad de descartes de un mismo motivo.
SEGUNDOS_RESUMEN = 60

_contadores: dict[tuple[str, str, str], dict] = {}
_lock = threading.Lock()


def evaluar(payload: dict, mapping_schema: dict | None) -> str | None:
    """
    Devuelve el motivo del descarte, o None si el evento entra.

    Sin condiciones configuradas, entra todo: es el comportamiento de siempre,
    y el de cualquier integración que no defina un filtro.
    """
    condiciones = (mapping_schema or {}).get("admision") or []
    if not condiciones:
        return None

    # Import tardío: el mapeador importa módulos del core al cargarse.
    from app.core.dynamic_mapper import DynamicMapper

    for i, cond in enumerate(condiciones, start=1):
        if not isinstance(cond, dict):
            return f"condición de admisión #{i} mal configurada: no es un objeto"
        campo = (cond.get("field") or "").strip()
        operador = (cond.get("operator") or "").strip()
        etiqueta = cond.get("label") or f"{campo} {operador} {cond.get('value', '')}".strip()

        # Una condición rota descarta, y lo dice. Dejar pasar todo ante una
        # configuración inválida sería el fallo silencioso que este filtro
        # existe para evitar.
        if not campo:
            return f"condición de admisión #{i} mal configurada: falta el campo"
        if operador not in OPERADORES:
            return (f"condición de admisión #{i} mal configurada: operador "
                    f"'{operador}' desconocido (válidos: {', '.join(OPERADORES)})")

        if not DynamicMapper._evaluate_rule(payload, campo, operador, cond.get("value", "")):
            return etiqueta

    return None


def registrar_descarte(provider: str, env: str, motivo: str) -> None:
    """Cuenta el descarte y lo registra sin inundar la consola."""
    clave = (provider.lower(), env.lower(), motivo)
    ahora = time.time()
    with _lock:
        c = _contadores.get(clave)
        if c is None:
            _contadores[clave] = {"total": 1, "pendientes": 0, "ultimo_resumen": ahora,
                                  "desde": ahora}
            primero = True
        else:
            c["total"] += 1
            c["pendientes"] += 1
            primero = False
            resumir = (ahora - c["ultimo_resumen"]) >= SEGUNDOS_RESUMEN and c["pendientes"]
            if resumir:
                pendientes, total = c["pendientes"], c["total"]
                c["pendientes"] = 0
                c["ultimo_resumen"] = ahora

    etiqueta = f"[{provider.upper()}-{env}]"
    if primero:
        logger.info(f"{etiqueta} Evento descartado por el filtro de admisión: {motivo}.")
    elif resumir:
        logger.info(
            f"{etiqueta} {pendientes} evento(s) más descartados por el filtro de "
            f"admisión ({motivo}) en el último minuto. {total} en total."
        )


def resumen() -> list[dict]:
    """Descartes acumulados por integración y motivo."""
    with _lock:
        return [
            {"provider": p, "env": e, "motivo": m, "total": c["total"]}
            for (p, e, m), c in sorted(_contadores.items())
        ]


def reset():
    """Para los tests."""
    with _lock:
        _contadores.clear()
