"""
Envelope encryption para credenciales de proveedores.
Usa Fernet (AES-128-CBC + HMAC-SHA256) con una llave maestra en .env.
"""
import os
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_MASTER_KEY_CACHE = None

def get_master_key() -> str:
    """
    Obtiene la llave maestra Fernet desde la variable de entorno MASTER_ENC_KEY.

    NO auto-genera la llave. En un despliegue con contenedores, generar una llave
    nueva cuando falta la variable deja ilegibles TODAS las credenciales cifradas
    de los proveedores (RC, webhooks, cuentas de AVL) sin ningún error evidente:
    la app arranca bien y los proveedores simplemente dejan de autenticar.

    Es preferible fallar al arrancar con un mensaje claro.
    """
    global _MASTER_KEY_CACHE
    if _MASTER_KEY_CACHE:
        return _MASTER_KEY_CACHE

    key = os.getenv("MASTER_ENC_KEY")
    if not key:
        raise RuntimeError(
            "MASTER_ENC_KEY no está definida en el entorno. Es obligatoria para "
            "cifrar y descifrar las credenciales de los proveedores.\n"
            "  - En local: agregala al archivo .env\n"
            "  - En Docker: verificá que .env esté en env_file y contenga la clave\n"
            "Para generar una llave NUEVA (solo en una instalación limpia, sin "
            "credenciales ya cifradas):\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

    # Validar el formato antes de cachear: una llave malformada produciría
    # errores confusos recién al primer cifrado/descifrado.
    try:
        Fernet(key.encode())
    except Exception as e:
        raise RuntimeError(
            f"MASTER_ENC_KEY tiene un formato inválido para Fernet: {e}. "
            "Debe ser una clave base64 url-safe de 32 bytes."
        )

    _MASTER_KEY_CACHE = key
    return key


def _get_fernet() -> Fernet:
    return Fernet(get_master_key().encode())

def encrypt(plaintext) -> str:
    """Cifra un string. Retorna None/empty si input es None/vacio."""
    if not plaintext:
        return plaintext
    try:
        return _get_fernet().encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error(f"Error cifrando campo: {e}")
        raise

def decrypt(ciphertext) -> str:
    """Descifra un string. Retorna None si input es None/vacio.
    Si no puede descifrar (clave rotada), retorna None y loguea warning.
    Si NO parece ciphertext Fernet (no empieza con gAAAAAB), asumir plaintext legacy."""
    if not ciphertext:
        return ciphertext
    # Si no parece ciphertext Fernet, asumir plaintext legacy
    if not ciphertext.startswith("gAAAAAB"):
        return ciphertext
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.warning("Campo cifrado no pudo ser descifrado (clave rotada?).")
        return None

def is_encrypted(value) -> bool:
    """Detecta si un string parece ciphertext de Fernet."""
    if not value:
        return False
    return value.startswith("gAAAAAB")
