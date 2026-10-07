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
        Texto para el panel.
    opciones_efectivas(module_options) -> dict
        Los interruptores guardados, completados con los valores por defecto.
"""
import importlib

_MODULOS = {
    "tive": "app.providers.tive.modulo",
}


def modulo_dedicado(provider_name: str | None):
    """El módulo del proveedor, o None si usa el mapeador dinámico."""
    ruta = _MODULOS.get((provider_name or "").strip().lower())
    return importlib.import_module(ruta) if ruta else None


def es_dedicado(provider_name: str | None) -> bool:
    return (provider_name or "").strip().lower() in _MODULOS
