"""Tests de migraciones idempotentes en app/database.py.

v1.9.8: antes estos cuatro tests buscaban texto en el código fuente de
check_and_migrate_db ("PRAGMA table_info", "except"...), y por eso no
podían fallar aunque la migración rompiera algo: la auditoría encontró en
esa función el bug que borraba la configuración PULL al reiniciar (B-2).
Ahora cada uno EJECUTA la migración sobre una base armada a mano, con los
mismos nombres:
  - una base vieja recibe las columnas que le faltan (migración incremental)
  - correrla dos veces no rompe nada ni cambia los datos (idempotente)
  - siembra system_settings si la tabla está vacía
  - migra el SCHMITZ_API_KEY desde el entorno a la base cifrada
"""
import os
import sqlite3

import pytest


@pytest.fixture
def base_vieja(tmp_path, monkeypatch):
    """Una base de configuración con el esquema mínimo de una versión vieja."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("db")
    ruta = os.path.join("db", "system_config_global.db")
    con = sqlite3.connect(ruta)
    con.executescript("""
        CREATE TABLE provider_config (
            id INTEGER PRIMARY KEY, provider_name TEXT, env TEXT, is_active BOOLEAN,
            rc_user TEXT, rc_password TEXT, purge_interval_min INTEGER
        );
        CREATE TABLE system_settings (id INTEGER PRIMARY KEY, queue_backend TEXT);
        CREATE TABLE daily_stats (id INTEGER PRIMARY KEY, date DATE, provider TEXT, env TEXT,
                                  sent_count INTEGER, failed_count INTEGER);
        INSERT INTO provider_config (provider_name, env, is_active, rc_user, rc_password, purge_interval_min)
            VALUES ('schmitz', 'prod', 1, 'AC_avl_SchmitzCargoBull', '', 15),
                   ('protrack', 'prod', 1, 'AC_avl_Protrack', '', 15);
    """)
    con.commit()
    con.close()
    return ruta


def _columnas(ruta, tabla):
    con = sqlite3.connect(ruta)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({tabla})")]
    finally:
        con.close()


def _filas(ruta, consulta):
    con = sqlite3.connect(ruta)
    try:
        return con.execute(consulta).fetchall()
    finally:
        con.close()


def test_migrate_uses_pragma_table_info(base_vieja):
    """Una base vieja recibe las columnas que le faltan, sin perder sus filas."""
    from app.database import check_and_migrate_db
    check_and_migrate_db()
    columnas = _columnas(base_vieja, "provider_config")
    for nueva in ("run_interval_sec", "use_mock", "mapping_schema", "fetch_config", "fetch_config_enc",
                  "rc_password_enc", "webhook_auth_secret_enc", "webhook_auth_header",
                  "enable_state_dedup", "provider_type", "rate_limit_per_min", "queue_backend",
                  "webhook_auth_config", "module_options"):
        assert nueva in columnas, f"la migración no agregó {nueva}"
    for nueva in ("avg_transmission_latency_sec", "avg_hub_latency_sec", "avg_rc_latency_sec"):
        assert nueva in _columnas(base_vieja, "daily_stats")
    assert _filas(base_vieja, "SELECT provider_name, rc_user FROM provider_config ORDER BY id") == [
        ("schmitz", "AC_avl_SchmitzCargoBull"), ("protrack", "AC_avl_Protrack")]
    # Schmitz queda reclasificado como PUSH (la migración lo corrige).
    assert _filas(base_vieja, "SELECT provider_type FROM provider_config WHERE provider_name='schmitz'") == [("push",)]


def test_migrate_is_idempotent_via_exception_handling(base_vieja):
    """Correrla dos veces da el mismo esquema y los mismos datos."""
    from app.database import check_and_migrate_db
    check_and_migrate_db()
    esquema = {t: _columnas(base_vieja, t) for t in ("provider_config", "system_settings", "daily_stats")}
    datos = _filas(base_vieja, "SELECT * FROM provider_config ORDER BY id")
    ajustes = _filas(base_vieja, "SELECT * FROM system_settings")
    check_and_migrate_db()
    assert {t: _columnas(base_vieja, t) for t in esquema} == esquema
    assert _filas(base_vieja, "SELECT * FROM provider_config ORDER BY id") == datos
    assert _filas(base_vieja, "SELECT * FROM system_settings") == ajustes


def test_migrate_seeds_system_settings(base_vieja):
    """Con system_settings vacía, queda una fila con los valores por defecto."""
    from app.database import check_and_migrate_db
    assert _filas(base_vieja, "SELECT COUNT(*) FROM system_settings") == [(0,)]
    check_and_migrate_db()
    (fila,) = _filas(base_vieja, "SELECT audit_retention_days, processed_retention_days, "
                                 "processed_logs_enabled FROM system_settings")
    assert fila == (30, 30, 1)


def test_migrate_has_schmitz_legacy_env_migration(base_vieja, monkeypatch):
    """SCHMITZ_API_KEY del entorno pasa a la base, cifrado, si Schmitz no tenía clave."""
    from app.core.crypto import decrypt
    from app.database import check_and_migrate_db
    monkeypatch.setenv("SCHMITZ_API_KEY", "clave-legada-del-env")
    check_and_migrate_db()
    ((cifrada,),) = _filas(base_vieja, "SELECT webhook_auth_secret_enc FROM provider_config "
                                       "WHERE provider_name='schmitz'")
    assert cifrada and cifrada != "clave-legada-del-env", "quedó en texto plano"
    assert decrypt(cifrada) == "clave-legada-del-env"
    # Protrack no tiene nada que ver con esa variable.
    assert _filas(base_vieja, "SELECT webhook_auth_secret_enc FROM provider_config "
                              "WHERE provider_name='protrack'") == [(None,)]
