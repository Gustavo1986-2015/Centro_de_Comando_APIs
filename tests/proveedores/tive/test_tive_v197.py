# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v197.py (reorganización por tema: proveedores/tive).
"""
v1.9.7 — Arreglos previos al despliegue (auditoría completa del 06/10/2026).

  1. B-2: la migración de arranque borraba la configuración PULL. Con
     fetch_config guardado como el TEXTO 'null' (lo dejaban el panel y la
     importación al asignar None a la columna JSON), la migración lo cifraba
     ENCIMA de fetch_config_enc. Ahora: None se guarda como NULL de SQL, el
     texto vacío no se cifra y lo cifrado nunca se pisa.
  2. B-14: lo simulado ya no se guarda como 'sent'. Queda 'simulado' y no
     cuenta como enviado a RC (contador diario, latencia de RC), pero sí lo
     purgan la retención y el respaldo, y lo lista la descarga con su estado.

Todos los tests EJECUTAN el código (migración, worker, purga, endpoints y el
JavaScript del panel): ninguno busca texto en el fuente.
"""
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.core.crypto import decrypt, encrypt


# ═══════════════════════════════════════════════════════════════════════════
# 4. B-26 — El aviso de alerta de sello ya no dice que nunca se vio una
# ═══════════════════════════════════════════════════════════════════════════

def test_el_aviso_de_alerta_de_sello_esta_al_dia(tmp_path, monkeypatch, caplog):
    """Ejecuta el módulo con una alerta de sello y lee el aviso que emite."""
    import copy
    from app.providers.tive import estado, modulo
    from tests.proveedores.tive.test_v195_tive import CRUDOS, CIERRE_2284
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    alerta = copy.deepcopy(CRUDOS[CIERRE_2284])
    for nodo in (alerta, alerta["Alert"]):
        nodo["DeviceId"] = nodo["DeviceName"] = nodo["EntityName"] = "A2A2A2017BF2"
    try:
        with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
            (ev,) = modulo.procesar(alerta, "prod", {}, "sello-1")
    finally:
        estado.cerrar_todo()
    assert ev.chassis_number == "A2A2A2017BF2", "el sello sale con su número, que es su patente"
    assert "aún no se vio" not in caplog.text
    assert "confirmada" in caplog.text and "A2A2A2017BF2" in caplog.text
