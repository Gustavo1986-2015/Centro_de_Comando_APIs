# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v198.py (reorganización por tema: proveedores/protrack).
"""
v1.9.8 — Protección de datos hacia RC, panel y visor de base de datos.

Todos los tests EJECUTAN el código (endpoints, worker, módulo de Tive, JS real
del panel): ninguno busca texto en el fuente.
"""
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 3. B-4 — El sondeo PULL no bloquea el bucle de eventos
# ═══════════════════════════════════════════════════════════════════════════

async def test_el_bucle_de_eventos_responde_durante_un_sondeo_con_la_base_bloqueada(config_aislada, monkeypatch):
    """
    Otra conexión tiene la base tomada 1,5 s mientras el sondeo inserta. Antes,
    el bucle quedaba sin responder todo ese tiempo (medido: 3.010 ms con 3 s
    de bloqueo) y el hub no atendía webhooks. Ahora la espera ocurre en un hilo.
    """
    import asyncio
    import threading
    import time
    from app.database import get_engine, get_session
    from app.models.db_models import NormalizedRCEvent
    from app.worker import processor, pull_engine
    from tests.proveedores.protrack.test_protrack_v192 import PROTRACK_ITEM, PROTRACK_MAPEO
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    get_engine("protrack", "prod")

    tomada = threading.Event()

    def candado():
        con = sqlite3.connect("db/protrack/prod.db")
        con.execute("BEGIN IMMEDIATE")
        tomada.set()
        time.sleep(1.5)
        con.rollback()
        con.close()

    peor, corriendo = 0.0, True

    async def latido():
        nonlocal peor
        while corriendo:
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            peor = max(peor, time.perf_counter() - t - 0.01)

    tarea = asyncio.create_task(latido())
    await asyncio.sleep(0.05)   # el latido ya corre antes del sondeo
    hilo = threading.Thread(target=candado)
    hilo.start()
    await asyncio.to_thread(tomada.wait, 5)   # esperar el candado sin bloquear el bucle
    await pull_engine.process_and_enqueue("protrack", "prod", {"code": 0, "record": [dict(PROTRACK_ITEM)]},
                                          PROTRACK_MAPEO, enable_state_dedup=False)
    corriendo = False
    await tarea
    hilo.join()
    db = get_session("protrack", "prod")
    assert db.query(NormalizedRCEvent).count() >= 1, "el sondeo tenía que esperar el candado e insertar"
    db.close()
    assert peor < 0.5, f"el bucle de eventos quedó {peor * 1000:.0f} ms sin responder"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Errores PULL: siempre el tipo, y el mensaje si lo hay
# ═══════════════════════════════════════════════════════════════════════════

def test_describir_error():
    import httpx
    from app.worker.pull_engine import describir_error
    assert str(httpx.ConnectTimeout("")) == "", "medido: el timeout real de httpx no tiene texto"
    assert describir_error(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert describir_error(ValueError("respuesta rara")) == "ValueError: respuesta rara"


async def test_cableado_un_timeout_del_sondeo_se_ve_en_consola_y_en_salud(monkeypatch, caplog):
    import asyncio
    import logging
    import httpx
    from app.core import provider_health
    from app.worker import pull_engine

    async def falla(_cfg):
        raise httpx.ConnectTimeout("")

    async def cortar(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(pull_engine, "_leer_config_sondeo_sync",
                        lambda p, e: (True, {"url": "http://proveedor.invalid/api"}, {}, 30, False, False))
    monkeypatch.setattr(pull_engine, "execute_fetch", falla)
    monkeypatch.setattr(pull_engine.asyncio, "sleep", cortar)
    provider_health.forget("sondeo", "prod")
    try:
        with caplog.at_level(logging.ERROR, logger=pull_engine.logger.name):
            with pytest.raises(asyncio.CancelledError):
                await pull_engine.telemetry_poll_loop("sondeo", "prod")
        assert provider_health._entry("sondeo", "prod")["last_fetch_error"] == "ConnectTimeout"
        assert "Error en Sondeo PULL: ConnectTimeout" in caplog.text
    finally:
        provider_health.forget("sondeo", "prod")
