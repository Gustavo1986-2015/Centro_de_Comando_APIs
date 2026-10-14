# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v199_bloque4.py (reorganización por tema: proveedores/tive).
"""
v1.9.9 · Bloque 4 — Código y configuración.

4.1 B-21  Siembra de integraciones: un solo lugar y un solo juego de valores;
          una base existente no cambia ninguna fila.
4.2 B-23  Valores rc_* en un solo lugar; active_queue_backend fuera sin romper
          la importación de un YAML viejo.
4.4       Tabla de cola vacía en tive/{env}_estado.db: se quita solo si está vacía.

(4.3 B-25, código muerto, y 4.5, texto de .env.example: ver la ENTREGA.)
Todos EJECUTAN el código.
"""
import logging
import os
import sqlite3

import pytest
import yaml


# ═══════════════════════════════════════════════════════════════════════════
# 4.4 — Tabla de cola vacía en la base de estado de Tive
# ═══════════════════════════════════════════════════════════════════════════

def _base_de_estado_con_tabla_de_cola(tmp_path, filas):
    os.makedirs(tmp_path / "estado", exist_ok=True)
    ruta = tmp_path / "estado" / "prod_estado.db"
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE normalized_rc_events (id INTEGER PRIMARY KEY, status TEXT)")
    con.executemany("INSERT INTO normalized_rc_events (status) VALUES (?)", [("sent",)] * filas)
    con.commit()
    con.close()
    return ruta


def _tablas(ruta):
    con = sqlite3.connect(ruta)
    try:
        return sorted(t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        con.close()


def test_44_la_tabla_de_cola_vacia_se_quita(tmp_path, monkeypatch):
    from app.providers.tive import estado
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    ruta = _base_de_estado_con_tabla_de_cola(tmp_path, 0)
    try:
        estado.aprender_par("prod", "865648069052454", "K393478", "webhook")
        assert "normalized_rc_events" not in _tablas(ruta)
        estado.cerrar_todo()
        estado.aprender_par("prod", "865648069052454", "K393478", "webhook")   # idempotente
        assert "pares" in _tablas(ruta) and "normalized_rc_events" not in _tablas(ruta)
    finally:
        estado.cerrar_todo()


def test_44_con_filas_no_se_toca_y_se_avisa(tmp_path, monkeypatch, caplog):
    from app.providers.tive import estado
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    ruta = _base_de_estado_con_tabla_de_cola(tmp_path, 2)
    try:
        with caplog.at_level(logging.WARNING, logger="app.providers.tive.estado"):
            estado.aprender_par("prod", "865648069052454", "K393478", "webhook")
        assert "normalized_rc_events" in _tablas(ruta)
        assert "no se borra porque no está vacía" in caplog.text
    finally:
        estado.cerrar_todo()
