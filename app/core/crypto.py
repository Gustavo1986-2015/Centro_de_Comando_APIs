"""
Envelope encryption para credenciales de proveedores.
Usa Fernet (AES-128-CBC + HMAC-SHA256) con una llave maestra en .env.
"""
import os
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_MASTER_KEY_CACHE = None
_NO_KEY_WARNED = False

def get_master_key() -> str | None:
    """
    Obtiene la llave maestra Fernet desde la variable de entorno MASTER_ENC_KEY.

    Devuelve None si no está definida: en ese caso el sistema opera SIN cifrado
    (las credenciales se guardan y leen en texto plano).

    Lo que NUNCA hace es auto-generar una llave. Generar una llave nueva cuando
    falta la variable deja ilegibles todas las credenciales cifradas con la
    llave anterior, sin ningún error evidente: la app arranca bien y los
    proveedores simplemente dejan de autenticar.
    """
    global _MASTER_KEY_CACHE, _NO_KEY_WARNED
    if _MASTER_KEY_CACHE:
        return _MASTER_KEY_CACHE

    key = os.getenv("MASTER_ENC_KEY")
    if not key:
        if not _NO_KEY_WARNED:
            logger.warning(
                "MASTER_ENC_KEY no está definida: las credenciales de proveedores "
                "se guardarán SIN cifrar. Para habilitar el cifrado, definí la "
                "variable en .env. Generar una nueva (solo en instalación limpia): "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
            _NO_KEY_WARNED = True
        return None

    # Validar el formato antes de cachear: una llave malformada produciría
    # errores confusos recién al primer cifrado o descifrado.
    try:
        Fernet(key.encode())
    except Exception as e:
        raise RuntimeError(
            f"MASTER_ENC_KEY tiene un formato inválido para Fernet: {e}. "
            "Debe ser una clave base64 url-safe de 32 bytes. "
            "Corregila o quitala del entorno para operar sin cifrado."
        )

    _MASTER_KEY_CACHE = key
    return key


def _get_fernet() -> Fernet | None:
    """Instancia de Fernet, o None si el sistema opera sin cifrado."""
    key = get_master_key()
    return Fernet(key.encode()) if key else None

def encrypt(plaintext) -> str:
    """Cifra un string. Retorna None/empty si input es None/vacio."""
    if not plaintext:
        return plaintext
    f = _get_fernet()
    if f is None:
        # Sin llave configurada: se guarda en claro. decrypt() lo devolverá
        # tal cual porque no tiene el prefijo de ciphertext Fernet.
        return plaintext
    try:
        return f.encrypt(plaintext.encode()).decode()
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

    f = _get_fernet()
    if f is None:
        # Hay datos cifrados pero no hay llave para descifrarlos. Suele indicar
        # que se quitó MASTER_ENC_KEY de un entorno que ya tenía credenciales
        # cifradas: hay que restaurar la llave original.
        logger.error(
            "Se encontró un valor cifrado pero MASTER_ENC_KEY no está definida. "
            "Restaurá la llave original para poder leer estas credenciales."
        )
        return None

    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        logger.warning("Campo cifrado no pudo ser descifrado (clave rotada?).")
        return None

class CredencialSinCifrar(Exception):
    """
    B-16 (v1.9.9): en producción sin MASTER_ENC_KEY no se guarda una
    credencial nueva o modificada: quedaría en texto plano. El mensaje es el
    que ve el operador en el panel.
    """


MENSAJE_SIN_CLAVE = (
    "No se guardó: el hub está en producción (APP_ENV=production) sin MASTER_ENC_KEY, "
    "y la credencial quedaría guardada SIN CIFRAR. Definí MASTER_ENC_KEY en el .env del "
    "servidor y reiniciá el hub; después volvé a cargarla. Lo ya guardado se sigue usando."
)


def produccion_sin_clave() -> bool:
    """True si APP_ENV=production y no hay MASTER_ENC_KEY."""
    return os.getenv("APP_ENV", "").strip().lower() == "production" and not os.getenv("MASTER_ENC_KEY")


def cifrar_credencial(plaintext) -> str:
    """
    Como encrypt(), para credenciales que se GUARDAN (panel, importación).

    B-16 (v1.9.9): en producción sin MASTER_ENC_KEY lanza CredencialSinCifrar
    en vez de guardar en claro. El hub arranca y sigue recibiendo igual: solo
    se niega a escribir una credencial nueva o modificada. Fuera de producción
    se comporta como encrypt() (desarrollo y tests pueden no tener clave).
    """
    if not plaintext:
        return plaintext
    if is_encrypted(plaintext):
        return plaintext
    if produccion_sin_clave():
        raise CredencialSinCifrar(MENSAJE_SIN_CLAVE)
    return encrypt(plaintext)


def is_encrypted(value) -> bool:
    """Detecta si un string parece ciphertext de Fernet."""
    if not value:
        return False
    return value.startswith("gAAAAAB")
