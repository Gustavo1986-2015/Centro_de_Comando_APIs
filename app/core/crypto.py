"""
Envelope encryption para credenciales de proveedores.
Usa Fernet (AES-128-CBC + HMAC-SHA256) con una llave maestra en .env.
"""
import os
import logging
from cryptography.fernet import Fernet, InvalidToken
from pathlib import Path

logger = logging.getLogger(__name__)

_MASTER_KEY_CACHE = None

def get_master_key() -> str:
    """
    Obtiene la llave maestra de .env. Si no existe, la genera automáticamente,
    la persiste en .env y la cachea en memoria.
    """
    global _MASTER_KEY_CACHE
    if _MASTER_KEY_CACHE:
        return _MASTER_KEY_CACHE
    
    key = os.getenv("MASTER_ENC_KEY")
    if not key:
        # Auto-generar
        key = Fernet.generate_key().decode()
        _persist_key_to_env(key)
        logger.info("MASTER_ENC_KEY generada automaticamente y guardada en .env")
        logger.warning("Backupea MASTER_ENC_KEY en un gestor de passwords seguro.")
    
    _MASTER_KEY_CACHE = key
    return key

def _persist_key_to_env(key: str):
    """Append la llave al .env (preservando contenido existente)."""
    env_path = Path(".env")
    marker = "# === Auto-generada por app/core/crypto.py ==="
    
    # Verificar si ya existe (race condition safety)
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "MASTER_ENC_KEY" in content:
            return
    
    with open(env_path, "a", encoding="utf-8") as f:
        f.write(f"\n{marker}\n")
        f.write(f"# Llave maestra para cifrar credenciales de proveedores en DB.\n")
        f.write(f"# SI LA PIERDES, todas las credenciales se vuelven ilegibles.\n")
        f.write(f"# Backupeala en un gestor de passwords seguro.\n")
        f.write(f"MASTER_ENC_KEY={key}\n")

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
