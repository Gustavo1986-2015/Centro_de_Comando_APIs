import os
"""
Regresión del respaldo de eventos procesados (procesados_*.jsonl).

Ese archivo es la única fuente de auditoría de lo despachado a RC: la base es un
colchón de tránsito y borra la fila apenas vence la retención. Si un campo no
está acá, se pierde para siempre.

Lo que cubre:
  · `code` presente — sin él es imposible auditar QUÉ TIPO de evento se despachó.
  · `job_id` como clave propia, tomado de la columna (no parseado de `response`).
  · `date` (hora del dispositivo) distinta de `created_at` (hora de recepción).
  · El set canónico completo, que es literalmente lo que se le manda a RC.
  · Las claves originales intactas: nombre, valor y significado.
  · Serialización JSON real, no solo la forma del dict.
"""
import json
from datetime import datetime

import pytest

from app.models.db_models import NormalizedRCEvent
from app.schemas.canonical import RCCanonicalModel
from app.worker.processor import evento_a_registro_respaldo


# Claves que ya existían antes del cambio y deben sobrevivir. `payload` NO está
# en la lista: se quitó a propósito porque duplicaba el crudo de audit/, y lo
# duplicaba N veces (los ingestores guardan el payload entero en cada evento que
# ese payload genera). Su ausencia se verifica explícitamente más abajo.
CLAVES_ORIGINALES = {
    "id", "provider", "env", "chassis", "status",
    "created_at", "response",
}


@pytest.fixture
def evento_completo():
    """Evento con todos los campos poblados, como queda tras un despacho exitoso."""
    return NormalizedRCEvent(
        id=4242,
        provider="schmitz",
        status="sent",
        raw_data='{"ChassisNumber": "AB1234", "Speed": 87.5}',
        rc_response='{"idJob": "998877", "status": "OK"}',
        job_id="998877",
        chassis_number="AB1234",
        latitude=-34.6037,
        longitude=-58.3816,
        speed=87.5,
        code="11",
        date=datetime(2026, 8, 14, 11, 30, 0),
        altitude=25.0,
        battery=12.7,
        course=180.0,
        humidity=44.0,
        ignition=True,
        odometer=154320.5,
        temperature=-18.5,
        serial_number="356938035643809",
        shipment="ENV-00912",
        vehicle_type="REEFER",
        vehicle_brand="SCHMITZ",
        vehicle_model="SCB*S3B",
        created_at=datetime(2026, 8, 14, 11, 30, 12),
        updated_at=datetime(2026, 8, 14, 11, 30, 14),
        rc_latency_sec=0.183,
        retry_count=0,
    )


def test_code_esta_presente_y_conserva_su_valor(evento_completo):
    """El motivo del cambio: sin `code` no se puede auditar el tipo de evento."""
    registro = evento_a_registro_respaldo(evento_completo, "test")
    assert "code" in registro, "Falta `code`: el respaldo vuelve a ser inauditable"
    assert registro["code"] == "11"


def test_code_nulo_se_respalda_como_null_y_no_se_omite(evento_completo):
    """Un `code` vacío tiene que quedar explícito, no desaparecer de la clave."""
    evento_completo.code = None
    registro = evento_a_registro_respaldo(evento_completo, "test")
    assert "code" in registro
    assert registro["code"] is None


def test_job_id_sale_de_la_columna_no_del_texto_de_response(evento_completo):
    """`job_id` es columna propia y poblada; no hay que parsear `response`."""
    evento_completo.rc_response = '{"otra_cosa": "sin idJob adentro"}'
    registro = evento_a_registro_respaldo(evento_completo, "test")
    assert registro["job_id"] == "998877"


def test_las_tres_fechas_son_distintas_y_no_se_confunden(evento_completo):
    """
    `date` es la hora del dispositivo (la que va a RC), `created_at` la de
    recepción en el hub y `updated_at` la del despacho. Confundirlas invalida
    cualquier análisis de latencia.
    """
    registro = evento_a_registro_respaldo(evento_completo, "test")
    assert registro["date"] == "2026-08-14T11:30:00"
    assert registro["created_at"] == "2026-08-14T11:30:12"
    assert registro["updated_at"] == "2026-08-14T11:30:14"
    assert len({registro["date"], registro["created_at"], registro["updated_at"]}) == 3


def test_fechas_nulas_no_revientan(evento_completo):
    """Un evento nunca despachado tiene updated_at en None."""
    evento_completo.date = None
    evento_completo.updated_at = None
    registro = evento_a_registro_respaldo(evento_completo, "test")
    assert registro["date"] is None
    assert registro["updated_at"] is None


def test_respalda_el_modelo_canonico_entero(evento_completo):
    """
    Todo campo que se le manda a RC tiene que poder reconstruirse desde el JSONL,
    sin la base. Si mañana se agrega un campo al canónico, este test lo exige acá.
    `chassis_number` se exceptúa: viaja con la clave histórica `chassis`.
    """
    registro = evento_a_registro_respaldo(evento_completo, "test")
    # v1.9.10: los campos excluidos del modelo (detalle_velocidad) no viajan a
    # RC; el detalle de la velocidad va al respaldo solo cuando existe (abajo).
    faltantes = [
        campo for campo, info in RCCanonicalModel.model_fields.items()
        if campo not in registro and campo != "chassis_number" and not info.exclude
    ]
    assert not faltantes, f"Campos canónicos ausentes del respaldo: {faltantes}"
    assert registro["chassis"] == "AB1234"


def test_las_claves_originales_sobreviven_intactas(evento_completo):
    """Compatibilidad: lo que se sigue escribiendo no cambió de nombre ni de valor."""
    registro = evento_a_registro_respaldo(evento_completo, "test")
    assert CLAVES_ORIGINALES.issubset(registro.keys())
    assert registro["id"] == 4242
    assert registro["provider"] == "schmitz"
    assert registro["env"] == "test"
    assert registro["chassis"] == "AB1234"
    assert registro["status"] == "sent"
    assert registro["response"] == evento_completo.rc_response


def test_el_payload_crudo_ya_no_se_duplica_en_el_respaldo(evento_completo):
    """
    El crudo vive en audit/, una sola vez y sin transformar. Repetirlo acá lo
    duplicaba N veces por payload y hacía el respaldo un 79% más pesado sin
    aportar un solo dato nuevo. Los dos archivos tienen roles separados.
    """
    registro = evento_a_registro_respaldo(evento_completo, "test")
    assert "payload" not in registro
    assert "raw_data" not in registro


def test_el_respaldo_pesa_menos_que_el_canonico_duplicado(evento_completo):
    """El registro ahora es MÁS completo que antes y no más caro: sin el crudo
    duplicado, agregar todo el canónico sale unas decenas de bytes."""
    registro = evento_a_registro_respaldo(evento_completo, "test")
    peso = len(json.dumps(registro, ensure_ascii=False))
    peso_con_crudo = peso + len(evento_completo.raw_data)
    assert peso < peso_con_crudo


def test_el_env_viene_del_argumento_no_del_modelo(evento_completo):
    """NormalizedRCEvent no tiene columna env: la base ya está shardeada por entorno."""
    assert not hasattr(NormalizedRCEvent, "env")
    assert evento_a_registro_respaldo(evento_completo, "prod")["env"] == "prod"


def test_el_registro_es_json_serializable_una_linea(evento_completo):
    """Es JSONL: si un valor no serializa, la purga rompe y se pierde el respaldo."""
    registro = evento_a_registro_respaldo(evento_completo, "test")
    linea = json.dumps(registro, ensure_ascii=False)
    assert "\n" not in linea
    assert json.loads(linea)["code"] == "11"


def test_evento_vacio_no_rompe_la_purga():
    """
    Caso borde real: fila mínima (fallo temprano, sin normalizar). La purga no
    puede caerse por esto, porque dejaría de respaldar el resto del lote.
    """
    registro = evento_a_registro_respaldo(NormalizedRCEvent(id=1), "test")
    json.dumps(registro)
    assert registro["code"] is None
    assert registro["job_id"] is None
    assert registro["chassis"] is None


# ═══════════════════════════════════════════════════════════════════════════
# Latencia del hub: datetime.now() local contra columna UTC
#
# Tercera aparición de la misma trampa. created_at lo escribe SQLite con
# CURRENT_TIMESTAMP, que es UTC. El código restaba datetime.now(), que dentro
# del contenedor devuelve hora local por el TZ=America/Argentina/Buenos_Aires
# del Dockerfile. La resta daba -10800 s, el max(...,0.0) la clampaba, y la
# latencia de proceso del hub leía CERO siempre — justo la métrica que se
# estuvo mirando para decidir sobre el SLA de certificación.
# ═══════════════════════════════════════════════════════════════════════════

import inspect
import re

from app.worker import processor


def _codigo_sin_comentarios(texto: str) -> str:
    """
    Quita los comentarios antes de analizar.

    Sin esto el propio comentario que explica el bug —que menciona
    `datetime.now()`— hacía saltar el test. Un test que se dispara con la
    documentación del arreglo no verifica nada.
    """
    return "\n".join(
        linea.split("#", 1)[0] for linea in texto.split("\n")
    )


def test_la_latencia_del_hub_no_se_calcula_con_hora_local(tmp_path):
    """
    created_at está en UTC: restarle la hora local daba 0 (o un valor de horas)
    en una zona distinta. v1.9.9 (B-20): un ciclo REAL del worker en un proceso
    con una zona horaria a 13 h de UTC, con un evento recibido hace 5 s: la
    latencia del hub da unos segundos (antes se analizaba el fuente).
    """
    import json
    for tz in ("ABC-13", "ABC+13"):
        carpeta = tmp_path / tz.replace("+", "mas").replace("-", "menos")
        carpeta.mkdir()
        salida = json.loads(_b20_en_proceso(carpeta, """
            import asyncio, json
            from datetime import datetime, timedelta, timezone
            from app.api.routers import schmitz
            from app.core.crypto import encrypt
            from app.database import get_session
            from app.models.config_models import ProviderConfig
            from app.models.db_models import NormalizedRCEvent
            from app.services import rc_soap
            from app.worker import processor
            db = get_session("system_config", "global")
            db.add(ProviderConfig(provider_name="schmitz", env="prod", provider_type="push", is_active=True,
                                  use_mock=False, rc_user="u", rc_password_enc=encrypt("x")))
            db.commit(); db.close()
            schmitz._persist_batch([({"ChassisNumber": "R1", "DeviceTime": "2026-10-04T22:23:36Z",
                                      "StatusData": [{"Position": {"Latitude": 1, "Longitude": 2}}]}, "prod", "l1")])
            db = get_session("schmitz", "prod")
            ev = db.query(NormalizedRCEvent).first()
            ev.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
            db.commit(); db.close()
            rc_soap.RC_USE_MOCK = False
            rc_soap.RCSOAPClient._send_batch_sync = lambda self, e: [{"exception": None, "idJob": 3}]
            processor.trigger_worker = lambda *a, **k: None
            asyncio.run(processor.process_provider_events("schmitz", "prod"))
            print(json.dumps(processor.obtener_promedios("schmitz", "prod")))
        """, TZ=tz))
        assert salida["avg_hub"] is not None and 3 <= salida["avg_hub"] <= 60, (tz, salida)


def test_ninguna_comparacion_contra_created_at_usa_hora_local(tmp_path):
    """
    Invariante transversal: toda comparación contra created_at o updated_at
    usa UTC (la trampa ya apareció en el reaper y en la latencia del hub).
    v1.9.9 (B-20): se EJECUTAN la purga y el rescate de atascados en un proceso
    con la hora local a 13 h de UTC, en los dos sentidos (antes se buscaba
    datetime.now() en el fuente).
    """
    import json
    for tz in ("ABC-13", "ABC+13"):
        carpeta = tmp_path / tz.replace("+", "mas").replace("-", "menos")
        carpeta.mkdir()
        salida = json.loads(_b20_en_proceso(carpeta, """
            import asyncio, json
            from datetime import datetime, timedelta, timezone
            from app.database import get_session
            from app.models.db_models import NormalizedRCEvent
            from app.core.sqlite_queue import SQLiteQueue
            from app.worker import processor
            ahora = datetime.now(timezone.utc).replace(tzinfo=None)
            db = get_session("schmitz", "prod")
            for nombre, estado, creado, cambiado in (
                    ("PURGAR", "sent", ahora - timedelta(hours=3), ahora - timedelta(hours=3)),
                    ("QUEDA", "sent", ahora - timedelta(minutes=30), ahora - timedelta(minutes=30)),
                    ("RESCATAR", "processing", ahora - timedelta(minutes=30), ahora - timedelta(minutes=20)),
                    ("EN_VUELO", "processing", ahora - timedelta(seconds=40), ahora - timedelta(seconds=30))):
                db.add(NormalizedRCEvent(provider="schmitz", status=estado, chassis_number=nombre, code="1",
                                         raw_data="{}", created_at=creado, updated_at=cambiado))
            db.commit(); db.close()
            parametros = dict(processor.obtener_parametros_rc(), retencion_horas=2)
            processor.obtener_parametros_rc = lambda: parametros
            asyncio.run(processor.purge_provider_events("schmitz", "prod"))
            asyncio.run(SQLiteQueue().recuperar_estancados("schmitz", "prod", 600))
            db = get_session("schmitz", "prod")
            print(json.dumps({e.chassis_number: e.status for e in db.query(NormalizedRCEvent).all()}))
        """, TZ=tz))
        assert salida == {"QUEDA": "sent", "RESCATAR": "pending", "EN_VUELO": "processing"}, (tz, salida)


# ─── v1.9.9 (B-20): ayuda para correr código del hub en un proceso aparte ──────
def _b20_en_proceso(tmp_path, codigo, **entorno):
    """
    Corre `codigo` con el hub importable, en una carpeta temporal y con el
    entorno dado (por ejemplo TZ, para que la hora local y la UTC difieran).
    Devuelve la última línea de la salida.
    """
    import os
    import subprocess
    import sys
    import textwrap
    from pathlib import Path
    from cryptography.fernet import Fernet
    raiz = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in ("RC_USE_MOCK", "PULL_ALLOW_INTERNAL_URLS", "TZ")}
    env.update(APP_ENV="development", MASTER_ENC_KEY=Fernet.generate_key().decode(),
               LOG_FILE_PATH=str(tmp_path / "proceso.jsonl"), PYTHONIOENCODING="utf-8")
    env.update(entorno)
    guion = tmp_path / "guion_b20.py"
    guion.write_text("import sys\nsys.path.insert(0, %r)\n" % str(raiz) + textwrap.dedent(codigo), encoding="utf-8")
    p = subprocess.run([sys.executable, str(guion)], cwd=str(tmp_path), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-3000:]
    return p.stdout.strip().splitlines()[-1]


def _b20_tz_con_otra_fecha():
    """Un TZ (formato del CRT) en el que la fecha local de HOY no es la UTC."""
    from datetime import datetime, timezone
    return "ABC-13" if datetime.now(timezone.utc).hour >= 11 else "ABC+13"


def test_el_detalle_de_la_velocidad_va_al_respaldo_solo_si_existe(evento_completo):
    """v1.9.10: con velocidad estimada (Tive) el respaldo la trae; sin ella, el registro es el de v1.9.9."""
    import json
    sin = evento_a_registro_respaldo(evento_completo, "test")
    assert "velocidad_detalle" not in sin
    evento_completo.velocidad_detalle = json.dumps({"valor": 34, "motivo": "estimada"})
    con = evento_a_registro_respaldo(evento_completo, "test")
    assert con["velocidad_detalle"] == {"valor": 34, "motivo": "estimada"}
    assert list(con)[:-1] == list(sin), "las claves de siempre conservan su orden"
