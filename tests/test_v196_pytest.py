"""
v1.9.6 · Punto 2 — pytest resuelve testpaths sin aviso.

El conftest de la v1.9.5 cambiaba de carpeta al importarse. pytest resuelve
`testpaths` dos veces (antes y después de importar el conftest), y la
segunda vez, ya en la carpeta temporal, no encontraba tests/: avisaba
"No files were found in testpaths" y buscaba recursivamente desde la carpeta
de lanzamiento. El cambio de carpeta ahora va en pytest_configure.
"""
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def test_testpaths_se_resuelve_sin_aviso():
    """Como en la carpeta del hub: lanzada desde la raíz, con testpaths = tests."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider",
         "-o", "testpaths=tests", "-o", "addopts=", "-W", "error::pytest.PytestConfigWarning"],
        cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    salida = r.stdout + r.stderr
    assert "PytestConfigWarning" not in salida
    assert r.returncode == 0, salida[-2000:]
    assert "testpaths: tests" in salida, "el encabezado tiene que volver a mostrar testpaths"


def test_la_suite_sigue_corriendo_fuera_del_repositorio():
    """El aislamiento de la v1.9.5 se mantiene con el cambio movido."""
    cwd = Path.cwd().resolve()
    assert RAIZ not in (cwd, *cwd.parents), f"la suite corre dentro del repositorio: {cwd}"
