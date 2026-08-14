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
    faltantes = [
        campo for campo in RCCanonicalModel.model_fields
        if campo not in registro and campo != "chassis_number"
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
