"""
v1.9.7 — Arreglos previos al despliegue (auditoría completa del 06/10/2026).

  1. B-2: la migración de arranque borraba la configuración PULL. Con
     fetch_config guardado como el TEXTO 'null' (lo dejaban el panel y la
     importación al asignar None a la columna JSON), la migración lo cifraba
     ENCIMA de fetch_config_enc. Ahora: None se guarda como NULL de SQL, el
     texto vacío no se cifra y lo cifrado nunca se pisa.
  2. B-14: lo simulado ya no se guarda como 'sent'. Queda 'simulado' y no
     cuenta como enviado a RC (contador diario, latencia de RC), pero sí lo
     purgan la retención y el respaldo, y lo lista la descarga con su estado.

Todos los tests EJECUTAN el código (migración, worker, purga, endpoints y el
JavaScript del panel): ninguno busca texto en el fuente.
"""
import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.core.crypto import decrypt, encrypt

RAIZ = Path(__file__).resolve().parent.parent
CONFIG_PULL = {"url": "http://api.protrack365.com/api/track", "method": "GET",
               "auth_type": "protrack", "auth_user": "prueba", "auth_pass": "secreta"}


def _ruta_config():
    return os.path.join("db", "system_config_global.db")


def _crear_protrack(**columnas):
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="protrack", env="prod", provider_type="pull", is_active=True,
                          use_mock=False, mapping_schema={"base_mapping": {"chassis_number": "imei"}},
                          **columnas))
    db.commit()
    db.close()


def _crudo(columnas="fetch_config, fetch_config_enc, rc_password, rc_password_enc"):
    con = sqlite3.connect(_ruta_config())
    try:
        return con.execute(f"SELECT {columnas} FROM provider_config WHERE provider_name='protrack'").fetchone()
    finally:
        con.close()


def _escribir_crudo(**valores):
    con = sqlite3.connect(_ruta_config())
    sets = ", ".join(f"{k} = ?" for k in valores)
    con.execute(f"UPDATE provider_config SET {sets} WHERE provider_name='protrack'", list(valores.values()))
    con.commit()
    con.close()


# ═══════════════════════════════════════════════════════════════════════════
# 1. B-2 — La migración no borra la configuración PULL
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("legado", ["null", "None", "", "{}", " NULL "])
def test_la_migracion_no_pisa_la_config_pull_con_un_vacio_legado(config_aislada, legado):
    """El caso de producción: fetch_config con el texto 'null' y la credencial cifrada."""
    from app import database
    _crear_protrack(fetch_config_enc=encrypt(json.dumps(CONFIG_PULL)))
    _escribir_crudo(fetch_config=legado)
    database.check_and_migrate_db()
    plano, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL, "la migración pisó la credencial cifrada"
    assert plano is None, "el vacío legado tiene que quedar como NULL de SQL"


def test_un_plano_real_sin_cifrar_se_sigue_cifrando(config_aislada):
    """La migración de siempre (texto plano heredado) sigue funcionando."""
    from app import database
    _crear_protrack()
    _escribir_crudo(fetch_config=json.dumps(CONFIG_PULL), fetch_config_enc=None)
    database.check_and_migrate_db()
    plano, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL
    assert plano is None


def test_con_cifrado_y_plano_real_no_pisa_y_avisa(config_aislada, caplog):
    from app import database
    _crear_protrack(fetch_config_enc=encrypt(json.dumps(CONFIG_PULL)))
    _escribir_crudo(fetch_config=json.dumps({"url": "http://otro/viejo"}))
    with caplog.at_level(logging.WARNING, logger="app.database"):
        database.check_and_migrate_db()
    _, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL
    assert "No se pisa la cifrada" in caplog.text


def test_la_contrasena_de_rc_cifrada_tampoco_se_pisa(config_aislada):
    from app import database
    _crear_protrack(rc_password_enc=encrypt("la-buena"))
    _escribir_crudo(rc_password="una-vieja-en-plano")
    database.check_and_migrate_db()
    _, _, _, rc_cifrada = _crudo()
    assert decrypt(rc_cifrada) == "la-buena"


def test_el_arranque_real_sobre_una_base_v186_conserva_protrack(config_aislada):
    """
    Cableado: el camino del despliegue. Una base como la de producción v1.8.6
    (fetch_config = 'null' y la credencial cifrada) y el arranque de la versión
    nueva, que corre la migración al abrir el motor de la base.
    """
    from app import database
    _crear_protrack(fetch_config_enc=encrypt(json.dumps(CONFIG_PULL)))
    _escribir_crudo(fetch_config="null")
    database._engines.clear()       # como un proceso recién arrancado
    database._sessions.clear()
    database.get_engine("system_config", "global")
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    from app.worker.pull_engine import _load_fetch_config
    db = get_session("system_config", "global")
    try:
        conf = db.query(ProviderConfig).filter_by(provider_name="protrack").one()
        assert _load_fetch_config(conf) == CONFIG_PULL
    finally:
        db.close()


def _cliente_panel():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import admin_config, config_backup
    app = FastAPI()
    app.include_router(admin_config.router)
    app.include_router(config_backup.router)
    return TestClient(app)


def test_cableado_guardar_desde_el_panel_y_reiniciar(config_aislada):
    """Panel → base → reinicio: la configuración PULL sobrevive."""
    from app import database
    _crear_protrack()
    c = _cliente_panel()
    auth = (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])
    (fila,) = c.get("/api/config", auth=auth).json()
    r = c.post("/api/config", auth=auth, json=[{
        "id": fila["id"], "is_active": True, "use_mock": False, "rc_user": "", "rc_password": "",
        "purge_interval_min": 15, "run_interval_sec": 55, "queue_backend": "sqlite",
        "enable_state_dedup": True, "fetch_config": json.dumps(CONFIG_PULL)}])
    assert r.status_code == 200, r.text
    con = sqlite3.connect(_ruta_config())
    assert con.execute("SELECT typeof(fetch_config) FROM provider_config").fetchone()[0] == "null", \
        "el panel guardó el texto 'null' en vez de NULL de SQL"
    con.close()
    database.check_and_migrate_db()
    _, cifrado, _, _ = _crudo()
    assert json.loads(decrypt(cifrado)) == CONFIG_PULL


def test_cableado_la_importacion_guarda_null_de_sql(config_aislada):
    c = _cliente_panel()
    auth = (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])
    yaml_ = ("formato: 1\nproveedores:\n- nombre: protrack\n  entorno: prod\n  tipo: pull\n"
             "  telemetria:\n    url: http://api.protrack365.com/api/track\n    auth_type: protrack\n")
    r = c.post("/api/config/import", auth=auth, json={"contenido": yaml_, "sobrescribir": True,
                                                       "confirmacion": "IMPORTAR"})
    assert r.status_code == 200, r.text
    con = sqlite3.connect(_ruta_config())
    tipo, cifrado = con.execute("SELECT typeof(fetch_config), fetch_config_enc FROM provider_config "
                                "WHERE provider_name='protrack'").fetchone()
    con.close()
    assert tipo == "null"
    assert json.loads(decrypt(cifrado))["url"] == "http://api.protrack365.com/api/track"


def test_capa_modelo_asignar_none_por_el_orm_guarda_null_de_sql(config_aislada):
    """Cualquier camino que asigne None (no solo los cuatro conocidos) deja NULL."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig
    _crear_protrack(fetch_config={"url": "x"})
    db = get_session("system_config", "global")
    db.query(ProviderConfig).filter_by(provider_name="protrack").one().fetch_config = None
    db.commit()
    db.close()
    assert _crudo("typeof(fetch_config)")[0] == "null"


def test_capa_panel_guarda_null_aunque_el_modelo_no_lo_haga(config_aislada, monkeypatch):
    """Los cuatro lugares escriben NULL por sí mismos, sin depender del modelo."""
    from app.models.config_models import ProviderConfig
    monkeypatch.setattr(ProviderConfig.__table__.c.fetch_config.type, "none_as_null", False)
    _crear_protrack()
    c = _cliente_panel()
    auth = (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])
    (fila,) = c.get("/api/config", auth=auth).json()
    r = c.post("/api/config", auth=auth, json=[{
        "id": fila["id"], "is_active": True, "use_mock": False, "rc_user": "", "rc_password": "",
        "purge_interval_min": 15, "run_interval_sec": 55, "queue_backend": "sqlite",
        "enable_state_dedup": True, "fetch_config": json.dumps(CONFIG_PULL)}])
    assert r.status_code == 200, r.text
    assert _crudo("typeof(fetch_config)")[0] == "null"


# ═══════════════════════════════════════════════════════════════════════════
# 2. B-14 — Lo simulado no se guarda ni se cuenta como enviado
# ═══════════════════════════════════════════════════════════════════════════

PAYLOAD_SCHMITZ = {
    "ChassisNumber": "R5868BDP", "Plate": "R-5868-BDP", "CtuId": 123052056,
    "DeviceTime": "2026-10-04T22:23:36Z",
    "StatusData": [{"Position": {"Latitude": 42.440728, "Longitude": -3.492675,
                                 "GPSSpeed": {"exists": True, "Value": 84}},
                    "SensorStatus": {"IsIgnitionOn": True}}],
    "Reason": {"Item": True, "ItemElementName": "Standard"}, "Events": [{"Type": "Standard"}],
}


@pytest.fixture
def schmitz_con_un_evento(config_aislada):
    """Schmitz/prod con un evento pendiente, por el camino real de ingesta."""
    from app.api.routers import schmitz
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    def preparar(use_mock: bool):
        db = get_session("system_config", "global")
        db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                              use_mock=use_mock, rc_user="AC_avl_SchmitzCargoBull",
                              rc_password_enc=encrypt("x")))
        db.commit()
        db.close()
        schmitz._persist_batch([(dict(PAYLOAD_SCHMITZ), "prod", "v197")])
    return preparar


def _correr_worker(monkeypatch, rc_llamadas=None, respuesta_rc=None):
    """Un ciclo real del worker. Si se pasa respuesta_rc, RC 'responde' eso."""
    from app.services import rc_soap
    from app.worker import processor
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    if respuesta_rc is not None:
        async def _enviar(self, eventos):
            if rc_llamadas is not None:
                rc_llamadas.append(len(eventos))
            if self.use_mock or rc_soap.RC_USE_MOCK:
                return await _original(self, eventos)
            return [respuesta_rc for _ in eventos]
        _original = rc_soap.RCSOAPClient.send_events_batch
        monkeypatch.setattr(rc_soap.RCSOAPClient, "send_events_batch", _enviar)

    async def ciclo():
        await processor.process_provider_events("schmitz", "prod")
        await asyncio.sleep(0.3)   # el contador diario se escribe en segundo plano
    asyncio.run(ciclo())


def _eventos():
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session("schmitz", "prod")
    try:
        return [(e.status, e.job_id) for e in db.query(NormalizedRCEvent).all()]
    finally:
        db.close()


def _enviados_hoy():
    from app.api.routers.dashboard import _totales_del_dia_sync
    return _totales_del_dia_sync()["sent"]


def test_en_modo_simulado_queda_simulado_y_no_suma_enviados(schmitz_con_un_evento, monkeypatch):
    schmitz_con_un_evento(use_mock=True)
    llamadas = []
    _correr_worker(monkeypatch, llamadas, respuesta_rc=("no", "debería", "usarse", None))
    ((estado, job_id),) = _eventos()
    assert estado == "simulado"
    assert job_id.startswith("job_mock_")
    assert _enviados_hoy() == 0, "lo simulado se contó como enviado a RC"


def test_un_envio_real_sigue_quedando_sent_y_suma(schmitz_con_un_evento, monkeypatch):
    from app.services.rc_soap import RCResponseCategory
    schmitz_con_un_evento(use_mock=False)
    monkeypatch.setattr("app.services.rc_soap.RC_USE_MOCK", False)
    llamadas = []
    _correr_worker(monkeypatch, llamadas,
                   respuesta_rc=(True, "1791152622925", "{'idJob': 1791152622925}", RCResponseCategory.SUCCESS))
    assert llamadas == [1], "un envío real tiene que llamar a RC"
    assert _eventos() == [("sent", "1791152622925")]
    assert _enviados_hoy() == 1


def test_la_purga_borra_lo_simulado_y_lo_respalda_con_su_estado(schmitz_con_un_evento, monkeypatch):
    from app.worker.processor import purge_provider_events
    schmitz_con_un_evento(use_mock=True)
    _correr_worker(monkeypatch, respuesta_rc=("x", "x", "x", None))
    asyncio.run(purge_provider_events("schmitz", "prod", ignorar_retencion=True))
    assert _eventos() == [], "lo simulado quedó para siempre en la base"
    respaldos = list(Path("db", "backups_diarios").rglob("*.jsonl"))
    lineas = [json.loads(l) for r in respaldos for l in r.read_text(encoding="utf-8").splitlines()]
    assert [l["status"] for l in lineas] == ["simulado"]


def _app_panel():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import dashboard, exports
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(exports.router)
    return TestClient(app), (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


def test_cableado_mantenimiento_cuenta_lo_simulado_como_purgable(schmitz_con_un_evento, monkeypatch):
    schmitz_con_un_evento(use_mock=True)
    _correr_worker(monkeypatch, respuesta_rc=("x", "x", "x", None))
    # Que ya haya vencido la retención, para que cuente como purgable.
    con = sqlite3.connect(os.path.join("db", "schmitz", "prod.db"))
    con.execute("UPDATE normalized_rc_events SET updated_at = datetime('now', '-3 days')")
    con.commit()
    con.close()
    c, auth = _app_panel()
    r = c.get("/api/maintenance/db-stats", auth=auth)
    assert r.status_code == 200, r.text
    (schmitz,) = [d for d in r.json()["databases"] if d["provider"] == "schmitz"]
    assert schmitz["by_status"]["simulado"] == 1
    assert schmitz["by_status"]["sent"] == 0
    assert schmitz["purgeable"] == 1


def test_cableado_la_descarga_lista_lo_simulado_con_su_estado(schmitz_con_un_evento, monkeypatch):
    from datetime import date
    schmitz_con_un_evento(use_mock=True)
    _correr_worker(monkeypatch, respuesta_rc=("x", "x", "x", None))
    c, auth = _app_panel()
    hoy = date.today().isoformat()
    r = c.get(f"/api/export/enviados?provider=schmitz&env=prod&desde={hoy}&hasta={hoy}", auth=auth)
    assert r.status_code == 200, r.text
    filas = [l.split(";") for l in r.text.lstrip("﻿").splitlines()]
    cab = filas[0]
    estados = [f[cab.index("status")] for f in filas[1:]]
    assert estados == ["simulado"]


def test_cableado_el_panel_lo_pinta_como_simulado(schmitz_con_un_evento, monkeypatch, tmp_path):
    """Worker real → GET /api/stats real → renderRecentTable real (Node)."""
    if not shutil.which("node"):
        pytest.skip("Node no está disponible")
    schmitz_con_un_evento(use_mock=True)
    _correr_worker(monkeypatch, respuesta_rc=("x", "x", "x", None))
    c, auth = _app_panel()
    r = c.get("/api/stats", auth=auth)
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == 0, "la tarjeta ENVIADOS (HOY) contó lo simulado"
    entrada, salida = tmp_path / "stats.json", tmp_path / "filas.json"
    entrada.write_text(r.text, encoding="utf-8")
    p = subprocess.run(["node", str(RAIZ / "tools" / "verificar_panel_v197.js"), str(entrada), str(salida)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    (fila,) = json.loads(salida.read_text(encoding="utf-8"))
    assert "row-simulado" in fila["clases"] and "row-sent" not in fila["clases"]
    assert "Simulado (no enviado a RC)" in fila["texto"]
    assert "Enviado" not in fila["texto"].replace("Enviado RC", "")


# ═══════════════════════════════════════════════════════════════════════════
# 4. B-26 — El aviso de alerta de sello ya no dice que nunca se vio una
# ═══════════════════════════════════════════════════════════════════════════

def test_el_aviso_de_alerta_de_sello_esta_al_dia(tmp_path, monkeypatch, caplog):
    """Ejecuta el módulo con una alerta de sello y lee el aviso que emite."""
    import copy
    from app.providers.tive import estado, modulo
    from tests.test_v195_tive import CRUDOS, CIERRE_2284
    estado.cerrar_todo()
    monkeypatch.setattr(estado, "DIRECTORIO", str(tmp_path / "estado"))
    alerta = copy.deepcopy(CRUDOS[CIERRE_2284])
    for nodo in (alerta, alerta["Alert"]):
        nodo["DeviceId"] = nodo["DeviceName"] = nodo["EntityName"] = "A2A2A2017BF2"
    try:
        with caplog.at_level(logging.INFO, logger="app.providers.tive.modulo"):
            (ev,) = modulo.procesar(alerta, "prod", {}, "sello-1")
    finally:
        estado.cerrar_todo()
    assert ev.chassis_number == "A2A2A2017BF2", "el sello sale con su número, que es su patente"
    assert "aún no se vio" not in caplog.text
    assert "confirmada" in caplog.text and "A2A2A2017BF2" in caplog.text
