"""
Levanta (y deja corriendo) el hub de una copia funcional ya preparada, para
mirarla en el navegador. Mismo entorno que prueba_funcional.py.

  python tools/banco/funcional/levantar.py <etiqueta> [archivo .py a ejecutar antes, dentro de la copia]
"""
import os
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.argv = [sys.argv[0], "-", sys.argv[1]] + sys.argv[2:]
import prueba_funcional as pf  # noqa: E402

DIR = os.path.join(pf.comun.trabajo("funcional"), sys.argv[2])
pf.DIR = DIR
env = pf.entorno()
if len(sys.argv) > 3:
    r = subprocess.run([sys.executable, os.path.abspath(sys.argv[3])], cwd=DIR, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout[-2000:], r.stderr[-2000:])
log = open(os.path.join(DIR, "servidor_navegador.log"), "w", encoding="utf-8")
subprocess.run([sys.executable, "_lanzar.py", str(pf.PUERTO_HUB)], cwd=DIR, env=env, stdout=log,
               stderr=subprocess.STDOUT)
