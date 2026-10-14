# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_auditoria_fixes.py (reorganización por tema: seguridad).
"""
Tests de los hallazgos M2, M3 y M5 de la auditoría.

Los tres afectan la calidad de lo que llega a Recurso Confiable o la protección
del panel, y ninguno estaba cubierto.
"""
import os
import pytest
from unittest.mock import MagicMock, patch

from app.core.dynamic_mapper import DynamicMapper, _a_booleano
from app.core.auth import (
    get_dashboard_password,
    verificar_credenciales_al_arrancar,
)


# ═══════════════════════════════════════════════════════════════════
# M5 — Contraseña del panel: fuente única y arranque protegido
# ═══════════════════════════════════════════════════════════════════

def test_sin_variable_no_hay_contrasena_adivinable(monkeypatch):
    """
    REGRESIÓN: había tres defaults distintos ("changeme", "admin", "") según el
    archivo, así que la protección dependía de por dónde entrara la petición.
    """
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert get_dashboard_password() == ""


def test_la_contrasena_configurada_se_respeta(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "clave_real_2026")
    assert get_dashboard_password() == "clave_real_2026"


def test_produccion_sin_contrasena_no_arranca(monkeypatch):
    """
    Un panel abierto puede pasar semanas inadvertido; un fallo de arranque se
    ve de inmediato. Expone configuración de proveedores y edición de base.
    """
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        verificar_credenciales_al_arrancar()


def test_desarrollo_sin_contrasena_arranca_con_aviso(monkeypatch):
    """En desarrollo no debe bloquear el trabajo, pero sí advertir."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    verificar_credenciales_al_arrancar()   # no debe lanzar


def test_produccion_con_contrasena_arranca(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "una_clave_larga_y_segura")

    verificar_credenciales_al_arrancar()   # no debe lanzar


def test_el_acceso_queda_bloqueado_sin_contrasena_configurada(monkeypatch):
    """
    Sin contraseña no debe haber ninguna combinación que abra el panel,
    tampoco enviando una contraseña vacía.
    """
    from fastapi import HTTPException
    from fastapi.security import HTTPBasicCredentials
    from app.core.auth import verify_dashboard_auth

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DASHBOARD_USER", "admin")

    for intento in ("", "changeme", "admin", "cualquiera"):
        with pytest.raises(HTTPException) as exc:
            verify_dashboard_auth(HTTPBasicCredentials(username="admin", password=intento))
        assert exc.value.status_code == 401
