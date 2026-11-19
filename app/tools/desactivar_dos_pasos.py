"""
Acceso de emergencia (v1.9.12): apaga la verificación en dos pasos del panel.

Para cuando no hay teléfono ni códigos de recuperación. Se corre DENTRO del
contenedor, que es lo mismo que decir: solo quien tiene acceso al servidor.

    docker exec centro-comando-app python -m app.tools.desactivar_dos_pasos

Borra el secreto, los códigos de recuperación y la llave de las sesiones, y
deja la verificación apagada (el panel vuelve a pedir solo la contraseña). El
hub en marcha lo toma en 5 segundos como mucho, sin reiniciar. Queda un
CRITICAL en el log. Se puede volver a activar desde el panel.
"""
import logging
import sys


def main() -> int:
    from app.core.logging_config import setup_logging
    setup_logging()
    log = logging.getLogger("app.tools.desactivar_dos_pasos")

    from app.core import dos_pasos
    try:
        antes = dos_pasos.resumen()["activo"]
        dos_pasos.desactivar("comando de emergencia app.tools.desactivar_dos_pasos")
    except Exception as e:
        log.error(f"No se pudo desactivar la verificación en dos pasos: {type(e).__name__}: {e}")
        print(f"ERROR: no se pudo desactivar ({type(e).__name__}). Revisá que se corra en /app, "
              "donde está db/system_config_global.db.", file=sys.stderr)
        return 1

    log.critical(
        "ACCESO DE EMERGENCIA: se desactivó la verificación en dos pasos del panel desde la "
        "consola del servidor (python -m app.tools.desactivar_dos_pasos). Estaba "
        f"{'activa' if antes else 'ya inactiva'}. Secreto, códigos de recuperación y sesiones borrados.")
    print("Verificación en dos pasos DESACTIVADA. El panel vuelve a pedir solo la contraseña "
          "(el hub en marcha lo toma en 5 s). Se puede activar de nuevo desde Configuración → Seguridad.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
