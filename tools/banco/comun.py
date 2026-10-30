"""
Rutas y datos compartidos por los arneses del banco de pruebas (v1.9.11).

El banco NO trae datos reales: los lee de una carpeta del hub en marcha (la de
producción local), en solo lectura, indicada con variables de entorno:

  BANCO_DATOS    carpeta del hub con audit/<proveedor>_<entorno>/AAAA-MM/crudos_*.jsonl
                 y docs/configuracion_hub_*.yaml (el YAML exportado del servidor).
                 Obligatoria.
  BANCO_YAML     ruta del YAML del servidor. Por defecto, el más nuevo de
                 BANCO_DATOS/docs/configuracion_hub_*.yaml.
  BANCO_TRABAJO  carpeta de salida (bases, salidas, copias funcionales). Por
                 defecto, <temporal>/cca_banco. Nunca dentro del repo.
"""
import glob
import json
import os
import tempfile

RAIZ_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def datos() -> str:
    ruta = os.environ.get("BANCO_DATOS")
    if not ruta or not os.path.isdir(ruta):
        raise SystemExit("Falta BANCO_DATOS: la carpeta del hub con audit/ y docs/ (solo se lee).")
    return os.path.abspath(ruta)


def yaml_servidor() -> str:
    if os.environ.get("BANCO_YAML"):
        return os.path.abspath(os.environ["BANCO_YAML"])
    candidatos = sorted(glob.glob(os.path.join(datos(), "docs", "configuracion_hub_*.yaml")))
    if not candidatos:
        raise SystemExit("No hay docs/configuracion_hub_*.yaml en BANCO_DATOS; indicar BANCO_YAML.")
    return candidatos[-1]


def trabajo(*partes) -> str:
    base = os.environ.get("BANCO_TRABAJO") or os.path.join(tempfile.gettempdir(), "cca_banco")
    ruta = os.path.join(os.path.abspath(base), *partes)
    os.makedirs(ruta, exist_ok=True)
    return ruta


def crudos(proveedor_entorno: str) -> list[str]:
    """Archivos de crudos de una integración, del más viejo al más nuevo."""
    return sorted(glob.glob(os.path.join(datos(), "audit", proveedor_entorno, "*", "crudos_*.jsonl")))


def payloads(ruta: str, limite: int | None = None) -> list:
    salida = []
    for linea in open(ruta, encoding="utf-8", errors="replace"):
        try:
            salida.append(json.loads(linea)["payload"])
        except Exception:
            continue
    return salida[:limite] if limite else salida
