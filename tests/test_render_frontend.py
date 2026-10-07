"""
Verifica que el JavaScript del panel EJECUTE, no solo que compile.

POR QUÉ EXISTE

`node --check` valida la sintaxis y nada más. Una variable que se usa pero
quedó sin definir pasa ese chequeo y explota recién en el navegador, donde el
`catch` la convierte en un discreto "No se pudo consultar el diagnóstico".

Eso pasó de verdad: un reemplazo se llevó por delante tres definiciones
—`avisoSla`, `colorSla` y el bloque de barras— la sintaxis siguió siendo
válida, los 687 tests siguieron en verde, y la sección del panel quedó muerta.

Este test arma la respuesta real del endpoint, ejecuta las funciones de render
con Node y comprueba que produzcan el HTML completo.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFICADOR = os.path.join(RAIZ, "tools", "verificar_render.js")


def _hay_node() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _hay_node(), reason="Node no está disponible en este entorno")
def test_el_render_del_diagnostico_ejecuta_sin_errores(tmp_path):
    """
    Arma datos reales del endpoint y ejecuta el render. Si falta una variable,
    Node lo dice acá en vez de que el panel muestre un error genérico.
    """
    from app.core import latencia

    latencia.limpiar()
    for i in range(300):
        c = latencia.Cronometro("schmitz", "prod")
        c.marca("auth")
        c.marca("parseo")
        c.marca("rate_limit")
        if i % 50 == 0:
            time.sleep(0.002)
        c.marca("encolado")
        c.cerrar()

    # La sonda del bucle tiene que haber corrido: sin sondeos ese bloque no se
    # dibuja, y el verificador lo reclamaría con razón.
    async def _sondear_un_rato():
        import asyncio
        t = asyncio.create_task(latencia.sondear_bucle_eventos())
        await asyncio.sleep(0.5)
        t.cancel()

    import asyncio
    asyncio.run(_sondear_un_rato())

    datos = latencia.desglose("schmitz", "prod")
    datos["integraciones"] = latencia.integraciones_medidas()
    datos["retraso_bucle"] = latencia.retraso_bucle()

    archivo = tmp_path / "diagnostico.json"
    archivo.write_text(json.dumps(datos), encoding="utf-8")

    resultado = subprocess.run(
        ["node", VERIFICADOR, str(archivo)],
        cwd=RAIZ, capture_output=True, text=True, timeout=60,
    )
    latencia.limpiar()

    assert resultado.returncode == 0, (
        f"El render del panel falló al ejecutarse:\n{resultado.stderr}\n{resultado.stdout}"
    )
    assert "render OK" in resultado.stdout


@pytest.mark.skipif(not _hay_node(), reason="Node no está disponible en este entorno")
def test_el_render_soporta_una_respuesta_vacia(tmp_path):
    """Recién arrancado, sin tráfico, el panel no puede romperse."""
    from app.core import latencia

    latencia.limpiar()
    datos = latencia.desglose()
    datos["integraciones"] = []
    datos["retraso_bucle"] = latencia.retraso_bucle()

    archivo = tmp_path / "vacio.json"
    archivo.write_text(json.dumps(datos), encoding="utf-8")

    resultado = subprocess.run(
        ["node", VERIFICADOR, str(archivo)],
        cwd=RAIZ, capture_output=True, text=True, timeout=60,
    )
    # Sin muestras el render sale temprano: lo que importa es que no lance.
    assert "ReferenceError" not in resultado.stderr, (
        f"El render rompe con una respuesta vacía:\n{resultado.stderr}"
    )


@pytest.mark.skipif(not _hay_node(), reason="Node no está disponible en este entorno")
def test_el_selector_de_autenticacion_del_panel_ejecuta_bien():
    """
    Ejecuta el JavaScript real del selector de autenticación de webhooks y de
    los campos de OAuth2. Verifica, entre otras cosas, que un proveedor
    histórico siga en "Clave fija" y que un esquema HMAC armado a mano no se
    pierda al guardar la tabla de proveedores.
    """
    resultado = subprocess.run(
        ["node", os.path.join(RAIZ, "tools", "verificar_auth_ui.js")],
        cwd=RAIZ, capture_output=True, text=True, timeout=60,
    )
    assert resultado.returncode == 0, f"{resultado.stdout}\n{resultado.stderr}"
    assert "Todo OK" in resultado.stdout


@pytest.mark.skipif(not _hay_node(), reason="Node no está disponible en este entorno")
def test_guardar_el_integration_studio_no_borra_el_filtro_de_admision():
    """
    Bug real: guardar armaba el esquema con tres claves y nada más. Un filtro
    de admisión importado por YAML desaparecía la primera vez que alguien
    tocaba el Integration Studio, sin aviso. Ejecuta el guardado real.
    """
    resultado = subprocess.run(
        ["node", os.path.join(RAIZ, "tools", "verificar_esquema_ui.js")],
        cwd=RAIZ, capture_output=True, text=True, timeout=60,
    )
    assert resultado.returncode == 0, f"{resultado.stdout}\n{resultado.stderr}"
    assert "Todo OK" in resultado.stdout
