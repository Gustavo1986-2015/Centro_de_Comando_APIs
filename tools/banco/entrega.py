"""
Arma la entrega: CRLF en el árbol de trabajo para lo nuevo/modificado (como
el resto del checkout, core.autocrlf=true), diff completo contra la base con
un índice temporal (sin tocar el índice real ni commitear), stat y ZIP con los
archivos nuevos/modificados más BORRAR.txt.

Uso: python tools/banco/entrega.py <worktree> <base> <carpeta de salida> <version>
"""
import os
import subprocess
import sys
import zipfile

RAIZ, BASE, SALIDA, VERSION = os.path.abspath(sys.argv[1]), sys.argv[2], os.path.abspath(sys.argv[3]), sys.argv[4]
os.makedirs(SALIDA, exist_ok=True)


def git(*args, env=None, binario=False):
    r = subprocess.run(["git", *args], cwd=RAIZ, env=env, capture_output=True)
    if r.returncode:
        raise SystemExit(r.stderr.decode("utf-8", "replace"))
    return r.stdout if binario else r.stdout.decode("utf-8", "replace")


indice = os.path.join(SALIDA, "indice_temporal")
if os.path.exists(indice):
    os.remove(indice)
env = dict(os.environ, GIT_INDEX_FILE=indice)
git("read-tree", BASE, env=env)
git("add", "-A", "--", ".", env=env)
estado = git("diff", "--cached", "--name-status", "--no-renames", BASE, env=env).splitlines()

TEXTO = (".py", ".js", ".html", ".css", ".md", ".txt", ".ini", ".example", ".yaml", ".yml", ".json", ".gitignore")
normalizados = 0
for linea in estado:
    marca, ruta = linea.split("\t", 1)
    if marca == "D" or not (ruta.endswith(TEXTO) or os.path.basename(ruta) == ".gitignore"):
        continue
    p = os.path.join(RAIZ, ruta)
    crudo = open(p, "rb").read()
    nuevo = crudo.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if nuevo != crudo:
        open(p, "wb").write(nuevo)
        normalizados += 1
git("add", "-A", "--", ".", env=env)

diff = git("diff", "--cached", "--binary", "--no-renames", BASE, env=env, binario=True)
open(os.path.join(SALIDA, f"v{VERSION}.diff"), "wb").write(diff)
stat = git("diff", "--cached", "--stat", "--no-renames", BASE, env=env)
open(os.path.join(SALIDA, f"v{VERSION}.stat.txt"), "w", encoding="utf-8").write(stat)

borrados = [l.split("\t", 1)[1] for l in estado if l.startswith("D\t")]
cambiados = [l.split("\t", 1)[1] for l in estado if not l.startswith("D\t")]
with zipfile.ZipFile(os.path.join(SALIDA, f"v{VERSION}.zip"), "w", zipfile.ZIP_DEFLATED) as z:
    for ruta in cambiados:
        z.write(os.path.join(RAIZ, ruta), ruta)
    z.writestr("BORRAR.txt", f"Archivos que v{VERSION} quita (borrarlos al copiar el ZIP sobre la versión anterior):\r\n"
               + ("".join(f"{r}\r\n" for r in borrados) or "(ninguno)\r\n"))
os.remove(indice)
print(f"{len(cambiados)} nuevos/modificados, {len(borrados)} borrados, {normalizados} pasados a CRLF")
print("\n".join(estado))
print(stat.splitlines()[-1])
