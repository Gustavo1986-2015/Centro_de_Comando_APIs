"""
Compara dos salidas del diferencial emparejando por id y agrupa cada
diferencia por su "firma": qué campos cambian, en la base, en el evento que
se le entrega a zeep y en el XML, y de qué a qué clase de valor.

Uso: python tools/banco/diferencial/comparar.py <salida_referencia.jsonl> <salida_nueva.jsonl>
"""
import collections
import json
import re
import sys


def clase(v):
    if v is None:
        return "None"
    if isinstance(v, bool):
        return f"bool:{v}"
    if isinstance(v, (int, float)):
        return "0" if v == 0 else "num"
    if isinstance(v, str):
        return "''" if v == "" else ("'0'" if v.strip() in ("0", "0.0") else "str")
    return type(v).__name__


def firma(a, b):
    partes = set()
    if len(a["filas"]) != len(b["filas"]):
        partes.add(f"filas:{len(a['filas'])}->{len(b['filas'])}")
    for fa, fb in zip(a["filas"], b["filas"]):
        for k in sorted(set(fa) | set(fb)):
            if fa.get(k) != fb.get(k):
                partes.add(f"base.{k}:{clase(fa.get(k))}->{clase(fb.get(k))}")
    if len(a["rc"]) != len(b["rc"]):
        partes.add(f"rc:{len(a['rc'])}->{len(b['rc'])}")
    for ea, eb in zip(a["rc"], b["rc"]):
        for k in sorted(set(ea) | set(eb)):
            if ea.get(k) != eb.get(k):
                partes.add(f"rc.{k}:{clase(ea.get(k))}->{clase(eb.get(k))}")
    if (a.get("xml") or []) != (b.get("xml") or []):
        partes.add("xml")
    for k in ("error", "error_xml"):
        if a.get(k) != b.get(k):
            partes.add(f"{k}:{bool(a.get(k))}->{bool(b.get(k))}")
    return tuple(sorted(partes))


def main(ref, nueva):
    A = {json.loads(l)["id"]: json.loads(l) for l in open(ref, encoding="utf-8")}
    B = {json.loads(l)["id"]: json.loads(l) for l in open(nueva, encoding="utf-8")}
    solo_a, solo_b = set(A) - set(B), set(B) - set(A)
    clases = collections.Counter()
    ejemplos = {}
    for k in A.keys() & B.keys():
        a, b = A[k], B[k]
        f = firma(a, b)
        if not f:
            clases[(a["proveedor"], "igual")] += 1
            continue
        clases[(a["proveedor"], f)] += 1
        ejemplos.setdefault((a["proveedor"], f), k)
    print(f"Entradas: referencia {len(A)}, nueva {len(B)} | solo en referencia {sorted(solo_a)[:5]} | solo en nueva {sorted(solo_b)[:5]}")
    for (prov, f), n in sorted(clases.items(), key=lambda x: (x[0][0], str(x[0][1]))):
        print(f"  {prov:9s} {n:6d}  {f if f == 'igual' else ' | '.join(f)}" + ("" if f == "igual" else f"   ej: {ejemplos[(prov, f)]}"))
    print("Errores en la nueva:", sum(1 for v in B.values() if v["error"]), "| errores XML:", sum(1 for v in B.values() if v["error_xml"]))
    for k in sorted(solo_b):
        v = B[k]
        print("SOLO EN NUEVA:", k, json.dumps({"filas": v["filas"], "rc": v["rc"], "error": v["error"]}, ensure_ascii=False)[:600])


if __name__ == "__main__":
    main(*sys.argv[1:3])
