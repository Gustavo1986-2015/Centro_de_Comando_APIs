"""
Registro de los cambios de modo simulado (v1.9.5).

El 05/10/2026 schmitz/prod pasó de simulado a REAL sin una sola línea en la
consola: el cambio no vino del panel sino de la suite de tests corrida sobre
la base en vivo, y el aviso solo existía dentro del endpoint del panel.

Por eso el aviso ya no depende de quién escribe: se engancha a la sesión de la
base de configuración y sale con CUALQUIER cambio de use_mock, venga del
panel, de la importación de YAML o de un script. Se emite DESPUÉS del commit:
un guardado que falla y se revierte no deja un aviso de algo que no pasó.

Cada línea lleva integración, valor anterior, valor nuevo, usuario y origen.
Quien escribe declara el usuario con `usuario(...)`; sin declararlo, el aviso
sale igual, marcado "sin usuario identificado".
"""
import contextvars
import logging
from contextlib import contextmanager

from sqlalchemy import event, inspect

logger = logging.getLogger(__name__)

SIN_USUARIO = "sin usuario identificado"

_quien = contextvars.ContextVar("modo_simulado_quien", default=None)
_CLAVE = "cambios_modo_simulado"


@contextmanager
def usuario(nombre: str, origen: str):
    """Declara quién hace los cambios dentro del bloque (panel, importación…)."""
    marca = _quien.set((nombre or SIN_USUARIO, origen))
    try:
        yield
    finally:
        _quien.reset(marca)


def _texto(valor) -> str:
    if valor is None:
        return "no existía"
    return "SIMULADO" if valor else "REAL"


def registrar(proveedor: str, env: str, anterior, nuevo, usuario_: str, origen: str) -> None:
    """Una línea por cambio. WARNING siempre: en los dos sentidos cambia qué
    llega a Recurso Confiable."""
    etiqueta = f"{proveedor}/{env}"
    if anterior is None:
        efecto = ("Se crea sin enviar a Recurso Confiable." if nuevo
                  else "Se crea ENVIANDO a Recurso Confiable.")
    elif nuevo:
        efecto = "Los eventos dejan de enviarse a Recurso Confiable."
    else:
        efecto = "Los eventos vuelven a enviarse a Recurso Confiable."
    accion = "ACTIVADO" if nuevo else "DESACTIVADO"
    logger.warning(
        f"MODO SIMULADO {accion} en {etiqueta}: {_texto(anterior)} -> {_texto(nuevo)} "
        f"| usuario={usuario_} | origen={origen}. {efecto}"
    )


def registrar_activacion(proveedor: str, env: str, anterior, nuevo, usuario_: str, origen: str) -> None:
    """
    v1.9.9 (B-13): activar o desactivar una integración también cambia qué
    llega a RC (inactiva = no procesa). Mismo aviso, con usuario y origen,
    venga del panel, del visor de BD o de una importación.
    """
    accion = "ACTIVADA" if nuevo else "DESACTIVADA"
    efecto = ("Vuelve a procesar sus eventos." if nuevo
              else "Deja de procesar: sus eventos no llegan a Recurso Confiable.")
    logger.warning(
        f"INTEGRACIÓN {accion}: {proveedor}/{env}: {'activa' if anterior else 'inactiva'} -> "
        f"{'activa' if nuevo else 'inactiva'} | usuario={usuario_} | origen={origen}. {efecto}"
    )


def _antes_del_flush(session, contexto, instancias):
    from app.models.config_models import ProviderConfig

    quien, origen = _quien.get() or (SIN_USUARIO, "código")
    pendientes = session.info.setdefault(_CLAVE, [])
    for obj in session.new:
        if isinstance(obj, ProviderConfig):
            # La columna vale True por defecto: sin valor explícito nace simulada.
            nuevo = True if obj.use_mock is None else bool(obj.use_mock)
            pendientes.append((obj.provider_name, obj.env, None, nuevo, quien, origen))
    for obj in session.dirty:
        if not isinstance(obj, ProviderConfig):
            continue
        activa = inspect(obj).attrs.is_active.history
        if activa.has_changes() and activa.deleted:
            anterior, nuevo = activa.deleted[0], activa.added[0] if activa.added else None
            if bool(anterior) != bool(nuevo):
                pendientes.append(("activacion", obj.provider_name, obj.env, bool(anterior), bool(nuevo),
                                   quien, origen))
        historia = inspect(obj).attrs.use_mock.history
        if not historia.has_changes() or not historia.deleted:
            continue
        anterior, nuevo = historia.deleted[0], historia.added[0] if historia.added else None
        if bool(anterior) != bool(nuevo):
            pendientes.append((obj.provider_name, obj.env, bool(anterior), bool(nuevo), quien, origen))


def _despues_del_commit(session):
    for cambio in session.info.pop(_CLAVE, []):
        if cambio[0] == "activacion":
            registrar_activacion(*cambio[1:])
        else:
            registrar(*cambio)


def _despues_del_rollback(session):
    session.info.pop(_CLAVE, None)


def instalar(fabrica_de_sesiones) -> None:
    """Engancha el registro a la fábrica de sesiones de system_config."""
    event.listen(fabrica_de_sesiones, "before_flush", _antes_del_flush)
    event.listen(fabrica_de_sesiones, "after_commit", _despues_del_commit)
    event.listen(fabrica_de_sesiones, "after_rollback", _despues_del_rollback)
