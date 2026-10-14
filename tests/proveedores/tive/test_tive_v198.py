# v1.9.9 (5.2): tests movidos sin cambios desde tests/test_v198.py (reorganización por tema: proveedores/tive).
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
# 4. Tive — ShipmentArriveDepart (estructura DERIVADA: faltan los crudos reales)
# ═══════════════════════════════════════════════════════════════════════════

def _llegada_salida(alert_on, reasons=None):
    """
    Alerta de sello derivada de un cierre real del fixture (HumidityMax,
    05/10): AlertType=ShipmentArriveDepart y Alert.Trigger.AlertOn. Los dos
    crudos reales del 08/10 (037ab87a… Departure, b3ad9d3f… Arrival) no están
    en esta máquina; cuando lleguen, reemplazan a esta derivación.
    """
    import copy
    from tests.proveedores.tive.test_v195_tive import CRUDOS, CIERRE_2284
    alerta = copy.deepcopy(CRUDOS[CIERRE_2284])
    for nodo in (alerta, alerta["Alert"]):
        nodo["DeviceId"] = nodo["DeviceName"] = nodo["EntityName"] = "A2A2A2017BF2"
        nodo["AlertType"] = "ShipmentArriveDepart"
    alerta["Alert"]["AlertId"] = f"derivada-{alert_on}-{reasons}"
    if alert_on is None:
        alerta["Alert"]["Trigger"].pop("AlertOn", None)
    else:
        alerta["Alert"]["Trigger"]["AlertOn"] = alert_on
    if reasons is not None:
        alerta["RecoveredAlertDate"] = "0001-01-01T00:00:00"
        for d in alerta["Alert"]["Details"]:
            d["Reasons"] = reasons
    return alerta


def _procesar_tive(tmp_path, monkeypatch, payload):
    from app.providers.tive import estado, modulo
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    try:
        return modulo.procesar(payload, "prod", {}, "llegada-salida")
    finally:
        estado.cerrar_todo()


@pytest.mark.parametrize("alert_on,codigo", [("Departure", "ShipmentArriveDepart-Departure"),
                                              ("Arrival", "ShipmentArriveDepart-Arrival"),
                                              ("arrival", "ShipmentArriveDepart-Arrival")])
@pytest.mark.parametrize("reasons", [None, ["Created", "Closed"], ["Closed"]])
def test_llegada_y_salida_son_puntuales_con_su_sufijo(tmp_path, monkeypatch, caplog, alert_on, codigo, reasons):
    """
    reasons=None: el cierre real (con fecha de recuperación) que antes salía
    con -FIN. ["Created","Closed"] sin recuperación: antes avisaba "Verificar
    el tipo". ["Closed"]: antes era un cierre.
    """
    import logging
    with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
        (ev,) = _procesar_tive(tmp_path, monkeypatch, _llegada_salida(alert_on, reasons))
    assert ev.code == codigo
    assert ev.chassis_number == "A2A2A2017BF2", "el sello sale con su número"
    assert "-FIN" not in ev.code and "Verificar el tipo" not in caplog.text


@pytest.mark.parametrize("alert_on", [None, "Loitering"])
def test_llegada_salida_sin_alert_on_reconocible_sale_literal_con_aviso(tmp_path, monkeypatch, caplog, alert_on):
    import logging
    with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
        (ev,) = _procesar_tive(tmp_path, monkeypatch, _llegada_salida(alert_on))
    assert ev.code == "ShipmentArriveDepart"
    assert "sin Alert.Trigger.AlertOn reconocible" in caplog.text and repr(alert_on) in caplog.text
    assert "Verificar el tipo" not in caplog.text
