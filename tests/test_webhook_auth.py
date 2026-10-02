"""
Autenticación de webhooks: secreto fijo y firma HMAC, transversal.

CONTEXTO

El webhook dinámico solo sabía comparar un secreto fijo en un header. Los
proveedores que FIRMAN cada petición —Tive, y muchos otros— mandan un valor
que cambia en cada envío, así que esa comparación nunca coincidía: todo su
tráfico habría recibido 401.

El esquema de Tive se verificó contra https://developers.tive.com/docs/webhook-signatures:

    x-tive-signature: t=2022-10-31 20:56:28Z,v1=<base64>
    contenido firmado: "<timestamp>.<cuerpo crudo>"
    HMAC SHA-256 con el secretKey del webhook, en base64

Dos detalles que un encargo previo NO traía y que habrían roto la
implementación: el timestamp va con ESPACIO entre fecha y hora (no con "T"),
y la firma solo viene si el webhook tiene secretKey cargado.
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core import webhook_auth as wa

SECRETO = "clave-secreta-del-webhook"


def _firmar_tive(cuerpo: bytes, secreto: str = SECRETO, ts: str | None = None) -> dict:
    """Arma el header exactamente como lo describe la documentación de Tive."""
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    digest = hmac.new(secreto.encode(), f"{ts}.".encode() + cuerpo, hashlib.sha256).digest()
    return {"x-tive-signature": f"t={ts},v1={base64.b64encode(digest).decode()}"}


def _cfg_tive(**sobrescribir):
    return wa.resolver_config({"modo": "hmac", "preset": "tive", **sobrescribir})


# ═══════════════════════════════════════════════════════════════════════════
# Resolución de la configuración
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_configuracion_es_el_modo_historico():
    """
    Garantía de compatibilidad: una integración que no configura nada se
    autentica exactamente igual que antes. Protrack y los webhooks dinámicos
    existentes no se enteran del cambio.
    """
    for vacio in (None, {}, {"modo": ""}, {"modo": "header"}):
        assert wa.resolver_config(vacio) == {"modo": "header"}


def test_el_preset_de_tive_trae_su_esquema_verificado():
    cfg = _cfg_tive()
    assert cfg["header"] == "x-tive-signature"
    assert cfg["contenido"] == "{ts}.{body}"
    assert cfg["codificacion"] == "base64"
    # El espacio del timestamp está en el patrón, no una T.
    assert "} [0-9]" in cfg["patron"]


def test_un_preset_se_puede_sobrescribir_campo_por_campo():
    cfg = _cfg_tive(tolerancia_seg=60)
    assert cfg["tolerancia_seg"] == 60
    assert cfg["header"] == "x-tive-signature"


def test_se_puede_describir_un_esquema_sin_preset():
    """Transversal: cualquier proveedor con HMAC se configura sin tocar código."""
    cfg = wa.resolver_config({
        "modo": "hmac", "header": "x-firma",
        "patron": r"^sha256=(?P<firma>[0-9a-f]+)$",
        "contenido": "{body}", "codificacion": "hex",
    })
    assert cfg["modo"] == "hmac"


@pytest.mark.parametrize("config, motivo", [
    ({"modo": "hmac", "preset": "inventado"}, "Preset de firma desconocido"),
    ({"modo": "hmac"}, "faltan"),
    ({"modo": "hmac", "preset": "tive", "contenido": "{ts}"}, "{body}"),
    ({"modo": "hmac", "preset": "tive", "codificacion": "rot13"}, "base64 o hex"),
    ({"modo": "hmac", "preset": "tive", "patron": "(sin cerrar"}, "expresión regular"),
])
def test_una_configuracion_rota_se_rechaza_con_motivo(config, motivo):
    """Fallar al configurar, no cuando el proveedor empieza a mandar tráfico."""
    with pytest.raises(ValueError, match=motivo.replace("{", r"\{").replace("}", r"\}")):
        wa.resolver_config(config)


# ═══════════════════════════════════════════════════════════════════════════
# Verificación HMAC con el esquema de Tive
# ═══════════════════════════════════════════════════════════════════════════

def test_acepta_una_firma_valida():
    cuerpo = b'{"DeviceId": "0001", "AccountId": 123}'
    wa.verificar_hmac(cuerpo, _firmar_tive(cuerpo), SECRETO, _cfg_tive())


def test_el_ejemplo_literal_de_la_documentacion_de_tive():
    """
    El cuerpo y el timestamp del ejemplo oficial. La ventana de tolerancia se
    desactiva porque el ejemplo es de 2022.
    """
    cuerpo = b'{"Property1": 123,"Property2": "abc"}'
    headers = _firmar_tive(cuerpo, ts="2022-10-31 20:56:28Z")
    wa.verificar_hmac(cuerpo, headers, SECRETO, _cfg_tive(tolerancia_seg=0))


def test_rechaza_un_cuerpo_reserializado():
    """
    El error más probable al implementar: parsear el JSON y volver a
    serializarlo antes de verificar. Cambian los espacios y la firma deja de
    coincidir aunque el contenido sea el mismo.
    """
    original = b'{"Property1": 123, "Property2": "abc"}'
    headers = _firmar_tive(original)
    reserializado = json.dumps(json.loads(original), separators=(",", ":")).encode()

    assert reserializado != original
    with pytest.raises(wa.FirmaInvalida, match="no coincide"):
        wa.verificar_hmac(reserializado, headers, SECRETO, _cfg_tive())


def test_rechaza_un_cuerpo_alterado():
    cuerpo = b'{"AccountId": 123}'
    headers = _firmar_tive(cuerpo)
    with pytest.raises(wa.FirmaInvalida, match="no coincide"):
        wa.verificar_hmac(b'{"AccountId": 999}', headers, SECRETO, _cfg_tive())


def test_rechaza_con_otro_secreto():
    cuerpo = b'{"a": 1}'
    with pytest.raises(wa.FirmaInvalida, match="no coincide"):
        wa.verificar_hmac(cuerpo, _firmar_tive(cuerpo, secreto="otro"), SECRETO, _cfg_tive())


def test_rechaza_el_timestamp_con_T():
    """El formato de Tive usa espacio. Una "T" no es el mismo formato."""
    cuerpo = b'{"a": 1}'
    firma = _firmar_tive(cuerpo)["x-tive-signature"].replace(" ", "T", 1)
    with pytest.raises(wa.FirmaInvalida, match="formato esperado"):
        wa.verificar_hmac(cuerpo, {"x-tive-signature": firma}, SECRETO, _cfg_tive())


def test_rechaza_una_firma_vieja():
    """
    Una petición legítima capturada y reenviada tiene firma válida. Lo que la
    delata es la antigüedad del timestamp.
    """
    cuerpo = b'{"a": 1}'
    viejo = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%SZ")
    with pytest.raises(wa.FirmaInvalida, match="ventana de tolerancia"):
        wa.verificar_hmac(cuerpo, _firmar_tive(cuerpo, ts=viejo), SECRETO, _cfg_tive())


def test_rechaza_si_falta_el_header():
    """Tive solo firma si el webhook tiene secretKey: el motivo lo tiene que decir."""
    with pytest.raises(wa.FirmaInvalida, match="secreta"):
        wa.verificar_hmac(b'{"a": 1}', {}, SECRETO, _cfg_tive())


def test_esquema_hex_sin_timestamp():
    """Transversal: otra variante habitual, descripta por configuración."""
    cfg = wa.resolver_config({
        "modo": "hmac", "header": "x-firma",
        "patron": r"^sha256=(?P<firma>[0-9a-f]+)$",
        "contenido": "{body}", "codificacion": "hex",
    })
    cuerpo = b'{"evento": "posicion"}'
    firma = hmac.new(SECRETO.encode(), cuerpo, hashlib.sha256).hexdigest()
    wa.verificar_hmac(cuerpo, {"x-firma": f"sha256={firma}"}, SECRETO, cfg)

    with pytest.raises(wa.FirmaInvalida):
        wa.verificar_hmac(b'{"evento": "otro"}', {"x-firma": f"sha256={firma}"}, SECRETO, cfg)


def test_modo_header_compara_en_tiempo_constante():
    wa.verificar_header("abc", "abc", "x-api-key")
    with pytest.raises(wa.FirmaInvalida, match="incorrecta"):
        wa.verificar_header("abd", "abc", "x-api-key")
    with pytest.raises(wa.FirmaInvalida, match="falta el header"):
        wa.verificar_header("", "abc", "x-api-key")


# ═══════════════════════════════════════════════════════════════════════════
# Cableado en el webhook dinámico — contra la aplicación real
# ═══════════════════════════════════════════════════════════════════════════

MAPEO_TIVE = {
    "base_mapping": {
        "chassis_number": "DeviceName",
        "latitude": "Location.Latitude",
        "longitude": "Location.Longitude",
        "date": "EntryTimeUtc",
    },
}

PAYLOAD_TELEMETRIA = {
    "AccountId": 123, "DeviceId": "0001", "DeviceName": "VD0001",
    "EntryTimeUtc": "2026-09-30T12:00:00", "EntryTimeEpoch": 1790000000000,
    "Location": {"Latitude": -34.6, "Longitude": -58.4},
}


@pytest.fixture
def app_con_tive(tmp_path, monkeypatch):
    """
    Solo el router del webhook dinámico, sobre bases temporales, con una
    integración 'tive' configurada con firma HMAC.

    Se levanta el router y no la app completa: es el patrón probado de la
    suite, y evita los estáticos y los efectos de arranque. Se aíslan los dos
    cachés de base (_engines y _sessions) y el de rate limit, igual que el
    fixture del respaldo de configuración.
    """
    from cryptography.fernet import Fernet
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import database
    from app.core import crypto, rate_limit, safety_net

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MASTER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_MASTER_KEY_CACHE", None)

    engines, sessions = dict(database._engines), dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()
    monkeypatch.setattr(safety_net, "DIRECTORIO_BASE", str(tmp_path / "red"))
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()

    from app.api.routers import dynamic_webhook
    from app.core.crypto import encrypt
    from app.models.config_models import ProviderConfig
    from app.models.db_models import NormalizedRCEvent

    database.check_and_migrate_provider_db("system_config", "global")
    db = database.get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="tive", env="prod", provider_type="push", is_active=True,
        use_mock=True, webhook_auth_secret_enc=encrypt(SECRETO),
        webhook_auth_config={"modo": "hmac", "preset": "tive"},
        mapping_schema=MAPEO_TIVE, rc_user="u", rc_password_enc=encrypt("p"),
    ))
    db.commit()
    db.close()
    NormalizedRCEvent.metadata.create_all(bind=database.get_engine("tive", "prod"))
    database.check_and_migrate_provider_db("tive", "prod")

    app = FastAPI()
    app.include_router(dynamic_webhook.router)
    yield TestClient(app)

    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines)
    database._sessions.update(sessions)
    rate_limit._db_limit_cache.clear()
    safety_net._anexadores.clear()
    safety_net._cache_estado.clear()


def _filas(provider="tive"):
    from app.database import get_session
    from app.models.db_models import NormalizedRCEvent
    db = get_session(provider, "prod")
    try:
        return db.query(NormalizedRCEvent).count()
    finally:
        db.close()


def test_el_webhook_acepta_una_peticion_firmada(app_con_tive):
    cuerpo = json.dumps(PAYLOAD_TELEMETRIA).encode()
    r = app_con_tive.post("/webhook/dynamic/tive?env=prod", content=cuerpo,
                          headers={"content-type": "application/json", **_firmar_tive(cuerpo)})
    assert r.status_code == 200, r.text
    assert _filas() == 1


def test_el_webhook_rechaza_una_firma_invalida_sin_guardar_nada(app_con_tive):
    cuerpo = json.dumps(PAYLOAD_TELEMETRIA).encode()
    r = app_con_tive.post("/webhook/dynamic/tive?env=prod", content=cuerpo,
                          headers={"content-type": "application/json",
                                   **_firmar_tive(cuerpo, secreto="falsa")})
    assert r.status_code == 401
    assert _filas() == 0, "Se guardó un evento con firma inválida"


def test_el_webhook_verifica_sobre_los_bytes_recibidos(app_con_tive):
    """
    Cableado, no solo lógica: si el handler parseara el JSON antes de
    verificar, este cuerpo con espacios particulares fallaría.
    """
    cuerpo = b'{ "AccountId":123,   "DeviceName": "VD0001", "EntryTimeUtc": "2026-09-30T12:00:00",' \
             b' "Location": {"Latitude": -34.6, "Longitude": -58.4} }'
    r = app_con_tive.post("/webhook/dynamic/tive?env=prod", content=cuerpo,
                          headers={"content-type": "application/json", **_firmar_tive(cuerpo)})
    assert r.status_code == 200, r.text


def test_el_webhook_no_acepta_la_api_key_fija_en_modo_hmac(app_con_tive):
    """
    Con firma configurada, mandar el secreto en un header ya no alcanza: el
    secreto no debe viajar nunca en este modo.
    """
    r = app_con_tive.post("/webhook/dynamic/tive?env=prod", json=PAYLOAD_TELEMETRIA,
                          headers={"x-api-key": SECRETO})
    assert r.status_code == 401


def test_ante_un_fallo_de_base_el_evento_va_a_la_red_de_seguridad(app_con_tive, monkeypatch):
    """
    Tive reintenta solo dos veces y abandona. Antes, un fallo de base devolvía
    500 y el evento se perdía. Ahora queda en disco y se responde 202, porque
    el evento QUEDÓ a salvo.
    """
    from app.api.routers import dynamic_webhook
    from app.core import safety_net

    def _falla(*a, **k):
        raise Exception("(sqlite3.OperationalError) database is locked")

    monkeypatch.setattr(dynamic_webhook, "_save_dynamic_events", _falla)

    cuerpo = json.dumps(PAYLOAD_TELEMETRIA).encode()
    r = app_con_tive.post("/webhook/dynamic/tive?env=prod", content=cuerpo,
                          headers={"content-type": "application/json", **_firmar_tive(cuerpo)})

    assert r.status_code == 200, r.text
    assert r.json()["note"] == "resguardado para reintento"

    import time as _t
    _t.sleep(0.4)
    pendientes = safety_net.pendientes_reales("tive", "prod")
    assert len(pendientes) == 1, "El evento no llegó a la red de seguridad"
    assert pendientes[0]["payload"]["DeviceName"] == "VD0001"


def test_el_reintentador_recupera_un_evento_dinamico(app_con_tive):
    """
    El reintentador es uno solo para todo el hub. Tiene que saber devolver un
    evento dinámico a su mapeador, no solo los de Schmitz.
    """
    import asyncio
    import time as _t

    from app.api.routers.schmitz import _persistir_cualquier_integracion
    from app.core import safety_net

    safety_net.registrar_pendiente("tive", "prod", "iid-prueba", PAYLOAD_TELEMETRIA)
    _t.sleep(0.4)

    asyncio.run(safety_net.reintentar_pendientes("tive", "prod", _persistir_cualquier_integracion))

    assert _filas() == 1
    assert safety_net.estado("tive", "prod", usar_cache=False)["pendientes"] == 0

    # Idempotente: reintentar lo mismo no duplica.
    from app.api.routers.dynamic_webhook import persistir_desde_red_de_seguridad
    asyncio.run(persistir_desde_red_de_seguridad("tive", "prod", [(PAYLOAD_TELEMETRIA, "iid-prueba")]))
    assert _filas() == 1, "El reintento duplicó el evento"
