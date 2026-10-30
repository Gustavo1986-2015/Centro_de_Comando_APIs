"""
v1.9.11 · Bloque 4 — Opciones de módulo dedicado en Configuración.

El módulo declara grupo, título, ayuda y si duplica RC de cada interruptor
(contrato en app/providers/registry.py); el panel dibuja "Opciones de
<proveedor>" desde eso, sin saber nada de Tive. La opción de coordenadas 0,0
va en la fila de cada integración.

Se ejecuta: registry real, GET/POST /api/config reales y el JS REAL del panel
(loadConfig, _opcionesDeModulo, saveConfig) con los arneses de tools/panel.
"""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.providers import registry
from app.providers.tive import modulo as tive
from tests.rc.test_v195_modo_simulado import _foto, _guardar_desde_el_panel, auth, cliente  # noqa: F401

RAIZ = Path(__file__).resolve().parents[2]
TITULOS_TIVE = {
    "posiciones_terceros": "Enviar posiciones de contenedores y tramos aéreos",
    "posiciones_equipos": "Enviar posiciones de trackers y sellos",
    "alertas_beacons": "Enviar alertas de sellos (beacons)",
    "alertas_trackers": "Enviar alertas de trackers",
    "enviar_sin_posicion": "Enviar eventos sin posición",
    "velocidad_equipos": "Calcular velocidad estimada de los equipos",
    "resolver_nombres_api": "Resolver nombres de trackers con la API de Tive",
}


# ─── Contrato del registro ───────────────────────────────────────────────────

def test_tive_declara_cada_interruptor_con_su_grupo_titulo_y_ayuda():
    detalle, grupos = registry.detalle_para_panel(tive)
    assert grupos == ["Qué se envía a RC", "Cálculos"]
    assert [d["clave"] for d in detalle] == list(TITULOS_TIVE), "el orden de la spec"
    assert {d["clave"]: d["titulo"] for d in detalle} == TITULOS_TIVE
    assert {d["clave"] for d in detalle if d["duplica_rc"]} == {"posiciones_equipos", "alertas_trackers"}
    assert {d["clave"] for d in detalle if d["grupo"] == "Cálculos"} == {"velocidad_equipos", "resolver_nombres_api"}
    for d in detalle:
        assert "Apagado" in d["ayuda"], f"{d['clave']}: la ayuda dice qué pasa apagado"
    assert set(tive.DETALLE_INTERRUPTORES) == set(tive.INTERRUPTORES), "ninguno sin declarar"


def test_un_modulo_sin_detalle_cae_en_opciones_con_su_descripcion():
    viejo = SimpleNamespace(INTERRUPTORES={"a": True, "b": False}, DESCRIPCION_INTERRUPTORES={"a": "Texto A"})
    detalle, grupos = registry.detalle_para_panel(viejo)
    assert grupos == ["Opciones"]
    assert [(d["clave"], d["titulo"], d["duplica_rc"]) for d in detalle] == [("a", "Texto A", False), ("b", "b", False)]


# ─── API ─────────────────────────────────────────────────────────────────────

def test_la_api_entrega_lo_que_declara_el_modulo_y_la_opcion_de_0_0(cliente, auth):
    configs = cliente.get("/api/config", auth=auth).json()
    tive_prod = next(c for c in configs if (c["provider_name"], c["env"]) == ("TIVE", "PROD"))
    assert tive_prod["module_options_grupos"] == ["Qué se envía a RC", "Cálculos"]
    assert [d["clave"] for d in tive_prod["module_options_detalle"]] == list(TITULOS_TIVE)
    assert tive_prod["module_credentials_grupo"] == "API de Tive"
    assert {c["coordenadas_cero"] for c in configs} == {"enviar"}, "todas las integraciones, por defecto enviar"
    assert all("module_options_detalle" not in c for c in configs if c["provider_name"] != "TIVE")


# ─── JS real ─────────────────────────────────────────────────────────────────

def _node(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "panel" / "verificar_panel_v199.js"), str(e), str(s)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


def test_el_panel_se_dibuja_desde_lo_que_declara_el_modulo_js_real(cliente, auth, tmp_path):
    configs = cliente.get("/api/config", auth=auth).json()
    tive_prod = dict(next(c for c in configs if (c["provider_name"], c["env"]) == ("TIVE", "PROD")), _originalIdx=4)
    r = _node({"llamadas": [{"fn": "_opcionesDeModulo", "args": [tive_prod]},
                            {"fn": "_botonOpcionesDeModulo", "args": [tive_prod]},
                            {"fn": "_selectorCeros", "args": [dict(tive_prod, coordenadas_cero="filtrar")]}]},
              tmp_path)
    html, boton, ceros = r["resultados"]
    # Secciones en el orden declarado, y credenciales al final.
    assert html.index("<h4>Qué se envía a RC</h4>") < html.index("<h4>Cálculos</h4>") < html.index("<h4>API de Tive</h4>")
    assert html.count('type="checkbox"') == 7
    for clave, titulo in TITULOS_TIVE.items():
        assert f'id="modopt_4_{clave}"' in html and titulo in html
    # ⚠ solo en los que duplican lo que RC ya recibe.
    assert html.count('class="marca-duplica-rc"') == 2
    for clave in ("posiciones_equipos", "alertas_trackers"):
        bloque = html[html.index(f'modopt_4_{clave}') - 200: html.index(f'modopt_4_{clave}') + 400]
        assert "marca-duplica-rc" in bloque and "duplica-rc" in bloque
    assert "Encendido: se envían a RC con latitud y longitud 0" in html, "la línea de ayuda"
    assert "grilla-opciones-modulo" in html
    assert "Opciones de TIVE" in boton and 'aria-expanded="false"' in boton
    assert 'value="filtrar" selected' in ceros and 'id="ceros_4"' in ceros


def test_un_modulo_cualquiera_se_dibuja_sin_saber_nada_de_tive_js_real(tmp_path):
    acme = {"_originalIdx": 2, "provider_name": "ACME", "modulo_dedicado": True,
            "module_options": {"x": True, "y": False, "z": True},
            "module_options_labels": {"z": "Zeta corta"},
            "module_options_grupos": ["Sección 1"],
            "module_options_detalle": [
                {"clave": "y", "grupo": "Sección 1", "titulo": "Hace Y", "ayuda": "Encendido Y. Apagado no.", "duplica_rc": True},
                {"clave": "x", "grupo": "Sección 2", "titulo": "Hace X", "ayuda": "", "duplica_rc": False}]}
    r = _node({"llamadas": [{"fn": "_opcionesDeModulo", "args": [acme]}]}, tmp_path)
    html = r["resultados"][0]
    assert html.index("<h4>Sección 1</h4>") < html.index("<h4>Sección 2</h4>") < html.index("<h4>Opciones</h4>")
    assert html.index("Hace Y") < html.index("Hace X") < html.index("Zeta corta")
    assert html.count('class="marca-duplica-rc"') == 1
    assert "Tive" not in html and "credenciales" not in html


def test_loadconfig_real_deja_la_fila_como_las_demas_y_el_panel_debajo(cliente, auth, tmp_path):
    configs = cliente.get("/api/config", auth=auth).json()
    r = _node({"respuestas": {"/api/config": {"status": 200, "cuerpo": configs}},
               "globales": {"currentConfigs": []},
               "llamadas": [{"fn": "loadConfig"}]}, tmp_path)
    filas = r["elementos"]["config-table-body"]["hijos"]
    paneles = [f for f in filas if "panel-opciones-modulo" in f]
    normales = [f for f in filas if "panel-opciones-modulo" not in f]
    assert len(normales) == len(configs)
    assert len(paneles) == sum(1 for c in configs if c["provider_name"] == "TIVE")
    assert all('type="checkbox" id="modopt_' not in f for f in normales), "la fila quedó como las demás"
    assert all('id="ceros_' in f for f in normales), "la opción 0,0 en la fila de cada integración"
    assert 'colspan="15"' in paneles[0]


def test_se_guarda_y_se_recarga_desde_el_panel(cliente, auth, tmp_path):
    configs = cliente.get("/api/config", auth=auth).json()
    i_tive = next(i for i, c in enumerate(configs) if (c["provider_name"], c["env"]) == ("TIVE", "PROD"))
    i_schmitz = next(i for i, c in enumerate(configs) if (c["provider_name"], c["env"]) == ("SCHMITZ", "PROD"))
    enviado, r = _guardar_desde_el_panel(cliente, auth, tmp_path,
                                         f"modopt_{i_tive}_enviar_sin_posicion=false",
                                         f"modopt_{i_tive}_velocidad_equipos=true",
                                         f"ceros_{i_schmitz}=filtrar")
    assert r.status_code == 200, r.text
    assert enviado["updates"][i_schmitz]["coordenadas_cero"] == "filtrar"
    recargado = cliente.get("/api/config", auth=auth).json()
    assert recargado[i_schmitz]["coordenadas_cero"] == "filtrar"
    assert recargado[i_tive]["module_options"]["enviar_sin_posicion"] is False
    assert recargado[i_tive]["module_options"]["velocidad_equipos"] is True
    # Recargado y guardado sin tocar nada: no cambia ningún campo.
    antes = _foto()
    _, r = _guardar_desde_el_panel(cliente, auth, tmp_path)
    assert r.status_code == 200 and _foto() == antes
