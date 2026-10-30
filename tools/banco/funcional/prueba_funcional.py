"""
Prueba funcional con la app levantada (copia aislada, RC simulado).

  python tools/banco/funcional/prueba_funcional.py <raíz del código> <etiqueta> [--mantener]

1. Copia app/, frontend/ y main.py a BANCO_TRABAJO/funcional/<etiqueta>/ (base nueva).
2. Prepara la configuración: importa el YAML del servidor (ver comun.py) con el
   endpoint real de importación y carga credenciales DE PRUEBA como estarían en
   producción (la contraseña del diccionario de Protrack en texto plano dentro
   de enrichment_config, como la tiene la base de producción).
   BANCO_ANTES=<archivo .py>: se ejecuta dentro de la copia después de preparar
   y antes de levantar (por ejemplo, para sembrar una base vieja).
   OPCIONES_TIVE='{"velocidad_equipos": true}': pisa interruptores de Tive.
3. Levanta un Protrack y una API de Tive simulados en 127.0.0.1:8802 que
   EXIGEN las credenciales (firma md5 de Protrack, client_secret de Tive), y
   un RC_ENDPOINT local: nada sale de la máquina.
4. Levanta el hub (uvicorn) con RC_USE_MOCK=True. La tarea de telemetría se
   reemplaza por una vacía EN ESTE PROCESO (sin contacto externo; el código del
   hub no se toca).
5. Tráfico real grabado: crudos de Schmitz por /Json/Data con su clave, crudos
   de Tive (el último día de BANCO_DATOS) por /webhook/dynamic/tive con firma
   HMAC, y un ciclo PULL de Protrack (diccionario + sondeo) contra el simulado.
6. Resume: colas por estado y código, descartes, diccionario, salud,
   peticiones al simulado, errores del log. Escribe <etiqueta>.json junto a la copia.
"""
import base64
import collections
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, ".."))
import comun  # noqa: E402

YAML_REAL = comun.yaml_servidor()
CRUDOS_SCHMITZ = (comun.crudos("schmitz_prod") or comun.crudos("schmitz_test"))[-1]
CRUDOS_PROTRACK = comun.crudos("protrack_prod")[-1]
CRUDOS_TIVE = comun.crudos("tive_prod")[-1]
DICCIONARIO = os.path.join(comun.trabajo("diferencial"), "diccionario.json")

PUERTO_HUB, PUERTO_SIM = 8801, 8802
USUARIO, CLAVE_PANEL = "prueba", "prueba-local"
CLAVE_SCHMITZ = "clave-schmitz-de-prueba-1234"
SECRETO_TIVE = "secreto-hmac-tive-de-prueba"
PASS_PROTRACK = "pass-telemetria-prueba-77"
PASS_DICCIONARIO = "pass-diccionario-prueba-88"
TIVE_CLIENT_SECRET = "tive-client-secret-prueba-99"
TOKEN_PROTRACK = "TOKENPROTRACKPRUEBA0123456789"
N_SCHMITZ = 300

RAIZ, ETIQUETA = os.path.abspath(sys.argv[1]), sys.argv[2]
MANTENER = "--mantener" in sys.argv
DIR = os.path.join(comun.trabajo("funcional"), ETIQUETA)


# ─── Simuladores ────────────────────────────────────────────────────────────

pedidos = []
dicc = json.load(open(DICCIONARIO, encoding="utf-8"))
ultimo_por_imei = {}
for linea in open(CRUDOS_PROTRACK, encoding="utf-8"):
    try:
        p = json.loads(linea)["payload"]
    except Exception:
        continue
    if isinstance(p, dict) and "imei" in p:
        ultimo_por_imei[str(p["imei"])] = p


def firma_valida(q, clave):
    t, s = q.get("time", [""])[0], q.get("signature", [""])[0]
    return s == hashlib.md5((hashlib.md5(clave.encode()).hexdigest() + t).encode()).hexdigest()


class Simulador(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, codigo, cuerpo):
        datos = json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        registro = {"metodo": "GET", "ruta": u.path}
        pedidos.append(registro)
        if u.path == "/api/authorization":
            cuenta = q.get("account", [""])[0]
            ok = cuenta == "prueba.maersk" and (firma_valida(q, PASS_PROTRACK) or firma_valida(q, PASS_DICCIONARIO))
            registro["auth_ok"] = ok
            registro["clave"] = "telemetria" if firma_valida(q, PASS_PROTRACK) else (
                "diccionario" if firma_valida(q, PASS_DICCIONARIO) else "ninguna")
            if not ok:
                return self._json(200, {"code": 10003, "message": "signature error"})
            return self._json(200, {"code": 0, "record": {"access_token": TOKEN_PROTRACK, "expires_in": 7200}})
        if u.path in ("/api/device/list", "/api/track"):
            if q.get("access_token", [""])[0] != TOKEN_PROTRACK:
                registro["token_ok"] = False
                return self._json(200, {"code": 10011, "message": "access_token error"})
            registro["token_ok"] = True
            if u.path == "/api/device/list":
                return self._json(200, {"code": 0, "record": [{"imei": k, "platenumber": v} for k, v in dicc.items()]})
            imeis = (q.get("imeis", [""])[0] or "").split(",")
            registro["imeis"] = len([i for i in imeis if i])
            return self._json(200, {"code": 0, "record": [ultimo_por_imei[i] for i in imeis if i in ultimo_por_imei]})
        if u.path == "/tive/v3/Accounts":
            return self._json(200, {"data": [{"accountId": 11341}], "next": None})
        if u.path.startswith("/tive/v3/Devices/"):
            serie = urllib.parse.unquote(u.path.rsplit("/", 1)[1])
            registro["serie"] = serie
            if serie == "867860087520523":
                return self._json(200, {"deviceId": serie, "deviceName": "J7777777"})
            return self._json(404, {"message": "not found"})
        return self._json(404, {"message": "ruta no simulada"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        largo = int(self.headers.get("Content-Length") or 0)
        cuerpo = self.rfile.read(largo).decode("utf-8", "replace")
        registro = {"metodo": "POST", "ruta": u.path}
        pedidos.append(registro)
        if u.path == "/tive/authenticate":
            ok = TIVE_CLIENT_SECRET in cuerpo
            registro["auth_ok"] = ok
            if not ok:
                return self._json(401, {"message": "invalid client"})
            return self._json(200, {"access_token": "TIVETOKENPRUEBA987654321", "expires_in": 3600})
        return self._json(404, {"message": "ruta no simulada"})


# ─── Preparación de la copia ────────────────────────────────────────────────

PREPARAR = r'''
import json, os, sys
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
import main
from app.core.crypto import encrypt
from app.database import get_session
from app.models.config_models import ProviderConfig
c = TestClient(main.app)
auth = (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])
r = c.post("/api/config/import", json={"contenido": open(sys.argv[1], encoding="utf-8").read(),
                                         "sobrescribir": True, "confirmacion": "IMPORTAR"}, auth=auth)
print("import", r.status_code, r.text[:300])
sim = "http://127.0.0.1:%s" % sys.argv[2]
db = get_session("system_config", "global")
for conf in db.query(ProviderConfig).all():
    conf.rc_password_enc = encrypt("rc-prueba")
    if conf.provider_name == "schmitz":
        conf.webhook_auth_secret_enc = encrypt(sys.argv[3])
    if conf.provider_name == "tive":
        conf.webhook_auth_secret_enc = encrypt(sys.argv[4])
        # v1.9.10: OPCIONES_TIVE='{"velocidad_equipos": true, ...}' enciende interruptores.
        if os.environ.get("OPCIONES_TIVE"):
            conf.module_options = dict(conf.module_options or {}, **json.loads(os.environ["OPCIONES_TIVE"]))
        conf.fetch_config_enc = encrypt(json.dumps({
            "url": sim + "/tive/v3", "auth_type": "oauth2_client_credentials",
            "token_url": sim + "/tive/authenticate", "token_body_format": "multipart",
            "auth_user": "Envios Assistcargo", "auth_pass": sys.argv[7]}))
    if (conf.provider_name, conf.env) == ("protrack", "prod"):
        conf.run_interval_sec = 10
        conf.fetch_config_enc = encrypt(json.dumps({
            "url": sim + "/api/track", "method": "GET", "auth_type": "protrack",
            "auth_user": "prueba.maersk", "auth_pass": sys.argv[5]}))
        enr = dict(conf.enrichment_config or {})
        enr.update(url=sim + "/api/device/list?access_token=ACCESS_TOKEN", auth_pass=sys.argv[6])
        conf.enrichment_config = enr   # como está hoy en producción: en texto plano
db.commit()
print({(p.provider_name, p.env): (p.provider_type, p.is_active, p.use_mock) for p in db.query(ProviderConfig).all()})
db.close()
'''

LANZAR = r'''
import asyncio, os, sys
sys.path.insert(0, os.getcwd())
import app.core.health_metrics as hm
async def _sin_contacto_externo():
    await asyncio.Event().wait()
hm.telemetry_daemon_loop = _sin_contacto_externo
import uvicorn, main
uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
'''


def entorno():
    from cryptography.fernet import Fernet
    clave_archivo = os.path.join(DIR, "clave.txt")
    if not os.path.exists(clave_archivo):
        open(clave_archivo, "w").write(Fernet.generate_key().decode())
    e = dict(os.environ)
    e.update(MASTER_ENC_KEY=open(clave_archivo).read().strip(), APP_ENV="development", RC_USE_MOCK="True",
             RC_ENDPOINT=f"http://127.0.0.1:{PUERTO_SIM}/rc/no-usar", DASHBOARD_USER=USUARIO,
             DASHBOARD_PASSWORD=CLAVE_PANEL, LOG_FILE_PATH=os.path.join(DIR, "app.jsonl"),
             PULL_ALLOW_INTERNAL_URLS="True", PYTHONIOENCODING="utf-8")
    return e


def preparar():
    shutil.rmtree(DIR, ignore_errors=True)
    os.makedirs(DIR)
    for carpeta in ("app", "frontend"):
        shutil.copytree(os.path.join(RAIZ, carpeta), os.path.join(DIR, carpeta))
    shutil.copy(os.path.join(RAIZ, "main.py"), DIR)
    open(os.path.join(DIR, "_preparar.py"), "w", encoding="utf-8").write(PREPARAR)
    open(os.path.join(DIR, "_lanzar.py"), "w", encoding="utf-8").write(LANZAR)
    r = subprocess.run([sys.executable, "_preparar.py", YAML_REAL, str(PUERTO_SIM), CLAVE_SCHMITZ, SECRETO_TIVE,
                        PASS_PROTRACK, PASS_DICCIONARIO, TIVE_CLIENT_SECRET],
                       cwd=DIR, env=entorno(), capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout.strip()[-800:])
    if r.returncode:
        print(r.stderr[-3000:])
        raise SystemExit("falló la preparación")
    if os.environ.get("BANCO_ANTES"):
        r = subprocess.run([sys.executable, os.path.abspath(os.environ["BANCO_ANTES"])], cwd=DIR, env=entorno(),
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("BANCO_ANTES:", r.stdout.strip()[-800:], r.stderr.strip()[-1500:] if r.returncode else "")
        if r.returncode:
            raise SystemExit("falló BANCO_ANTES")


def firma_tive(cuerpo: bytes) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    dig = hmac.new(SECRETO_TIVE.encode(), f"{ts}.{cuerpo.decode()}".encode(), hashlib.sha256).digest()
    return f"t={ts},v1={base64.b64encode(dig).decode()}"


def crudos(ruta, limite=None):
    salida = []
    for linea in open(ruta, encoding="utf-8", errors="replace"):
        try:
            salida.append(json.loads(linea)["payload"])
        except Exception:
            continue
    return salida[:limite] if limite else salida


def esperar(cond, segundos, paso=1.0):
    fin = time.time() + segundos
    while time.time() < fin:
        if cond():
            return True
        time.sleep(paso)
    return False


def resumen_bases():
    r = {}
    for prov in ("schmitz", "protrack", "tive"):
        ruta = os.path.join(DIR, "db", prov, "prod.db")
        if not os.path.exists(ruta):
            r[prov] = None
            continue
        con = sqlite3.connect(ruta)
        r[prov] = {
            "por_estado": dict(con.execute("SELECT status, COUNT(*) FROM normalized_rc_events GROUP BY status")),
            "por_codigo": dict(con.execute("SELECT code, COUNT(*) FROM normalized_rc_events GROUP BY code")),
            "patentes": con.execute("SELECT COUNT(DISTINCT chassis_number) FROM normalized_rc_events").fetchone()[0],
            "velocidad_null": con.execute("SELECT COUNT(*) FROM normalized_rc_events WHERE speed IS NULL").fetchone()[0],
            "ignicion_null": con.execute("SELECT COUNT(*) FROM normalized_rc_events WHERE ignition IS NULL").fetchone()[0],
            "con_job_id": con.execute("SELECT COUNT(*) FROM normalized_rc_events WHERE job_id IS NOT NULL").fetchone()[0],
        }
        con.close()
    return r


def foto_config():
    """provider_config completo (sin columnas que el propio hub reescribe al usar)."""
    con = sqlite3.connect(os.path.join(DIR, "db", "system_config_global.db"))
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM provider_config ORDER BY id")]
    finally:
        con.close()


def main():
    preparar()
    foto_antes = foto_config()
    servidor_sim = ThreadingHTTPServer(("127.0.0.1", PUERTO_SIM), Simulador)
    threading.Thread(target=servidor_sim.serve_forever, daemon=True).start()
    log_hub = open(os.path.join(DIR, "servidor.log"), "w", encoding="utf-8")
    hub = subprocess.Popen([sys.executable, "_lanzar.py", str(PUERTO_HUB)], cwd=DIR, env=entorno(),
                           stdout=log_hub, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{PUERTO_HUB}"
    auth = (USUARIO, CLAVE_PANEL)
    res = {"etiqueta": ETIQUETA, "raiz": RAIZ}
    try:
        cli = httpx.Client(timeout=30)
        assert esperar(lambda: _ok(cli, base + "/health"), 60), "el hub no levantó"

        # Schmitz: crudos reales por /Json/Data con su clave.
        codigos = collections.Counter()
        for p in crudos(CRUDOS_SCHMITZ, N_SCHMITZ):
            codigos[cli.post(base + "/Json/Data?env=prod", json=p, headers={"x-api-key": CLAVE_SCHMITZ}).status_code] += 1
        codigos_mala = cli.post(base + "/Json/Data?env=prod", json={}, headers={"x-api-key": "mala"}).status_code
        res["schmitz_http"] = {"enviados": sum(codigos.values()), "codigos": dict(codigos), "clave_mala": codigos_mala}

        # Tive: crudos reales por el webhook genérico, firmados.
        codigos = collections.Counter()
        for p in crudos(CRUDOS_TIVE):
            cuerpo = json.dumps(p).encode()
            codigos[cli.post(base + "/webhook/dynamic/tive?env=prod", content=cuerpo,
                             headers={"Content-Type": "application/json",
                                      "x-tive-signature": firma_tive(cuerpo)}).status_code] += 1
        cuerpo = b'{"x": 1}'
        res["tive_http"] = {"enviados": sum(codigos.values()), "codigos": dict(codigos),
                            "firma_mala": cli.post(base + "/webhook/dynamic/tive?env=prod", content=cuerpo,
                                                   headers={"x-tive-signature": "t=2026-01-01 00:00:00Z,v1=AAAA"}).status_code}

        # Protrack: diccionario + al menos un sondeo con datos.
        res["protrack_ciclo"] = esperar(lambda: any(p["ruta"] == "/api/track" and p.get("imeis") for p in pedidos), 120)
        # Resolutor de Tive: corre cada 60 s.
        esperar(lambda: any(p["ruta"].startswith("/tive/v3/Devices/") for p in pedidos), 90)
        # Que el worker drene la cola.
        esperar(lambda: _pendientes(cli, base, auth) == 0, 120, 2)
        time.sleep(3)

        stats = cli.get(base + "/api/stats", auth=auth).json()
        res["stats"] = {k: stats.get(k) for k in ("pending", "sent", "failed", "retries")}
        res["salud"] = {f"{h.get('provider')}:{h.get('env')}": {k: h.get(k) for k in
                        ("status", "detail", "last_fetch_error", "last_auth_error", "dict_count", "mode")}
                        for h in stats.get("provider_health", [])}
        res["push_per_provider"] = stats.get("push_per_provider")
        desc = cli.get(base + "/api/diagnostico/descartes?limite=500", auth=auth).json()
        res["descartes"] = sorted([(r["proveedor"], r["origen"], r["motivo"], r["total"]) for r in desc.get("resumen", [])])
        historial = cli.get(base + "/api/history", auth=auth).json()
        claves = [(h["date"], h["provider"], h["env"]) for h in historial]
        res["historial"] = {"filas": len(historial), "repetidas": len(claves) - len(set(claves)),
                            "enviados": sum(h["sent_count"] or 0 for h in historial)}
        res["panel_http"] = {r: cli.get(base + r, auth=auth).status_code for r in
                             ("/dashboard", "/api/config", "/api/maintenance/db-stats", "/api/db-viewer/databases",
                              "/api/config/export", "/api/export/opciones")}
    finally:
        hub.terminate()
        try:
            hub.wait(20)
        except subprocess.TimeoutExpired:
            hub.kill()
        servidor_sim.shutdown()
        log_hub.close()

    res["bases"] = resumen_bases()
    # B-21 (v1.9.9): la base migrada del YAML real no cambia ninguna fila al arrancar.
    foto_despues = foto_config()
    cambios = [(a.get("provider_name"), a.get("env"), k, a.get(k), b.get(k))
               for a, b in zip(foto_antes, foto_despues) for k in a if a.get(k) != b.get(k)
               and k not in ("enrichment_config",)]   # 2.4 cifra la contraseña del diccionario al arrancar
    res["config_al_arrancar"] = {"filas_antes": len(foto_antes), "filas_despues": len(foto_despues),
                                 "cambios": [list(map(str, c)) for c in cambios]}
    con = sqlite3.connect(os.path.join(DIR, "db", "system_config_global.db"))
    res["diccionario_filas"] = con.execute("SELECT COUNT(*) FROM provider_dictionary WHERE provider_name='protrack' AND env='prod'").fetchone()[0]
    con.close()
    ruta_estado = os.path.join(DIR, "db", "tive", "prod_estado.db")
    if os.path.exists(ruta_estado):
        con = sqlite3.connect(ruta_estado)
        res["tive_estado"] = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                              for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
    # v1.9.10: velocidad estimada de Tive en la cola (si la versión la tiene).
    ruta_cola = os.path.join(DIR, "db", "tive", "prod.db")
    if os.path.exists(ruta_cola):
        con = sqlite3.connect(ruta_cola)
        columnas = {r[1] for r in con.execute("PRAGMA table_info(normalized_rc_events)")}
        velocidades = collections.Counter()
        for speed, detalle in con.execute("SELECT speed, " + ("velocidad_detalle" if "velocidad_detalle" in columnas
                                          else "NULL") + " FROM normalized_rc_events"):
            if detalle:
                d = json.loads(detalle)
                clave = (f"{d['motivo']} {(d.get('anterior') or {}).get('metodo') or '-'}->"
                         f"{(d.get('actual') or {}).get('metodo') or '-'}")
                rango = "0" if d["valor"] == 0 else "1" if d["valor"] == 1 else "2-9" if d["valor"] < 10 \
                    else "10-129" if d["valor"] < 130 else ">=130"
                velocidades[f"{clave} | {rango}"] += 1
            else:
                velocidades[f"sin detalle | speed={speed}"] += 1
        res["tive_velocidad"] = dict(sorted(velocidades.items()))
        con.close()
    # v1.9.11: daily_stats (filas por día/integración y duplicados), lo enviado
    # a RC guardado por evento y los eventos de Tive sin posición. Las versiones
    # que no tienen esas columnas dan None.
    con = sqlite3.connect(os.path.join(DIR, "db", "system_config_global.db"))
    res["daily_stats"] = {
        "filas": [list(f) for f in con.execute(
            "SELECT date, provider, env, sent_count, failed_count, recovered_count FROM daily_stats ORDER BY 1, 2, 3")],
        "duplicados": con.execute("SELECT COUNT(*) FROM (SELECT 1 FROM daily_stats GROUP BY date, lower(provider), "
                                  "lower(env) HAVING COUNT(*) > 1)").fetchone()[0],
        "indice_unico": bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='ux_daily_stats_dia'").fetchone()),
    }
    con.close()
    for prov in ("schmitz", "protrack", "tive"):
        ruta_cola = os.path.join(DIR, "db", prov, "prod.db")
        if not os.path.exists(ruta_cola):
            continue
        con = sqlite3.connect(ruta_cola)
        columnas = {r[1] for r in con.execute("PRAGMA table_info(normalized_rc_events)")}
        res.setdefault("v1911", {})[prov] = {
            "con_rc_payload": con.execute("SELECT COUNT(*) FROM normalized_rc_events WHERE rc_payload IS NOT NULL")
                                 .fetchone()[0] if "rc_payload" in columnas else None,
            "sin_posicion": con.execute("SELECT COUNT(*) FROM normalized_rc_events WHERE latitude IS NULL "
                                        "OR longitude IS NULL").fetchone()[0],
            "cero_cero": con.execute("SELECT COUNT(*) FROM normalized_rc_events WHERE latitude = 0 AND longitude = 0")
                            .fetchone()[0],
        }
        con.close()
    res["simulador"] = dict(collections.Counter(
        f"{p['metodo']} {p['ruta'] if not p['ruta'].startswith('/tive/v3/Devices/') else '/tive/v3/Devices/*'}"
        f"{'' if p.get('auth_ok', True) and p.get('token_ok', True) else ' (RECHAZADO)'}"
        f"{' clave=' + p['clave'] if 'clave' in p else ''}" for p in pedidos))
    errores = collections.Counter()
    for linea in open(os.path.join(DIR, "app.jsonl"), encoding="utf-8", errors="replace"):
        try:
            r = json.loads(linea)
        except Exception:
            continue
        if r.get("levelname") in ("ERROR", "CRITICAL"):
            errores[str(r.get("message"))[:140]] += 1
    res["errores_log"] = errores.most_common(25)
    res["secretos_en_log"] = {s: open(os.path.join(DIR, "app.jsonl"), encoding="utf-8", errors="replace").read().count(s)
                              for s in (TOKEN_PROTRACK, PASS_PROTRACK, PASS_DICCIONARIO, TIVE_CLIENT_SECRET, CLAVE_SCHMITZ)}
    json.dump(res, open(os.path.join(comun.trabajo("funcional"), f"{ETIQUETA}.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False, default=str)
    print(json.dumps(res, indent=1, ensure_ascii=False, default=str)[:6000])
    if not MANTENER:
        pass


def _ok(cli, url):
    try:
        return cli.get(url).status_code == 200
    except Exception:
        return False


def _pendientes(cli, base, auth):
    try:
        return cli.get(base + "/api/stats", auth=auth).json().get("pending")
    except Exception:
        return None


if __name__ == "__main__":
    main()
