"""
Mutaciones: rompe cada regla, corre los tests que la cubren (por nombre, con
-k sobre toda la carpeta tests/) y exige que fallen. Restaura SIEMPRE el
archivo y verifica al final que el árbol quedó idéntico byte a byte.

Uso: python tools/banco/mutaciones/mutaciones.py <raíz del worktree> <lista.py> [<lista.py> ...]
Cada lista define MUTACIONES = [(nombre, archivo, viejo, nuevo, expresión -k)].
Una regla con dos defensas (v1.9.11) se rompe entera con una lista de cambios
en lugar de archivo: (nombre, [(archivo, viejo, nuevo), ...], None, None, -k).
Las cadenas se buscan con fin de línea \\n (el archivo se normaliza y se
vuelve a escribir con el suyo).
"""
import hashlib
import os
import runpy
import subprocess
import sys

RAIZ = os.path.abspath(sys.argv[1])
MUTACIONES = []
for lista in sys.argv[2:]:
    MUTACIONES += runpy.run_path(lista)["MUTACIONES"]


def cambios(mutacion) -> list[tuple[str, str, str]]:
    _, archivo, viejo, nuevo, _ = mutacion
    return list(archivo) if isinstance(archivo, (list, tuple)) else [(archivo, viejo, nuevo)]


def huella():
    h = hashlib.sha256()
    for archivo in sorted({c[0] for m in MUTACIONES for c in cambios(m)}):
        h.update(open(os.path.join(RAIZ, archivo), "rb").read())
    return h.hexdigest()


def main():
    antes = huella()
    sobrevivientes, errores = [], []
    for mutacion in MUTACIONES:
        nombre, tests = mutacion[0], mutacion[4]
        originales = {}
        try:
            fallo = None
            for archivo, viejo, nuevo in cambios(mutacion):
                ruta = os.path.join(RAIZ, archivo)
                if ruta not in originales:
                    originales[ruta] = open(ruta, "rb").read()
                texto = open(ruta, "rb").read().decode("utf-8")
                crlf = "\r\n" in originales[ruta].decode("utf-8")
                plano = texto.replace("\r\n", "\n")
                n = plano.count(viejo)
                if n != 1:
                    fallo = f"el texto a mutar aparece {n} veces en {archivo}"
                    break
                mutado = plano.replace(viejo, nuevo)
                open(ruta, "wb").write((mutado.replace("\n", "\r\n") if crlf else mutado).encode("utf-8"))
            if fallo:
                errores.append(nombre)
                print(f"ERROR      {nombre}: {fallo}", flush=True)
                continue
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                                "-o", "addopts=", "tests", "-k", tests],
                               cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace")
            ultima = (r.stdout.strip().splitlines() or ["(sin salida)"])[-1]
            if r.returncode == 0 or "no tests ran" in ultima or " deselected" in ultima and "failed" not in ultima \
                    and "error" not in ultima:
                sobrevivientes.append(nombre)
                print(f"SOBREVIVE  {nombre}  ({ultima})", flush=True)
            else:
                print(f"DETECTADA  {nombre}  ({ultima})", flush=True)
        finally:
            for ruta, original in originales.items():
                open(ruta, "wb").write(original)
    print(f"\n{len(MUTACIONES)} mutaciones, {len(MUTACIONES) - len(sobrevivientes) - len(errores)} detectadas, "
          f"{len(sobrevivientes)} sin detectar: {sobrevivientes}, {len(errores)} con error: {errores}")
    print("Árbol restaurado: idéntico byte a byte." if huella() == antes else "¡EL ÁRBOL CAMBIÓ!")


main()
