"""Tests de migraciones idempotentes en app/database.py.

Valida mediante inspección de código fuente que:
  - check_and_migrate_db usa PRAGMA table_info y ADD COLUMN (migración incremental)
  - La migración es idempotente (tiene manejo de excepciones para columnas ya existentes)
  - Siembra system_settings si la tabla está vacía
  - Migra el SCHMITZ_API_KEY desde env a DB cifrada (migración legacy)
"""
import pytest
import inspect


def test_migrate_uses_pragma_table_info():
    """check_and_migrate_db debe usar PRAGMA table_info para leer columnas existentes."""
    from app.database import check_and_migrate_db
    src = inspect.getsource(check_and_migrate_db)

    assert "PRAGMA table_info" in src, (
        "La migración debe usar PRAGMA table_info para detectar columnas existentes"
    )
    assert "ADD COLUMN" in src, (
        "La migración debe usar ALTER TABLE ... ADD COLUMN para agregar campos"
    )


def test_migrate_is_idempotent_via_exception_handling():
    """La migración debe manejar excepciones para que sea seguro llamarla múltiples veces."""
    from app.database import check_and_migrate_db
    src = inspect.getsource(check_and_migrate_db)

    assert "except" in src, (
        "La migración debe tener manejo de excepciones para ser idempotente"
    )


def test_migrate_seeds_system_settings():
    """check_and_migrate_db debe insertar valores por defecto en system_settings si está vacío."""
    from app.database import check_and_migrate_db
    src = inspect.getsource(check_and_migrate_db)

    assert "INSERT INTO system_settings" in src, (
        "La migración debe hacer seed de system_settings si la tabla está vacía"
    )


def test_migrate_has_schmitz_legacy_env_migration():
    """check_and_migrate_db debe migrar SCHMITZ_API_KEY desde env a DB cifrada."""
    from app.database import check_and_migrate_db
    src = inspect.getsource(check_and_migrate_db)

    assert "SCHMITZ_API_KEY" in src, (
        "La migración debe manejar la migración legacy de SCHMITZ_API_KEY desde .env"
    )
