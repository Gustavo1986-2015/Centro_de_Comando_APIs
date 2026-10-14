"""
Siembra de integraciones (v1.9.9, B-21): un solo lugar, un solo juego de valores.

Antes había tres lugares con valores distintos:
  · el worker (processor.py) creaba 4 filas con los valores por defecto de la
    columna: Schmitz nacía PULL y lanzaba un sondeo hasta el siguiente reinicio;
    y en una base EXISTENTE le agregaba protrack/prod o protrack/test si faltaban;
  · el panel (GET /api/config) creaba schmitz/prod y schmitz/test con otros;
  · el tablero (dashboard.py) devolvía dos filas fantasma de Schmitz.

Ahora:
  · solo se siembra una base VACÍA (sin ninguna integración). Una base
    existente no recibe ni cambia ninguna fila;
  · los valores son estos, y nada más: Schmitz es PUSH (sin deduplicación de
    estado), Protrack es PULL (con deduplicación). Todas nacen activas y EN
    MODO SIMULADO: nada llega a Recurso Confiable hasta que el operador cargue
    las credenciales y lo pase a real desde el panel.
"""
import logging

logger = logging.getLogger(__name__)

INTEGRACIONES_INICIALES = (
    {"provider_name": "schmitz", "env": "prod", "provider_type": "push", "enable_state_dedup": False},
    {"provider_name": "schmitz", "env": "test", "provider_type": "push", "enable_state_dedup": False},
    {"provider_name": "protrack", "env": "prod", "provider_type": "pull", "enable_state_dedup": True},
    {"provider_name": "protrack", "env": "test", "provider_type": "pull", "enable_state_dedup": True},
)
VALORES_COMUNES = {"is_active": True, "use_mock": True}


def sembrar_si_vacia(db) -> bool:
    """
    Crea las integraciones iniciales si la base no tiene NINGUNA. Devuelve si
    sembró. `db` es una sesión de system_config.
    """
    from app.models.config_models import ProviderConfig

    if db.query(ProviderConfig).count():
        return False
    db.add_all([ProviderConfig(**VALORES_COMUNES, **fila) for fila in INTEGRACIONES_INICIALES])
    db.commit()
    logger.info(
        "Base de configuración nueva: se crearon las integraciones iniciales "
        f"({', '.join(f'{f['provider_name']}/{f['env']} {f['provider_type'].upper()}' for f in INTEGRACIONES_INICIALES)}), "
        "todas en MODO SIMULADO."
    )
    return True
