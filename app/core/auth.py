"""Auth compartido para todos los routers del dashboard y admin."""
import os
import secrets
import logging
import threading
import time
from collections import deque
from fastapi import Depends, HTTPException, Request
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

    # B-16 (v1.9.9): sin clave de cifrado en producción el hub ARRANCA igual
    # (no puede dejar de recibir), pero no guarda credenciales nuevas en claro:
    # el panel lo rechaza con el motivo (app/core/crypto.py).
    from app.core.crypto import produccion_sin_clave
    if produccion_sin_clave():
        logger.critical(
            "******** MASTER_ENC_KEY NO ESTÁ DEFINIDA EN PRODUCCIÓN ******** "
            "El hub arranca y sigue recibiendo, y usa las credenciales ya guardadas. "
            "Pero NO va a guardar ninguna credencial nueva o modificada: quedaría sin "
            "cifrar. El panel lo rechaza. Definí MASTER_ENC_KEY en el .env (la MISMA que "
            "cifró las credenciales actuales) y reiniciá."
        )


# ── Ingresos fallidos (v1.9.12, D) ───────────────────────────────────────────
# Cada contraseña incorrecta queda en el log (sin la contraseña) y la respuesta
# se demora un segundo. NO se bloquea por IP: en el servidor todas las
# peticiones llegan desde la IP del proxy (172.18.0.x), así que bloquear la IP
# dejaría afuera a todos, operador incluido. La demora corre en el threadpool
# (estas dependencias son `def`, no `async def`): no frena el bucle de eventos
# ni la ingesta.
DEMORA_FALLO_SEG = 1.0
VENTANA_FALLOS_SEG = 600
_fallos_recientes: deque = deque()
_fallos_lock = threading.Lock()


def _contar_fallo() -> int:
    ahora = time.monotonic()
    with _fallos_lock:
        _fallos_recientes.append(ahora)
        while _fallos_recientes and ahora - _fallos_recientes[0] > VENTANA_FALLOS_SEG:
            _fallos_recientes.popleft()
        return len(_fallos_recientes)


def registrar_ingreso_fallido(request: Request | None, usuario: str, que: str = "credenciales") -> None:
    """WARNING con ruta, IP de conexión, X-Forwarded-For (solo informativo),
    el usuario intentado (32 caracteres como mucho) y los fallos de los
    últimos 10 minutos. Nunca la contraseña. Después, la demora."""
    fallos = _contar_fallo()
    ruta = request.url.path if request is not None else "-"
    ip = (request.client.host if request is not None and request.client else None) or "-"
    xff = request.headers.get("x-forwarded-for") if request is not None else None
    logger.warning(
        "Ingreso fallido al panel (%s): ruta=%s ip=%s x-forwarded-for=%s usuario=%r "
        "fallos_ultimos_10_min=%d",
        que, ruta, ip, (xff or "-")[:200], (usuario or "")[:32], fallos,
    )
    time.sleep(DEMORA_FALLO_SEG)


def _credenciales_correctas(credentials: HTTPBasicCredentials) -> bool:
    correct_user = get_dashboard_user()
    correct_pass = get_dashboard_password()

    user_ok = secrets.compare_digest(credentials.username.encode(), correct_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), correct_pass.encode())

    # Sin contraseña configurada no hay acceso posible: compare_digest contra
    # cadena vacía es False para cualquier entrada no vacía, y una contraseña
    # vacía tampoco debe habilitar el ingreso.
    return bool(correct_pass) and user_ok and pass_ok


def _comprobar_credenciales(credentials: HTTPBasicCredentials, request: Request | None):
    if not _credenciales_correctas(credentials):
        registrar_ingreso_fallido(request, credentials.username)
        raise HTTPException(
            status_code=401,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )


def contrasena_correcta(request: Request | None, contrasena: str | None) -> bool:
    """Revalidación de la contraseña del panel dentro de una acción (alta y baja
    de la verificación en dos pasos). Un fallo cuenta como ingreso fallido."""
    esperada = get_dashboard_password()
    ok = bool(esperada) and secrets.compare_digest((contrasena or "").encode(), esperada.encode())
    if not ok:
        registrar_ingreso_fallido(request, get_dashboard_user(), que="revalidación de contraseña")
    return ok


def verify_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security),
                          request: Request = None):
    """
    Valida HTTP Basic Auth con secrets.compare_digest (anti timing attack) y,
    si la verificación en dos pasos está activa, la sesión verificada
    (app/core/dos_pasos.py). Apagada, se comporta como en la v1.9.11.

    `request` va segundo y con valor por defecto: hay código y tests que la
    llaman directo con las credenciales.
    """
    _comprobar_credenciales(credentials, request)
    if request is not None:
        from app.core import dos_pasos
        dos_pasos.exigir(request)
    return credentials


def verify_dashboard_password(credentials: HTTPBasicCredentials = Depends(security),
                              request: Request = None):
    """Solo la contraseña, sin la verificación en dos pasos: la usan la página
    que la pide (/auth/verificar) y la salida (/auth/salir)."""
    _comprobar_credenciales(credentials, request)
    return credentials
