"""
Registro de módulos dedicados para la ruta /webhook/dynamic/{proveedor}.

Un proveedor puede necesitar lógica que el Integration Studio no expresa
(Tive: patentes aprendidas, apertura y cierre de alertas, duplicados con
identificadores distintos). En vez de poner esa lógica en el endpoint
genérico, el endpoint consulta este registro: si el proveedor tiene módulo,
le entrega el payload; si no, sigue con el mapeador dinámico de siempre.

La URL no cambia, así que el proveedor no se reconfigura. Autenticación,
auditoría de crudos, red de seguridad, cola y envío a RC siguen siendo los
comunes.

CONTRATO DE UN MÓDULO

    procesar(payload, env, module_options, ingest_id) -> list[RCCanonicalModel]
        Eventos a encolar, ya validados contra el contrato de RC. Lista vacía
        si no hay nada que enviar. Cada descarte, en consola.
    INTERRUPTORES: dict[str, bool]
        Opciones del módulo con su valor por defecto.
    DESCRIPCION_INTERRUPTORES: dict[str, str]
        Texto corto de cada interruptor (consola, respaldo y panel viejo).
    opciones_efectivas(module_options) -> dict
        Los interruptores guardados, completados con los valores por defecto.

OPCIONAL (v1.9.11): CÓMO SE VE EN CONFIGURACIÓN

El panel dibuja "Opciones de <proveedor>" desde lo que declara el módulo, sin
saber nada del proveedor:

    DETALLE_INTERRUPTORES: dict[str, dict]
        Por interruptor: {"grupo": str, "titulo": str, "ayuda": str,
        "duplica_rc": bool}. El título dice qué hace al tildarlo; la ayuda,
        qué pasa encendido y apagado; duplica_rc marca con ⚠ los que mandan
        algo que RC ya recibe por otro camino.
    GRUPOS_INTERRUPTORES: list[str]
        Orden de las secciones.
    USA_CREDENCIALES_API: bool / GRUPO_CREDENCIALES: str
        Si el módulo usa usuario y secreto de la API del proveedor, y el
        título de esa sección.

Un interruptor sin detalle se muestra en la sección "Opciones" con su
DESCRIPCION_INTERRUPTORES como título (ver detalle_para_panel).
"""
import importlib

GRUPO_POR_DEFECTO = "Opciones"

_MODULOS = {
    "tive": "app.providers.tive.modulo",
}


def modulo_dedicado(provider_name: str | None):
    """El módulo del proveedor, o None si usa el mapeador dinámico."""
    ruta = _MODULOS.get((provider_name or "").strip().lower())
    return importlib.import_module(ruta) if ruta else None


def es_dedicado(provider_name: str | None) -> bool:
    return (provider_name or "").strip().lower() in _MODULOS


def detalle_para_panel(modulo) -> tuple[list[dict], list[str]]:
    """
    v1.9.11: los interruptores del módulo como los dibuja Configuración, en el
    orden en que los declara DETALLE_INTERRUPTORES (después, los que no tengan
    detalle), y el orden de las secciones.
    """
    declarados = getattr(modulo, "DETALLE_INTERRUPTORES", None) or {}
    descripciones = getattr(modulo, "DESCRIPCION_INTERRUPTORES", None) or {}
    orden = [c for c in declarados if c in modulo.INTERRUPTORES]
    orden += [c for c in modulo.INTERRUPTORES if c not in orden]
    salida = []
    for clave in orden:
        d = declarados.get(clave) or {}
        salida.append({
            "clave": clave,
            "grupo": d.get("grupo") or GRUPO_POR_DEFECTO,
            "titulo": d.get("titulo") or descripciones.get(clave) or clave,
            "ayuda": d.get("ayuda") or "",
            "duplica_rc": bool(d.get("duplica_rc")),
        })
    grupos = list(getattr(modulo, "GRUPOS_INTERRUPTORES", None) or [])
    for d in salida:
        if d["grupo"] not in grupos:
            grupos.append(d["grupo"])
    return salida, grupos
