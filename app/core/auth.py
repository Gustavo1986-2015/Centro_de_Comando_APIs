"""Auth compartido para todos los routers del dashboard y admin."""
import os
import secrets
import logging
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)
security = HTTPBasic()


def get_dashboard_password() -> str:
    """
    Contraseña del panel, con una única fuente de verdad.

    Estaba duplicada con tres valores distintos según el archivo ("changeme",
    "admin", ""), así que la protección real dependía de por dónde entrara la
    petición. Centralizarla evita que una ruta quede con un default débil.

    Sin la variable definida devuelve cadena vacía, y compare_digest contra
    vacío rechaza cualquier contraseña: falla cerrado en lugar de dejar el
    panel accesible con una credencial adivinable.
    """
    return os.getenv("DASHBOARD_PASSWORD", "")


def get_dashboard_user() -> str:
    return os.getenv("DASHBOARD_USER", "admin")


def verificar_credenciales_al_arrancar():
    """
    Comprueba la configuración de acceso durante el arranque.

    En producción, arrancar sin contraseña dejaría expuestos el panel, el visor
    de base de datos y las credenciales de los proveedores. Es preferible no
    levantar el servicio antes que levantarlo sin protección: un fallo de
    arranque se ve enseguida, un panel abierto puede pasar semanas inadvertido.
    """
    entorno = os.getenv("APP_ENV", "development").lower()
    password = get_dashboard_password()

    if not password:
        if entorno == "production":
            raise RuntimeError(
                "DASHBOARD_PASSWORD no está definida y APP_ENV=production. "
                "El panel, el visor de base de datos y las credenciales de los "
                "proveedores quedarían accesibles. Definí la variable en el .env "
                "antes de iniciar el servicio."
            )
        logger.warning(
            "DASHBOARD_PASSWORD no está definida: el acceso al panel quedará "
            "bloqueado (se rechaza cualquier contraseña). Definila en el .env."
        )
    elif len(password) < 8:
        logger.warning(
            "DASHBOARD_PASSWORD tiene menos de 8 caracteres. El panel expone "
            "configuración de proveedores y edición de base de datos."
        )


def verify_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Valida HTTP Basic Auth con secrets.compare_digest (anti timing attack)."""
    correct_user = get_dashboard_user()
    correct_pass = get_dashboard_password()

    user_ok = secrets.compare_digest(credentials.username.encode(), correct_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), correct_pass.encode())

    # Sin contraseña configurada no hay acceso posible: compare_digest contra
    # cadena vacía es False para cualquier entrada no vacía, y una contraseña
    # vacía tampoco debe habilitar el ingreso.
    if not correct_pass or not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials
