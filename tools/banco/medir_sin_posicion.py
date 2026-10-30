"""
Medición del Bloque 2a (v1.9.11): cuántos eventos de Tive pasan de descarte a
envío con "enviar_sin_posicion", separados en alertas y posiciones.

Pasa los crudos reales de Tive (BANCO_DATOS/audit/tive_prod/) por el módulo
REAL de este checkout dos veces, con el interruptor apagado (= v1.9.10) y
encendido, y con cada juego de interruptores:

  - los del YAML del servidor (comun.yaml_servidor), completados con los
    valores por defecto del módulo;
  - los que se pasen en OPCIONES_LOCALES (JSON), p. ej. los de la base local.

  python tools/banco/medir_sin_posicion.py

El estado del módulo (pares aprendidos, duplicados) va a una carpeta temporal.
No se envía nada ni se escribe en BANCO_DATOS.
"""
import collections
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import comun  # noqa: E402

sys.path.insert(0, comun.RAIZ_REPO)
os.chdir(tempfile.mkdtemp(prefix="medir_sin_posicion_"))

import logging  # noqa: E402
import yaml  # noqa: E402

logging.disable(logging.WARNING)

from app.core import descartes  # noqa: E402
from app.providers.tive import estado, modulo  # noqa: E402


def _opciones_yaml():
    datos = yaml.safe_load(open(comun.yaml_servidor(), encoding="utf-8"))
    for prov in datos.get("proveedores") or []:
        if str(prov.get("nombre", "")).lower() == "tive" and str(prov.get("entorno", "prod")).lower() == "prod":
            return prov.get("opciones_modulo") or {}
    return {}


def _correr(payloads, opciones):
    """Eventos que salen, por clase: alerta o posición."""
    estado.cerrar_todo()
    estado.DIRECTORIO = tempfile.mkdtemp(prefix="estado_tive_")
    salen = collections.Counter()
    sin_posicion = collections.Counter()
    for i, p in enumerate(payloads):
        clase = "alertas" if isinstance(p.get("Alert"), dict) else "posiciones"
        for ev in modulo.procesar(p, "prod", opciones, f"m{i}"):
            salen[clase] += 1
            if ev.sin_posicion:
                sin_posicion[clase] += 1
    estado.cerrar_todo()
    return salen, sin_posicion


FIXTURE = os.path.join(comun.RAIZ_REPO, "tests", "fixtures", "tive_crudos_2026-10.jsonl")


def main():
    descartes.DIRECTORIO = tempfile.mkdtemp(prefix="descartes_")
    archivos = comun.crudos("tive_prod")
    fuentes = {
        "crudos reales": [p for ruta in archivos for p in comun.payloads(ruta)],
        # El fixture del repo (01-02/10 y casos de otros días, recortado): trae
        # las alertas reales con Location vacía que los crudos de hoy no tienen.
        "fixture del repo": sorted(comun.payloads(FIXTURE), key=lambda p: p.get("EntryTimeEpoch") or 0),
    }
    juegos = {"servidor (YAML)": _opciones_yaml()}
    if os.environ.get("OPCIONES_LOCALES"):
        juegos["local"] = json.loads(os.environ["OPCIONES_LOCALES"])
    print(f"Crudos reales de Tive: {len(fuentes['crudos reales'])} payloads en {len(archivos)} archivo(s): "
          f"{', '.join(os.path.basename(a) for a in archivos)}")
    resultado = {}
    for fuente, payloads in fuentes.items():
        for nombre, opciones in juegos.items():
            efectivas = modulo.opciones_efectivas(opciones)
            apagado, _ = _correr(payloads, {**efectivas, "enviar_sin_posicion": False})
            encendido, sin_pos = _correr(payloads, {**efectivas, "enviar_sin_posicion": True})
            resultado[f"{fuente} · interruptores {nombre}"] = {
                "payloads": len(payloads),
                "interruptores": {k: v for k, v in efectivas.items() if k != "enviar_sin_posicion"},
                "salen_apagado_v1910": dict(apagado), "salen_encendido": dict(encendido),
                "pasan_de_descarte_a_envio": dict(sin_pos),
            }
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
