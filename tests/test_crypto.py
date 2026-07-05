"""Tests de app/core/crypto.py — Envelope Encryption (Fernet AES-128-CBC + HMAC-SHA256).

Cubre:
  - get_master_key() retorna la key de test (env var seteada en conftest.py)
  - encrypt/decrypt son reversibles
  - encrypt(None) y encrypt("") no lanzan excepción
  - decrypt de token inválido retorna None (no lanza)
  - is_encrypted detecta correctamente el prefijo Fernet
"""
import pytest


def test_get_master_key_returns_env_value():
    """get_master_key() debe retornar el valor de MASTER_ENC_KEY del entorno de test."""
    import os
    import app.core.crypto as crypto
    crypto._MASTER_KEY_CACHE = None  # Resetear caché para forzar re-lectura

    result = crypto.get_master_key()
    # Debe coincidir con lo que conftest.py seteó en el entorno
    assert result == os.environ["MASTER_ENC_KEY"]
    assert len(result) > 0, "La master key no debe estar vacía"


def test_encrypt_decrypt_reversible():
    """encrypt() -> decrypt() debe recuperar el plaintext original."""
    from app.core.crypto import encrypt, decrypt, is_encrypted

    plaintext = "sk_schmitz_8f3a9b2c7e1d4056"
    ciphertext = encrypt(plaintext)

    assert ciphertext != plaintext, "El ciphertext no debe ser igual al plaintext"
    assert is_encrypted(ciphertext) is True, "El ciphertext debe identificarse como cifrado"
    assert decrypt(ciphertext) == plaintext, "decrypt(encrypt(x)) debe retornar x"


def test_encrypt_none_and_empty_passthrough():
    """encrypt(None) retorna None, encrypt('') retorna ''  — sin excepción."""
    from app.core.crypto import encrypt

    assert encrypt(None) is None
    assert encrypt("") == ""


def test_decrypt_invalid_token_returns_none():
    """decrypt() con token corrupto/inválido debe retornar None, no lanzar excepción."""
    from app.core.crypto import decrypt

    result = decrypt("gAAAAAB_invalid_corrupt_data_here_XXXXXXXX")
    assert result is None, "Un token inválido debe retornar None, no lanzar excepción"


def test_is_encrypted_detects_fernet_prefix():
    """is_encrypted() reconoce el prefijo gAAAAAB como ciphertext Fernet."""
    from app.core.crypto import is_encrypted

    assert is_encrypted("gAAAAABsomething_valid_looking") is True
    assert is_encrypted("plaintext_password_123") is False
    assert is_encrypted(None) is False
    assert is_encrypted("") is False
