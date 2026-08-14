"""
Regresión: audit_retention_days nunca se aplicó a los crudos.

processor.clean_old_files() limpiaba "audit/{provider}", pero log_raw_payload()
escribe en "audit/{provider}_{env}". El directorio apuntado no existía nunca, así
que os.path.exists() daba False y la limpieza no borraba absolutamente nada: los
crudos crecían sin techo desde siempre. A caudal de certificación, ~2 GB por día.

Este test es una invariante de RUTA, no de comportamiento: verifica que la
limpieza mire el mismo directorio que la escritura. Si mañana alguien cambia el
esquema de carpetas en un lado y se olvida del otro, esto lo frena.
"""
import asyncio
import json
import os
import time

import pytest

from app.core.auditor import log_raw_payload
from app.worker import processor


@pytest.fixture
def disco(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _envejecer(ruta, dias):
    viejo = time.time() - dias * 24 * 60 * 60
    os.utime(ruta, (viejo, viejo))


def _archivos_crudos(base):
    return sorted(
        os.path.join(r, f)
        for r, _, files in os.walk(os.path.join(base, "audit"))
        for f in files if f.endswith(".jsonl")
    )


def _correr_limpieza(monkeypatch, provider, env, audit_dias=30, procesados_dias=30):
    class _S:
        audit_retention_days = audit_dias
        processed_retention_days = procesados_dias
        processed_logs_enabled = True

    monkeypatch.setattr(processor, "get_settings", lambda: _S())
    monkeypatch.setattr(processor, "obtener_parametros_rc", lambda: {"retencion_horas": 2})
    asyncio.run(processor.purge_provider_events(provider, env))


def test_la_limpieza_mira_el_mismo_directorio_que_escribe_el_auditor(disco, monkeypatch):
    """El bug exacto: la ruta de limpieza no coincidía con la de escritura."""
    log_raw_payload("schmitz", "test", {"ChassisNumber": "AB1234"})

    escritos = _archivos_crudos(disco)
    assert escritos, "El auditor no escribió nada; el test no prueba lo que dice"
    assert "schmitz_test" in escritos[0], "El auditor escribe en audit/{provider}_{env}"

    _envejecer(escritos[0], dias=60)
    _correr_limpieza(monkeypatch, "schmitz", "test", audit_dias=30)

    assert not _archivos_crudos(disco), (
        "El crudo de 60 días sigue ahí con retención de 30: la limpieza vuelve a "
        "apuntar a un directorio que no existe"
    )


def test_los_crudos_recientes_no_se_borran(disco, monkeypatch):
    """La retención borra lo vencido, no lo vigente."""
    log_raw_payload("schmitz", "test", {"ChassisNumber": "AB1234"})
    reciente = _archivos_crudos(disco)[0]
    _envejecer(reciente, dias=5)

    _correr_limpieza(monkeypatch, "schmitz", "test", audit_dias=30)

    assert _archivos_crudos(disco) == [reciente]


def test_la_retencion_de_crudos_es_configurable(disco, monkeypatch):
    """El valor sale de SystemSettings, editable desde el panel."""
    log_raw_payload("protrack", "prod", {"imei": "123"})
    archivo = _archivos_crudos(disco)[0]
    _envejecer(archivo, dias=10)

    _correr_limpieza(monkeypatch, "protrack", "prod", audit_dias=30)
    assert _archivos_crudos(disco), "Con 30 días de retención, uno de 10 se conserva"

    _correr_limpieza(monkeypatch, "protrack", "prod", audit_dias=7)
    assert not _archivos_crudos(disco), "Con 7 días de retención, uno de 10 se borra"


def test_la_limpieza_de_un_proveedor_no_toca_a_otro(disco, monkeypatch):
    """Cada proveedor y entorno tiene su propia carpeta y su propio ciclo."""
    log_raw_payload("schmitz", "test", {"a": 1})
    log_raw_payload("protrack", "prod", {"b": 2})
    for ruta in _archivos_crudos(disco):
        _envejecer(ruta, dias=60)

    _correr_limpieza(monkeypatch, "schmitz", "test", audit_dias=30)

    quedan = _archivos_crudos(disco)
    assert len(quedan) == 1
    assert "protrack_prod" in quedan[0]


def test_el_contenido_del_crudo_no_se_altera_al_escribirlo(disco):
    """
    Lo que se descarga después tiene que ser lo que mandó el proveedor. Si el
    auditor transformara el payload, la descarga de crudos perdería su sentido.
    """
    payload = {"ChassisNumber": "AB1234", "Nested": {"lista": [1, 2, {"x": None}]}}
    log_raw_payload("schmitz", "test", payload)

    with open(_archivos_crudos(disco)[0], encoding="utf-8") as f:
        registro = json.loads(f.readline())

    assert registro["payload"] == payload
    assert registro["provider"] == "schmitz"
    assert registro["env"] == "test"
