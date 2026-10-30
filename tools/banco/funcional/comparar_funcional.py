"""Compara dos resúmenes de prueba_funcional.py (claves planas, sin tiempos).

  python tools/banco/funcional/comparar_funcional.py <a.json> <b.json>
"""
import json, sys
a=json.load(open(sys.argv[1],encoding="utf-8")); b=json.load(open(sys.argv[2],encoding="utf-8"))
def plano(d,pre=""):
    out={}
    for k,v in d.items():
        if isinstance(v,dict): out.update(plano(v,pre+k+"."))
        else: out[pre+k]=v
    return out
A,B=plano(a),plano(b)
for k in sorted(set(A)|set(B)):
    if k in ("etiqueta","raiz") or "_ms" in k or "midiendo" in k or "compliance" in k: continue
    if A.get(k)!=B.get(k): print(f"{k}: {A.get(k)} -> {B.get(k)}")
print("(fin de diferencias)")
