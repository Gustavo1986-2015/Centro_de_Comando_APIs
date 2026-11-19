"""
v1.9.12 (E.7): el panel ante la verificación en dos pasos, con el JS REAL de
dashboard.js en Node.

- El manejo central de fetch: 403 verificacion_requerida → /dashboard; nada
  más cambia (con la verificación apagada esa respuesta no existe).
- El sondeo al cortarse un stream: solo con la verificación activa.
- La sección Configuración → Seguridad del panel.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
JS = RAIZ / "frontend" / "static" / "dashboard.js"
ARNES = RAIZ / "tools" / "panel" / "verificar_panel_v199.js"


def _bloque_fetch() -> str:
    src = JS.read_text(encoding="utf-8")
    inicio = src.index("(function () {\n            const fetchOriginal")
    fin = src.index("})();", inicio) + len("})();")
    sondeo_i = src.index("let _ultimoSondeoSesion = 0;")
    sondeo_f = src.index("\n        }\n", src.index("function _sondearSesionTrasCorte", sondeo_i)) + len("\n        }\n")
    return src[inicio:fin] + "\n" + src[sondeo_i:sondeo_f]


PRUEBA_FETCH = r"""
const fs = require('fs');
const casos = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const bloque = fs.readFileSync(process.argv[3], 'utf8');
(async () => {
  const salida = [];
  for (const caso of casos) {
    const pedidos = [];
    let clones = 0;
    const respuesta = {
      status: caso.status,
      clone() { clones++; return { json: async () => { if (caso.cuerpo === null) throw new SyntaxError('no JSON'); return caso.cuerpo; } }; },
    };
    global.window = { location: { href: '/dashboard#config' }, fetch: async (url, op) => { pedidos.push(url); return respuesta; } };
    global.fetch = (...a) => window.fetch(...a);
    global.document = { body: { dataset: { dosPasos: caso.dosPasos || '0' } } };
    global.Date = Object.assign(function () {}, { now: () => caso.ahora || 100000 });
    eval(bloque + '\nglobal._sondearSesionTrasCorte = _sondearSesionTrasCorte;');
    let devuelta = null;
    if (caso.status !== undefined) devuelta = await window.fetch('/api/algo');
    for (let i = 0; i < (caso.sondeos || 0); i++) _sondearSesionTrasCorte();
    await new Promise(r => setImmediate(r));
    salida.push({ href: window.location.href, misma: devuelta === respuesta, clones, pedidos });
  }
  fs.writeFileSync(process.argv[4], JSON.stringify(salida));
})();
"""


def _correr_fetch(casos, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    (tmp_path / "casos.json").write_text(json.dumps(casos), encoding="utf-8")
    (tmp_path / "bloque.js").write_text(_bloque_fetch(), encoding="utf-8")
    (tmp_path / "prueba.js").write_text(PRUEBA_FETCH, encoding="utf-8")
    p = subprocess.run(["node", str(tmp_path / "prueba.js"), str(tmp_path / "casos.json"),
                        str(tmp_path / "bloque.js"), str(tmp_path / "salida.json")],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads((tmp_path / "salida.json").read_text(encoding="utf-8"))


def test_fetch_403_verificacion_requerida_vuelve_al_panel(tmp_path):
    r = _correr_fetch([{"status": 403, "cuerpo": {"detail": "verificacion_requerida"}}], tmp_path)[0]
    assert r["href"] == "/dashboard"
    assert r["misma"], "la respuesta se devuelve igual a quien la pidió"


@pytest.mark.parametrize("caso", [
    {"status": 200, "cuerpo": {"detail": "verificacion_requerida"}},
    {"status": 403, "cuerpo": {"detail": "Petición rechazada: llegó desde otro sitio."}},
    {"status": 403, "cuerpo": None},
    {"status": 401, "cuerpo": {"detail": "Credenciales incorrectas"}},
    {"status": 500, "cuerpo": {}},
])
def test_fetch_lo_demas_no_cambia(tmp_path, caso):
    r = _correr_fetch([caso], tmp_path)[0]
    assert r["href"] == "/dashboard#config", "no redirige"
    assert r["misma"]
    if caso["status"] != 403:
        assert r["clones"] == 0, "ni siquiera lee el cuerpo: la respuesta queda intacta"


def test_sondeo_tras_corte_solo_con_la_verificacion_activa(tmp_path):
    apagada, activa = _correr_fetch([{"dosPasos": "0", "sondeos": 3},
                                     {"dosPasos": "1", "sondeos": 3}], tmp_path)
    assert apagada["pedidos"] == [], "apagada: un corte de stream no hace ninguna petición nueva"
    assert activa["pedidos"] == ["/api/seguridad/sesion"], "activa: una consulta liviana, no una por error"


def test_los_tres_streams_sondean_al_cortarse():
    src = JS.read_text(encoding="utf-8")
    for marca in ("window.evtSource.onerror = () => {", "_consoleSource.onerror = () => {"):
        i = 0
        while (i := src.find(marca, i)) >= 0:
            cuerpo = src[i:src.index("};", i)]
            assert "_sondearSesionTrasCorte();" in cuerpo, marca
            i += len(marca)
    assert src.count("_sondearSesionTrasCorte();") == 3


# ── Sección Seguridad ───────────────────────────────────────────────────────

# Las funciones de la sección atrapan sus errores (los muestran en el panel):
# el arnés no se entera de qué dependencia falta, así que se cargan antes. La
# constante de estilo lleva ";" y el arnés no la lee sola.
_DEPENDENCIAS = ["_segPedir", "_segMensaje", "_cajaImport", "_escapeHtml", "_fechaHora", "_segMostrarCodigos"]
_GLOBALES = {"_SEG_INPUT": "", "_segUltimosCodigos": []}


def _panel(entrada, tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    entrada = {**entrada, "cargar": _DEPENDENCIAS + entrada.get("cargar", []),
               "globales": {**_GLOBALES, **entrada.get("globales", {})}}
    e, s = tmp_path / "entrada.json", tmp_path / "salida.json"
    e.write_text(json.dumps(entrada), encoding="utf-8")
    p = subprocess.run(["node", str(ARNES), str(e), str(s)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, env={**os.environ, "TZ": "UTC"})
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(s.read_text(encoding="utf-8"))


ESTADO_INACTIVO = {"activo": False, "activado_en": None, "codigos_restantes": 0, "duracion_horas": 12,
                   "duracion_min": 1, "duracion_max": 168, "puede_activar": True,
                   "motivo_no_puede": None, "bloqueado_seg": 0}


def test_seccion_inactiva_ofrece_activar(tmp_path):
    r = _panel({"llamadas": [{"fn": "_segHtmlInactiva", "args": [ESTADO_INACTIVO]}]}, tmp_path)["resultados"][0]
    assert "Activar verificación en dos pasos" in r and 'id="seg-pass-alta"' in r and "desactivada" in r


def test_seccion_sin_master_enc_key_explica_y_no_ofrece_activar(tmp_path):
    estado = {**ESTADO_INACTIVO, "puede_activar": False,
              "motivo_no_puede": "No se puede activar: MASTER_ENC_KEY no está definida <x>"}
    r = _panel({"llamadas": [{"fn": "_segHtmlInactiva", "args": [estado]}]}, tmp_path)["resultados"][0]
    assert "MASTER_ENC_KEY no está definida &lt;x&gt;" in r, "el motivo, escapado"
    assert "seguridadIniciar" not in r


def test_seccion_activa_muestra_estado_y_acciones(tmp_path):
    estado = {**ESTADO_INACTIVO, "activo": True, "activado_en": "2026-11-08T14:00:00+00:00",
              "codigos_restantes": 2, "duracion_horas": 24}
    r = _panel({"llamadas": [{"fn": "_segHtmlActiva", "args": [estado]}]}, tmp_path)["resultados"][0]
    assert "2026-11-08 14:00:00" in r and "2 de 10" in r and "generá códigos nuevos" in r
    assert 'value="24"' in r
    for accion in ("seguridadGuardarDuracion", "seguridadCerrarEsta", "seguridadCerrarTodas",
                   "seguridadNuevosCodigos", "seguridadDesactivar"):
        assert accion in r, accion


RESPUESTAS_ALTA = {
    "/api/seguridad/dos-pasos/iniciar": {"status": 200, "cuerpo": {
        "secreto": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP", "uri": "otpauth://x",
        "qr_svg": "<svg id='qr'></svg>", "vence_en_seg": 900}},
    "/api/seguridad/dos-pasos/confirmar": {"status": 200, "cuerpo": {
        "activo": True, "codigos_recuperacion": [f"ABC{i}-DEF{i}" for i in range(2, 10)] + ["HJKM-NPQR", "STUV-WXYZ"]}},
}


def test_alta_muestra_el_qr_y_el_secreto(tmp_path):
    salida = _panel({"respuestas": RESPUESTAS_ALTA, "stubs": ["cargarSeguridad"], "llamadas": [
        {"set": {"id": "seg-pass-alta", "value": "la-clave"}},
        {"fn": "seguridadIniciar", "args": []},
    ]}, tmp_path)
    pedido = salida["fetch"][0]
    assert pedido["url"] == "/api/seguridad/dos-pasos/iniciar"
    assert json.loads(pedido["opciones"]["body"]) == {"password": "la-clave"}
    alta = salida["elementos"]["seguridad-alta"]["innerHTML"]
    assert "<svg id='qr'></svg>" in alta
    assert "JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP" in alta, "el secreto en grupos de 4 para cargarlo a mano"
    assert "seguridadConfirmar" in alta
    assert salida["elementos"]["seg-pass-alta"]["value"] == "", "la contraseña no queda en el campo"


def test_al_confirmar_se_muestran_los_codigos_una_vez(tmp_path):
    codigos = RESPUESTAS_ALTA["/api/seguridad/dos-pasos/confirmar"]["cuerpo"]["codigos_recuperacion"]
    salida = _panel({"respuestas": RESPUESTAS_ALTA, "stubs": ["cargarSeguridad"], "llamadas": [
        {"set": {"id": "seg-pass-alta", "value": "la-clave"}},
        {"fn": "seguridadIniciar", "args": []},
        {"set": {"id": "seg-codigo-alta", "value": "123456"}},
        {"fn": "seguridadConfirmar", "args": []},
        {"fn": "seguridadOcultarCodigos", "args": []},
    ]}, tmp_path)
    assert json.loads(salida["fetch"][1]["opciones"]["body"]) == {"codigo": "123456"}
    assert salida["elementos"]["seguridad-alta"]["innerHTML"] == "", "el QR y el secreto se borran al activar"
    # Antes de "Ya los guardé" estaban los 10; después, nada.
    assert salida["elementos"]["seguridad-codigos"]["innerHTML"] == ""


def test_los_codigos_se_ven_y_se_descargan(tmp_path):
    codigos = RESPUESTAS_ALTA["/api/seguridad/dos-pasos/confirmar"]["cuerpo"]["codigos_recuperacion"]
    salida = _panel({"respuestas": RESPUESTAS_ALTA, "stubs": ["cargarSeguridad"], "llamadas": [
        {"fn": "_segMostrarCodigos", "args": [codigos, "Guardá estos códigos"]},
    ]}, tmp_path)
    html = salida["elementos"]["seguridad-codigos"]["innerHTML"]
    assert all(c in html for c in codigos)
    assert "una sola vez" in html and "Descargar como .txt" in html


def test_un_error_del_servidor_se_muestra(tmp_path):
    salida = _panel({"respuestas": {"/api/seguridad/dos-pasos/iniciar": {
        "status": 403, "cuerpo": {"detail": "La contraseña no es correcta."}}},
        "stubs": ["cargarSeguridad"], "llamadas": [{"fn": "seguridadIniciar", "args": []}]}, tmp_path)
    assert "La contraseña no es correcta." in salida["elementos"]["seg-msg"]["innerHTML"]
    assert salida["elementos"]["seguridad-alta"]["innerHTML"] == ""
