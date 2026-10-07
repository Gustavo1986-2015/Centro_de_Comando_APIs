"""
v1.9.4 · Punto 1 — Cierre de alerta de rango clasificado como puntual.

Medido en audit/tive_prod/2026-10/crudos_2026-10-05.jsonl (las líneas están
en tests/fixtures/tive_crudos_2026-10.jsonl con origen "2026-10-05:<línea>"):

  · Los cierres de alertas de rango llegan de DOS formas:
      [["Created"],["Closed"]]  — 6aed7e20, 7a51b62d
      [["Created","Closed"]]    — 2284be50, 7883ad0b, a5e5bcc9 (HumidityMax)
  · La segunda es la misma forma que las puntuales (ShockEvents 6de04aef,
    725d6000). Lo que las separa: el cierre trae RecoveredAlertDate; la
    puntual trae 0001-01-01.
  · El hub mandó el cierre de 7883ad0b como "HumidityMax", no "HumidityMax-FIN".
"""
import copy
import json
import logging
import os

import pytest

from app.providers.tive import estado, modulo

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tive_crudos_2026-10.jsonl")
CRUDOS = {json.loads(l)["origen"]: json.loads(l)["payload"] for l in open(FIXTURE, encoding="utf-8")}

APERTURA_7883 = "2026-10-05:154"
APERTURA_A5E5 = "2026-10-05:156"      # gemela de 7883ad0b (otro AlertId, mismo hecho)
CIERRE_7883 = "2026-10-05:162"
CIERRE_A5E5 = "2026-10-05:163"
APERTURA_2284, CIERRE_2284 = "2026-10-05:103", "2026-10-05:104"
SHOCK = "2026-10-05:64"
CIERRE_SEPARADO = "2026-10-05:99"     # 7a51b62d, forma [["Created"],["Closed"]]

TRACKERS_ON = {"alertas_trackers": True}


def _p(origen):
    return copy.deepcopy(CRUDOS[origen])


@pytest.fixture(autouse=True)
def estado_aislado(tmp_path, monkeypatch):
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado_tive"))
    yield
    estado.cerrar_todo()


_n = [0]


def _procesar(payload):
    _n[0] += 1
    return modulo.procesar(payload, "prod", TRACKERS_ON, f"iid-{_n[0]}")


def test_lo_medido_esta_en_el_fixture():
    cierre = _p(CIERRE_A5E5)
    assert cierre["Alert"]["AlertId"].startswith("a5e5bcc9")
    assert [d["Reasons"] for d in cierre["Alert"]["Details"]] == [["Created", "Closed"]]
    assert not cierre["RecoveredAlertDate"].startswith("0001")
    shock = _p(SHOCK)
    assert [d["Reasons"] for d in shock["Alert"]["Details"]] == [["Created", "Closed"]]
    assert shock["RecoveredAlertDate"].startswith("0001")


def test_el_caso_reportado_sale_como_apertura_y_cierre():
    """K393478 HumidityMax del 05/10: una alerta que empezó y terminó, no dos."""
    salida = []
    for origen in (APERTURA_7883, APERTURA_A5E5, CIERRE_7883, CIERRE_A5E5):
        salida += _procesar(_p(origen))
    assert [e.code for e in salida] == ["HumidityMax", "HumidityMax-FIN"]


def test_el_cierre_corto_de_2284be50():
    codigos = [e.code for o in (APERTURA_2284, CIERRE_2284) for e in _procesar(_p(o))]
    assert codigos == ["HumidityMax", "HumidityMax-FIN"]


def test_el_cierre_se_reconoce_aunque_la_apertura_no_haya_llegado():
    """Por RecoveredAlertDate: no depende del orden de llegada."""
    assert [e.code for e in _procesar(_p(CIERRE_A5E5))] == ["HumidityMax-FIN"]


def test_sin_fecha_de_recuperacion_alcanza_con_que_ya_se_haya_abierto():
    """La regla del pedido: un AlertId registrado abierto no puede ser puntual."""
    assert len(_procesar(_p(APERTURA_A5E5))) == 1
    cierre = _p(CIERRE_A5E5)
    cierre["RecoveredAlertDate"] = "0001-01-01T00:00:00"
    assert modulo.estado_alerta(cierre, "prod") == "cierre"


def test_la_puntual_real_sigue_siendo_puntual():
    assert modulo.estado_alerta(_p(SHOCK), "prod") == "puntual"
    assert [e.code for e in _procesar(_p(SHOCK))] == ["ShockEvents"]


def test_un_tipo_puntual_medido_nunca_lleva_fin():
    shock = _p(SHOCK)
    shock["RecoveredAlertDate"] = "2026-10-05T13:00:00Z"
    assert modulo.estado_alerta(shock, "prod") == "puntual"


def test_la_forma_separada_sigue_siendo_cierre():
    assert modulo.estado_alerta(_p(CIERRE_SEPARADO), "prod") == "cierre"


def test_sin_ninguna_senal_es_puntual_y_avisa(caplog):
    """Un tipo no medido, sin recuperación ni apertura: no se adivina en silencio."""
    raro = _p(CIERRE_A5E5)
    raro["AlertType"] = raro["Alert"]["AlertType"] = "TipoNuevo"
    raro["RecoveredAlertDate"] = "0001-01-01T00:00:00"
    with caplog.at_level(logging.WARNING, logger="app.providers.tive.modulo"):
        assert modulo.estado_alerta(raro, "prod") == "puntual"
    assert "Verificar el tipo" in caplog.text and "TipoNuevo" in caplog.text


def test_el_dia_completo_no_deja_cierres_sin_fin():
    """Con trackers activos, cada AlertId de rango cerrado sale con -FIN."""
    salida = []
    for origen in sorted(o for o in CRUDOS if o.startswith("2026-10-05")):
        for e in _procesar(_p(origen)):
            salida.append((e.code, origen))
    fines = [c for c, _ in salida if c.endswith("-FIN")]
    assert sorted(fines) == ["HumidityMax-FIN", "HumidityMax-FIN", "HumidityMax-FIN",
                             "LocationPerimeter-FIN"]
    assert [c for c, _ in salida if c == "ShockEvents"] == ["ShockEvents", "ShockEvents"]


def test_por_el_camino_real_la_apertura_registrada_convierte_en_cierre():
    """
    Cableado: procesar() tiene que pasarle el entorno a la clasificación, si
    no la regla "ya se registró abierta" no puede consultar el estado. Sin
    fecha de recuperación, es la única señal que queda.
    """
    assert [e.code for e in _procesar(_p(APERTURA_A5E5))] == ["HumidityMax"]
    cierre = _p(CIERRE_A5E5)
    cierre["RecoveredAlertDate"] = "0001-01-01T00:00:00"
    assert [e.code for e in _procesar(cierre)] == ["HumidityMax-FIN"]
