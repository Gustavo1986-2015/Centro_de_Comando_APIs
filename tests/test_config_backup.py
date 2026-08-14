"""
Regresión del respaldo de configuración (export en YAML).

Lo que este archivo protege, y por qué cada cosa:

  · NINGUNA credencial sale en el export. Es la propiedad que hace que el
    archivo sea inerte —guardable, commiteable, enviable por mail—. Si un día
    se filtra una, todo el criterio de diseño se cae.
  · La estructura SÍ sale entera. Un export al que le falta el mapeo o las
    reglas de disparo no sirve para mudarse, que es su única razón de existir.
  · Las URLs y usuarios sobreviven junto a las contraseñas quitadas: sellar el
    bloque entero perdería el mapa.
  · La lista de credenciales a cargar refleja lo que ESE proveedor usa.
"""
import json
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.routers import config_backup


@pytest.fixture
def auth():
    """Credenciales leídas en el momento: otros tests de la suite las cambian."""
    return (os.environ["DASHBOARD_USER"], os.environ["DASHBOARD_PASSWORD"])


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    """
    App con solo este router, sobre una base temporal vacía.

    `database._engines` y `database._sessions` son cachés a nivel de módulo, y
    hay que aislar LOS DOS. Limpiar solo `_engines` no alcanza: `get_session`
    resuelve contra `_sessions`, así que al terminar el archivo quedaba un
    sessionmaker apuntando a la base temporal de este test. El síntoma apareció
    lejos —`test_ingest_capacity` leía el rate_limit del proveedor de prueba de
    acá— y con la suite en verde si se corría el archivo solo.

    También se limpia el caché de límites de rate_limit, que guarda por
    proveedor lo que leyó de la base con un TTL propio.
    """
    from fastapi import FastAPI

    from app import database
    from app.core import rate_limit

    monkeypatch.chdir(tmp_path)
    engines_previos = dict(database._engines)
    sessions_previas = dict(database._sessions)
    database._engines.clear()
    database._sessions.clear()
    rate_limit._db_limit_cache.clear()

    database.check_and_migrate_provider_db("system_config", "global")

    app = FastAPI()
    app.include_router(config_backup.router)
    yield TestClient(app)

    database._engines.clear()
    database._sessions.clear()
    database._engines.update(engines_previos)
    database._sessions.update(sessions_previas)
    rate_limit._db_limit_cache.clear()


@pytest.fixture
def proveedor_completo():
    """
    Un proveedor con TODOS los campos poblados, incluidos todos los secretos.

    Sirve de caso peor: si el export filtra algo, lo filtra acá.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    conf = ProviderConfig(
        provider_name="protrack",
        env="test",
        provider_type="pull",
        is_active=True,
        use_mock=False,
        enable_state_dedup=True,
        run_interval_sec=55,
        purge_interval_min=15,
        queue_backend="sqlite",
        rate_limit_per_min=600,
        webhook_auth_header="x-api-key",
        rc_user="AC_avl_Protrack",
        rc_password_enc="gAAAAABsecretoDeRC==",
        webhook_auth_secret_enc="gAAAAABclaveDeWebhook==",
        fetch_config={
            "url": "http://api.protrack365.com/api/track",
            "method": "GET",
            "auth_type": "basic",
            "auth_user": "prueba.maersk",
            "auth_pass": "ClaveSecretaDelPull",
            "bearer_token": "tok_secreto_pull",
        },
        enrichment_config={
            "enabled": True,
            "url": "http://api.protrack365.com/api/device/list",
            "frequency": 24,
            "key_path": "imei",
            "value_path": "plate",
            "auth_user": "prueba.maersk",
            "auth_pass": "ClaveSecretaDelDiccionario",
        },
        mapping_schema={
            "default_rule": {"rc_code": "1"},
            "base_mapping": {"chassis_number": "imei", "latitude": "latitude"},
            "trigger_rules": [
                {"rc_code": "11", "dedup_key": "accstatus", "event_type": "state",
                 "field": "accstatus", "operator": "==", "value": "1"},
            ],
        },
    )
    db.add(conf)
    db.commit()
    db.close()
    return conf


def _descargar(cliente, auth) -> dict:
    r = cliente.get("/api/config/export", auth=auth)
    assert r.status_code == 200
    return yaml.safe_load(r.text)


# ─── Autenticación ───────────────────────────────────────────────────────────

def test_el_export_exige_autenticacion(cliente):
    """Lleva URLs, usuarios y la estructura entera de las integraciones."""
    assert cliente.get("/api/config/export").status_code == 401


# ─── Lo que NO puede salir ───────────────────────────────────────────────────

SECRETOS_SEMBRADOS = [
    "gAAAAABsecretoDeRC==",
    "gAAAAABclaveDeWebhook==",
    "ClaveSecretaDelPull",
    "tok_secreto_pull",
    "ClaveSecretaDelDiccionario",
]


@pytest.mark.parametrize("secreto", SECRETOS_SEMBRADOS)
def test_ninguna_credencial_aparece_en_el_archivo(cliente, auth, proveedor_completo, secreto):
    """
    Se busca sobre el TEXTO crudo, no sobre el YAML parseado: un secreto podría
    colarse en un comentario, en una clave anidada o en el encabezado, y el
    parseo lo escondería.
    """
    texto = cliente.get("/api/config/export", auth=auth).text
    assert secreto not in texto, f"El export filtró la credencial {secreto!r}"


@pytest.mark.parametrize("clave", ["auth_pass", "bearer_token", "password", "api_key", "secret"])
def test_las_claves_secretas_no_aparecen_ni_vacias(cliente, auth, proveedor_completo, clave):
    """Ni siquiera el NOMBRE del campo: si está, alguien va a intentar llenarlo."""
    texto = cliente.get("/api/config/export", auth=auth).text
    assert f"{clave}:" not in texto


def test_el_export_se_declara_sin_credenciales(cliente, auth, proveedor_completo):
    """Bandera explícita en el archivo, para que el import no tenga que deducirlo."""
    assert _descargar(cliente, auth)["incluye_credenciales"] is False


def test_el_diccionario_imei_patente_no_se_exporta(cliente, auth, proveedor_completo):
    """
    Lo escribe solo el sincronizador automático (pull_engine), así que se
    regenera en el primer ciclo. Exportarlo serían miles de líneas que envejecen.
    """
    from app.database import get_session
    from app.models.config_models import ProviderDictionary

    db = get_session("system_config", "global")
    db.add(ProviderDictionary(provider_name="protrack", env="test",
                              dict_key="864035052734572", dict_value="AB1234"))
    db.commit()
    db.close()

    texto = cliente.get("/api/config/export", auth=auth).text
    assert "864035052734572" not in texto
    assert "AB1234" not in texto


# ─── Lo que SÍ tiene que salir ───────────────────────────────────────────────

def test_las_urls_sobreviven_a_la_quita_de_contrasenas(cliente, auth, proveedor_completo):
    """
    Es el punto del diseño: se abre el bloque y se saca solo el campo secreto.
    Sellarlo entero perdería la URL, que es justo lo que hace falta ver.
    """
    prov = _descargar(cliente, auth)["proveedores"][0]
    assert prov["telemetria"]["url"] == "http://api.protrack365.com/api/track"
    assert prov["telemetria"]["auth_user"] == "prueba.maersk"
    assert prov["telemetria"]["auth_type"] == "basic"
    assert "auth_pass" not in prov["telemetria"]

    assert prov["diccionario"]["url"] == "http://api.protrack365.com/api/device/list"
    assert prov["diccionario"]["key_path"] == "imei"
    assert "auth_pass" not in prov["diccionario"]


def test_el_mapeo_completo_se_exporta(cliente, auth, proveedor_completo):
    """Sin las reglas de disparo, el export no sirve para mudarse."""
    mapeo = _descargar(cliente, auth)["proveedores"][0]["mapeo"]
    assert mapeo["default_rule"]["rc_code"] == "1"
    assert mapeo["base_mapping"]["chassis_number"] == "imei"
    assert len(mapeo["trigger_rules"]) == 1
    assert mapeo["trigger_rules"][0]["dedup_key"] == "accstatus"


def test_los_parametros_operativos_se_exportan(cliente, auth, proveedor_completo):
    prov = _descargar(cliente, auth)["proveedores"][0]
    assert prov["nombre"] == "protrack"
    assert prov["entorno"] == "test"
    assert prov["tipo"] == "pull"
    assert prov["activo"] is True
    assert prov["modo_simulado"] is False
    assert prov["deduplicacion"] is True
    assert prov["intervalo_ejecucion_seg"] == 55
    assert prov["intervalo_purga_min"] == 15
    assert prov["limite_push_por_min"] == 600
    assert prov["rc_usuario"] == "AC_avl_Protrack"


def test_la_configuracion_general_se_exporta(cliente, auth):
    """
    La migración ya inserta una fila de settings al crear la base, así que acá
    se ACTUALIZA esa fila. Agregar una segunda no serviría: el código lee con
    .first() y seguiría viendo la original, igual que en producción.
    """
    from app.database import get_session
    from app.models.config_models import SystemSettings

    db = get_session("system_config", "global")
    settings = db.query(SystemSettings).first()
    assert settings is not None, "La migración debería haber creado la fila inicial"
    settings.audit_retention_days = 14
    settings.processed_retention_days = 21
    settings.retencion_horas_db = 6
    settings.export_max_days = 3
    db.commit()
    db.close()

    general = _descargar(cliente, auth)["configuracion_general"]
    assert general["retencion_crudos_dias"] == 14
    assert general["retencion_procesados_dias"] == 21
    assert general["retencion_base_horas"] == 6
    assert general["tope_dias_por_descarga"] == 3


# ─── La lista de lo que hay que tipear ───────────────────────────────────────

def test_lista_las_credenciales_que_hay_que_recargar(cliente, auth, proveedor_completo):
    """El operador de la mudanza tiene que saber qué le falta sin adivinar."""
    faltantes = " · ".join(_descargar(cliente, auth)["proveedores"][0]["credenciales_a_cargar"])
    assert "Recurso Confiable" in faltantes
    assert "API key del webhook" in faltantes
    assert "PULL de telemetría" in faltantes
    assert "diccionario" in faltantes


def test_no_pide_recargar_lo_que_el_proveedor_no_usa(cliente, auth):
    """Un proveedor sin API key no debe aparecer pidiéndola."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="simple", env="prod", provider_type="push",
                          rc_user="u", rc_password_enc="gAAAAABalgo=="))
    db.commit()
    db.close()

    prov = _descargar(cliente, auth)["proveedores"][0]
    faltantes = prov.get("credenciales_a_cargar", [])
    assert any("Recurso Confiable" in f for f in faltantes)
    assert not any("webhook" in f for f in faltantes)


# ─── Forma del archivo ───────────────────────────────────────────────────────

def test_los_codigos_con_cero_inicial_sobreviven_al_round_trip(cliente, auth):
    """
    El defecto más peligroso que puede tener este archivo: YAML 1.1 lee `07`
    como el entero 7 (octal) y `010` como 8. Un rc_code "07" volvería del import
    convertido en 7 y la regla de disparo dejaría de coincidir, sin ningún error
    visible. `09` zafa de casualidad porque 9 no es dígito octal, pero no se
    puede depender de eso.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    codigos = ["1", "07", "09", "010", "11", "0"]
    db = get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="codigos", env="test",
        mapping_schema={"trigger_rules": [{"rc_code": c} for c in codigos]},
    ))
    db.commit()
    db.close()

    reglas = _descargar(cliente, auth)["proveedores"][0]["mapeo"]["trigger_rules"]
    devueltos = [r["rc_code"] for r in reglas]
    assert devueltos == codigos, f"Los códigos cambiaron al serializar: {devueltos}"
    assert all(isinstance(c, str) for c in devueltos), "Algún código dejó de ser cadena"


@pytest.mark.parametrize("ambiguo", ["true", "false", "no", "yes", "null", "~",
                                     "1.5", "2026-08-14", "0755", "1e3"])
def test_los_valores_ambiguos_vuelven_como_cadena(cliente, auth, ambiguo):
    """
    Un mapeo puede referirse a un campo llamado 'no' o a un valor '2026-08-14'.
    Todos tienen que volver tal cual, no como booleano, fecha ni número.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="ambiguo", env="test",
                          mapping_schema={"base_mapping": {"campo": ambiguo}}))
    db.commit()
    db.close()

    devuelto = _descargar(cliente, auth)["proveedores"][0]["mapeo"]["base_mapping"]["campo"]
    assert devuelto == ambiguo
    assert isinstance(devuelto, str)


def test_los_defaults_se_exportan_en_vez_de_nulos(cliente, auth):
    """
    La migración inserta la fila de settings con SQL crudo y solo tres columnas;
    el resto queda NULL hasta que alguien las toque desde el panel. Exportar esos
    NULL y reimportarlos pisaría los defaults con nada.
    """
    general = _descargar(cliente, auth)["configuracion_general"]
    assert None not in general.values(), f"Hay valores nulos en el export: {general}"
    assert general["retencion_base_horas"] == 2
    assert general["tope_dias_por_descarga"] == 7
    assert general["rc_liberacion_tanda"] == 500
    assert general["rc_max_reintentos"] == 4


def test_el_archivo_es_yaml_valido_y_se_descarga(cliente, auth, proveedor_completo):
    r = cliente.get("/api/config/export", auth=auth)
    assert "attachment" in r.headers["content-disposition"]
    assert ".yaml" in r.headers["content-disposition"]
    assert yaml.safe_load(r.text) is not None


def test_el_archivo_arranca_con_una_explicacion(cliente, auth):
    """
    Quien lo abra dentro de un año tiene que entender qué es y qué le falta sin
    volver a preguntar. El encabezado es comentario YAML: no rompe el parseo.
    """
    texto = cliente.get("/api/config/export", auth=auth).text
    assert texto.startswith("#")
    assert "NO contiene ninguna contraseña" in texto
    assert yaml.safe_load(texto) is not None


def test_declara_version_de_formato_y_de_hub(cliente, auth):
    """El import compara el formato y se niega a leer uno más nuevo que él."""
    from app.version import __version__
    datos = _descargar(cliente, auth)
    assert datos["formato"] == config_backup.FORMATO_ACTUAL
    assert datos["hub_version"] == __version__
    assert datos["exportado"]


def test_los_acentos_se_leen_no_se_escapan(cliente, auth):
    """allow_unicode: un YAML con \\u00e1 por todos lados no es legible."""
    texto = cliente.get("/api/config/export", auth=auth).text
    assert "\\u" not in texto


def test_base_sin_proveedores_exporta_igual(cliente, auth):
    """Servidor recién instalado: no es un error, es un export vacío."""
    datos = _descargar(cliente, auth)
    assert datos["proveedores"] == []


def test_los_proveedores_salen_ordenados(cliente, auth):
    """Orden estable para que dos exports se puedan diffear entre sí."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    for nombre, entorno in [("zeta", "test"), ("alfa", "prod"), ("alfa", "test")]:
        db.add(ProviderConfig(provider_name=nombre, env=entorno))
    db.commit()
    db.close()

    pares = [(p["nombre"], p["entorno"]) for p in _descargar(cliente, auth)["proveedores"]]
    assert pares == [("alfa", "prod"), ("alfa", "test"), ("zeta", "test")]


# ─── Cifrado ─────────────────────────────────────────────────────────────────

def test_lee_fetch_config_cifrado(cliente, auth):
    """
    En producción fetch_config viaja cifrado. Si el export no lo descifra, el
    archivo sale sin la URL del PULL y nadie lo nota hasta el día de la mudanza.
    """
    import json
    from app.core.crypto import encrypt
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(
        provider_name="cifrado", env="prod",
        fetch_config_enc=encrypt(json.dumps({
            "url": "https://api.ejemplo.com/track",
            "auth_user": "usuario_visible",
            "auth_pass": "NoDebeSalir",
        })),
    ))
    db.commit()
    db.close()

    r = cliente.get("/api/config/export", auth=auth)
    prov = yaml.safe_load(r.text)["proveedores"][0]
    assert prov["telemetria"]["url"] == "https://api.ejemplo.com/track"
    assert prov["telemetria"]["auth_user"] == "usuario_visible"
    assert "NoDebeSalir" not in r.text


# ═══════════════════════════════════════════════════════════════════════════
# A2 — Simulación de import
#
# La simulación existe para poder mirar qué se va a pisar ANTES de escribir.
# Lo que protegen estos tests:
#   · Que no escriba nada. Es su única promesa y la más fácil de romper.
#   · Que un round-trip completo dé "sin cambios": si el export y el import no
#     hablan el mismo idioma, la restauración va a diferir y nadie lo va a ver.
#   · Que un archivo inválido frene con un mensaje que diga qué está mal.
#   · Que avise de las variables de entorno, que el import NO puede tocar.
# ═══════════════════════════════════════════════════════════════════════════


def _simular(cliente, auth, texto, sobrescribir=False):
    r = cliente.post(
        "/api/config/import/simular",
        json={"contenido": texto, "sobrescribir": sobrescribir},
        auth=auth,
    )
    return r


def _yaml_de(cliente, auth) -> str:
    return cliente.get("/api/config/export", auth=auth).text


def test_la_simulacion_exige_autenticacion(cliente):
    r = cliente.post("/api/config/import/simular", json={"contenido": "formato: 1"})
    assert r.status_code == 401


def test_la_simulacion_no_escribe_nada(cliente, auth, proveedor_completo):
    """
    Su única promesa. Se verifica sobre un archivo que SÍ traería cambios: si
    solo se probara con uno idéntico, un import que escribe pasaría el test.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    texto = _yaml_de(cliente, auth).replace("intervalo_ejecucion_seg: 55",
                                            "intervalo_ejecucion_seg: 999")
    texto += "\n"  # el YAML sigue siendo válido

    r = _simular(cliente, auth, texto, sobrescribir=True)
    assert r.status_code == 200
    assert r.json()["actualizados"], "El test no probó nada: no detectó cambios"

    db = get_session("system_config", "global")
    conf = db.query(ProviderConfig).filter_by(provider_name="protrack", env="test").first()
    assert conf.run_interval_sec == 55, "La simulación escribió en la base"
    db.close()


def test_reimportar_el_propio_export_no_da_cambios(cliente, auth, proveedor_completo):
    """
    El round-trip: exportar y simular el import sobre la misma base tiene que
    dar "sin cambios". Si diera diferencias, el export y el import estarían
    interpretando distinto los mismos campos, y una restauración real quedaría
    silenciosamente distinta del original.
    """
    datos = _simular(cliente, auth, _yaml_de(cliente, auth), sobrescribir=True).json()
    assert datos["sin_cambios"] == ["protrack/test"]
    assert datos["nuevos"] == []
    assert datos["actualizados"] == []


def test_un_proveedor_que_no_existe_se_reporta_como_nuevo(cliente, auth):
    texto = """
formato: 1
proveedores:
- nombre: nuevo_avl
  entorno: prod
  tipo: push
  mapeo:
    trigger_rules:
    - rc_code: '11'
    - rc_code: '12'
  credenciales_a_cargar:
  - contraseña de Recurso Confiable
"""
    datos = _simular(cliente, auth, texto).json()
    assert len(datos["nuevos"]) == 1
    nuevo = datos["nuevos"][0]
    assert nuevo["proveedor"] == "nuevo_avl/prod"
    assert nuevo["reglas_de_disparo"] == 2
    assert "Recurso Confiable" in nuevo["credenciales_a_cargar"][0]


def test_sin_sobrescribir_los_existentes_se_omiten(cliente, auth, proveedor_completo):
    """Por defecto solo agrega: pisar lo que ya funciona tiene que ser explícito."""
    texto = _yaml_de(cliente, auth).replace("intervalo_ejecucion_seg: 55",
                                            "intervalo_ejecucion_seg: 30")
    datos = _simular(cliente, auth, texto, sobrescribir=False).json()
    assert datos["actualizados"] == []
    assert len(datos["omitidos"]) == 1
    assert "no se pidió sobrescribir" in datos["omitidos"][0]["motivo"]


def test_con_sobrescribir_se_ve_el_cambio_campo_por_campo(cliente, auth, proveedor_completo):
    """Un 'hay cambios' genérico no alcanza para decidir si aceptarlo."""
    texto = _yaml_de(cliente, auth).replace("intervalo_ejecucion_seg: 55",
                                            "intervalo_ejecucion_seg: 30")
    cambios = _simular(cliente, auth, texto, sobrescribir=True).json()["actualizados"][0]["cambios"]
    campo = next(c for c in cambios if c["campo"] == "intervalo_ejecucion_seg")
    assert campo["actual"] == 55
    assert campo["nuevo"] == 30


def test_detecta_cambios_en_el_mapeo(cliente, auth, proveedor_completo):
    """El mapeo es lo más valioso del respaldo: un cambio ahí no puede pasar mudo."""
    texto = _yaml_de(cliente, auth).replace("rc_code: '11'", "rc_code: '99'")
    cambios = _simular(cliente, auth, texto, sobrescribir=True).json()["actualizados"][0]["cambios"]
    assert any(c["campo"] == "mapeo" for c in cambios)


def test_los_booleanos_de_sqlite_no_se_leen_como_cambios(cliente, auth, proveedor_completo):
    """
    SQLite devuelve 0/1 y el YAML trae true/false. Sin normalizar, cada
    simulación reportaría cambios falsos en activo, modo_simulado y
    deduplicacion, y el aviso se volvería ruido que nadie mira.
    """
    datos = _simular(cliente, auth, _yaml_de(cliente, auth), sobrescribir=True).json()
    assert datos["sin_cambios"], f"Cambios falsos detectados: {datos['actualizados']}"


# ─── Archivos inválidos ──────────────────────────────────────────────────────

def test_yaml_roto_se_rechaza_con_mensaje(cliente, auth):
    r = _simular(cliente, auth, "esto: [no cierra\n  - :")
    assert r.status_code == 400
    assert "YAML" in r.json()["detail"]


def test_archivo_sin_formato_se_rechaza(cliente, auth):
    r = _simular(cliente, auth, "proveedores: []")
    assert r.status_code == 400
    assert "formato" in r.json()["detail"]


def test_un_formato_mas_nuevo_se_rechaza(cliente, auth):
    """
    Un respaldo de una versión futura puede traer campos que este hub no
    entiende. Aplicarlo a medias es peor que no aplicarlo.
    """
    r = _simular(cliente, auth, "formato: 99\nproveedores: []")
    assert r.status_code == 400
    assert "99" in r.json()["detail"]


def test_un_proveedor_sin_nombre_se_rechaza_indicando_cual(cliente, auth):
    """(nombre, entorno) es la identidad de la integración: sin eso no hay nada."""
    r = _simular(cliente, auth, "formato: 1\nproveedores:\n- nombre: ok\n  entorno: test\n- entorno: prod")
    assert r.status_code == 400
    assert "posición 2" in r.json()["detail"]


def test_un_texto_cualquiera_se_rechaza(cliente, auth):
    r = _simular(cliente, auth, "hola mundo")
    assert r.status_code == 400


# ─── Variables de entorno ────────────────────────────────────────────────────

def test_avisa_de_las_variables_de_entorno_que_faltan(cliente, auth, monkeypatch):
    """
    El import no puede tocar el .env. Pero un servidor con la misma
    configuración en base y distinto entorno se comporta distinto: ya pasó con
    WEBHOOK_RATE_LIMIT_PER_MIN=600 cuando el código decía 12000.
    """
    monkeypatch.delenv("WEBHOOK_RATE_LIMIT_PER_MIN", raising=False)
    texto = """
formato: 1
proveedores: []
entorno:
  definidas:
    WEBHOOK_RATE_LIMIT_PER_MIN: '12000'
"""
    entorno = _simular(cliente, auth, texto).json()["entorno_a_revisar"]
    assert entorno["faltantes"][0]["variable"] == "WEBHOOK_RATE_LIMIT_PER_MIN"
    assert entorno["faltantes"][0]["valor_en_el_respaldo"] == "12000"


def test_avisa_cuando_una_variable_tiene_otro_valor(cliente, auth, monkeypatch):
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MIN", "600")
    texto = """
formato: 1
proveedores: []
entorno:
  definidas:
    WEBHOOK_RATE_LIMIT_PER_MIN: '12000'
"""
    distintas = _simular(cliente, auth, texto).json()["entorno_a_revisar"]["distintas"]
    assert distintas[0]["valor_en_el_respaldo"] == "12000"
    assert distintas[0]["valor_en_este_servidor"] == "600"


def test_avisa_de_las_llaves_secretas_que_faltan(cliente, auth, monkeypatch):
    """Sin MASTER_ENC_KEY las credenciales que se carguen no se cifran."""
    monkeypatch.delenv("MASTER_ENC_KEY", raising=False)
    texto = "formato: 1\nproveedores: []\nentorno:\n  secretas_definidas:\n  - MASTER_ENC_KEY"
    entorno = _simular(cliente, auth, texto).json()["entorno_a_revisar"]
    assert "MASTER_ENC_KEY" in entorno["secretas_faltantes"]


# ─── Sección entorno del export ──────────────────────────────────────────────

def test_el_export_incluye_el_entorno(cliente, auth, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MIN", "12000")
    entorno = _descargar(cliente, auth)["entorno"]
    assert entorno["definidas"]["APP_ENV"] == "production"
    assert entorno["definidas"]["WEBHOOK_RATE_LIMIT_PER_MIN"] == "12000"


def test_de_las_variables_secretas_sale_solo_el_nombre(cliente, auth, monkeypatch):
    """
    Si el valor de una llave saliera acá, el archivo dejaría de ser inerte y
    todo el criterio de diseño se caería.

    Se usan MASTER_ENC_KEY y RC_PASSWORD y no DASHBOARD_PASSWORD: cambiar la
    contraseña del panel a mitad del test rompe la autenticación del propio
    request, la respuesta pasa a ser un 401 y las aserciones de "el secreto no
    aparece" se cumplirían por la razón equivocada.
    """
    monkeypatch.setenv("MASTER_ENC_KEY", "VALOR_ULTRA_SECRETO_DE_LA_LLAVE")
    monkeypatch.setenv("RC_PASSWORD", "OTRA_CLAVE_SECRETA")

    r = cliente.get("/api/config/export", auth=auth)
    assert r.status_code == 200, "Sin un 200, el resto del test no prueba nada"

    assert "VALOR_ULTRA_SECRETO_DE_LA_LLAVE" not in r.text
    assert "OTRA_CLAVE_SECRETA" not in r.text

    entorno = yaml.safe_load(r.text)["entorno"]
    assert "MASTER_ENC_KEY" in entorno["secretas_definidas"]
    assert "RC_PASSWORD" in entorno["secretas_definidas"]


# ─── Legibilidad ─────────────────────────────────────────────────────────────

def test_los_codigos_numericos_van_todos_entre_comillas(cliente, auth):
    """
    Consistencia y seguridad: '09' sin comillas al lado de '1' con comillas se
    lee como un descuido, y si alguien lo edita a mano y escribe 07, YAML se lo
    convierte en el entero 7 sin avisar.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="codigos", env="test",
                          mapping_schema={"trigger_rules": [{"rc_code": c}
                                                            for c in ["1", "09", "11"]]}))
    db.commit()
    db.close()

    texto = cliente.get("/api/config/export", auth=auth).text
    for codigo in ["1", "09", "11"]:
        assert f"rc_code: '{codigo}'" in texto, f"rc_code {codigo} salió sin comillas"


def test_los_proveedores_van_separados_por_una_linea_en_blanco(cliente, auth):
    """Con 50 integraciones, un bloque corrido no se puede recorrer con la vista."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    for nombre in ("alfa", "beta"):
        db.add(ProviderConfig(provider_name=nombre, env="test"))
    db.commit()
    db.close()

    texto = cliente.get("/api/config/export", auth=auth).text
    assert "\n\n- nombre: beta" in texto


def test_el_archivo_con_secciones_sigue_siendo_yaml_valido(cliente, auth, proveedor_completo):
    """
    Los comentarios y las líneas en blanco se agregan partiendo la
    serialización. Si esa costura estuviera mal, el archivo no volvería a
    parsear — y no habría forma de saberlo hasta el día de la mudanza.
    """
    texto = cliente.get("/api/config/export", auth=auth).text
    datos = yaml.safe_load(texto)
    assert datos["proveedores"][0]["nombre"] == "protrack"
    assert "Integraciones" in texto
    assert "Entorno" in texto


# ═══════════════════════════════════════════════════════════════════════════
# A3 — Import real
#
# Es el endpoint que escribe: un error acá deja 50 integraciones apuntando a
# ningún lado y el hub arranca "funcionando" sin despachar bien. Las cuatro
# reglas que protegen estos tests:
#   1. Confirmación escrita obligatoria.
#   2. Todo o nada: si algo falla, la base queda como estaba.
#   3. Las credenciales existentes no se tocan nunca.
#   4. Aplica exactamente lo que mostró la simulación.
# ═══════════════════════════════════════════════════════════════════════════


def _importar(cliente, auth, texto, sobrescribir=False, confirmacion="IMPORTAR"):
    return cliente.post(
        "/api/config/import",
        json={"contenido": texto, "sobrescribir": sobrescribir,
              "confirmacion": confirmacion},
        auth=auth,
    )


def _leer(nombre, entorno="test"):
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    conf = db.query(ProviderConfig).filter_by(provider_name=nombre, env=entorno).first()
    db.expunge_all()
    db.close()
    return conf


YAML_MINIMO = """
formato: 1
proveedores:
- nombre: nuevo_avl
  entorno: test
  tipo: pull
  activo: true
  intervalo_ejecucion_seg: 42
  rc_usuario: usuario_rc
  telemetria:
    url: https://api.nuevo.com/track
    auth_type: basic
    auth_user: uu
  mapeo:
    base_mapping:
      chassis_number: imei
    trigger_rules:
    - rc_code: '11'
      dedup_key: accstatus
"""


# ─── Regla 1: confirmación escrita ───────────────────────────────────────────

def test_el_import_exige_autenticacion(cliente):
    r = cliente.post("/api/config/import",
                     json={"contenido": YAML_MINIMO, "confirmacion": "IMPORTAR"})
    assert r.status_code == 401


@pytest.mark.parametrize("confirmacion", ["", "importar mal", "SI", "OK", "borrar"])
def test_sin_la_confirmacion_exacta_no_escribe(cliente, auth, confirmacion):
    """Un clic accidental no puede reescribir 50 integraciones."""
    r = _importar(cliente, auth, YAML_MINIMO, confirmacion=confirmacion)
    assert r.status_code == 400
    assert "IMPORTAR" in r.json()["detail"]
    assert _leer("nuevo_avl") is None, "Escribió sin confirmación"


def test_la_confirmacion_acepta_minusculas_y_espacios(cliente, auth):
    """Exigir mayúsculas exactas solo genera reintentos, no seguridad."""
    assert _importar(cliente, auth, YAML_MINIMO, confirmacion=" importar ").status_code == 200


# ─── Aplicación ──────────────────────────────────────────────────────────────

def test_crea_el_proveedor_con_todo_su_mapeo(cliente, auth):
    """Es la razón de ser del import: que no haya que rearmar el mapeo a mano."""
    r = _importar(cliente, auth, YAML_MINIMO)
    assert r.status_code == 200
    assert r.json()["creados"] == ["nuevo_avl/test"]

    conf = _leer("nuevo_avl")
    assert conf.provider_type == "pull"
    assert conf.is_active is True
    assert conf.run_interval_sec == 42
    assert conf.rc_user == "usuario_rc"
    assert conf.mapping_schema["trigger_rules"][0]["rc_code"] == "11"
    assert conf.mapping_schema["trigger_rules"][0]["dedup_key"] == "accstatus"


def test_la_telemetria_queda_cifrada_como_la_guarda_el_panel(cliente, auth):
    """
    Dos rutas escribiendo el mismo campo de formas distintas es una fuente de
    bugs silenciosos: el panel cifra y borra el plano, el import hace igual.
    """
    _importar(cliente, auth, YAML_MINIMO)
    conf = _leer("nuevo_avl")
    assert conf.fetch_config_enc, "La telemetría no quedó cifrada"
    assert conf.fetch_config is None, "Quedó una copia en plano"

    from app.core.crypto import decrypt
    assert json.loads(decrypt(conf.fetch_config_enc))["url"] == "https://api.nuevo.com/track"


def test_un_proveedor_nuevo_nace_sin_credenciales(cliente, auth):
    """El YAML no las trae; el import no las inventa."""
    _importar(cliente, auth, YAML_MINIMO)
    conf = _leer("nuevo_avl")
    assert not conf.rc_password_enc
    assert not conf.webhook_auth_secret_enc


def test_avisa_que_faltan_credenciales_por_cargar(cliente, auth):
    texto = YAML_MINIMO + """  credenciales_a_cargar:
  - contraseña de Recurso Confiable
"""
    faltan = _importar(cliente, auth, texto).json()["credenciales_a_cargar"]
    assert faltan[0]["proveedor"] == "nuevo_avl/test"
    assert "Recurso Confiable" in faltan[0]["faltan"][0]


# ─── Regla 3: no borra credenciales ──────────────────────────────────────────

def test_no_borra_la_contrasena_de_rc_al_actualizar(cliente, auth, proveedor_completo):
    """
    La peor sorpresa posible: importar y que las integraciones dejen de
    autenticar porque el respaldo "no traía" las contraseñas.
    """
    texto = _yaml_de(cliente, auth).replace("intervalo_ejecucion_seg: 55",
                                            "intervalo_ejecucion_seg: 30")
    assert _importar(cliente, auth, texto, sobrescribir=True).status_code == 200

    conf = _leer("protrack")
    assert conf.run_interval_sec == 30, "No aplicó el cambio pedido"
    assert conf.rc_password_enc == "gAAAAABsecretoDeRC==", "Borró la contraseña de RC"
    assert conf.webhook_auth_secret_enc == "gAAAAABclaveDeWebhook==", "Borró la API key"


def test_conserva_la_contrasena_del_pull_dentro_del_bloque(cliente, auth, proveedor_completo):
    """
    El caso fino: el YAML trae url y auth_user de telemetría pero no auth_pass.
    Escribir el bloque tal cual borraría la contraseña que estaba adentro.
    """
    from app.core.crypto import decrypt

    texto = _yaml_de(cliente, auth).replace(
        "url: http://api.protrack365.com/api/track", "url: http://api.nuevo.com/track")
    assert _importar(cliente, auth, texto, sobrescribir=True).status_code == 200

    fetch = json.loads(decrypt(_leer("protrack").fetch_config_enc))
    assert fetch["url"] == "http://api.nuevo.com/track", "No aplicó la URL nueva"
    assert fetch["auth_pass"] == "ClaveSecretaDelPull", "Borró la contraseña del PULL"
    assert fetch["bearer_token"] == "tok_secreto_pull", "Borró el bearer token"


def test_conserva_la_contrasena_del_diccionario(cliente, auth, proveedor_completo):
    texto = _yaml_de(cliente, auth).replace("frequency: 24", "frequency: 12")
    assert _importar(cliente, auth, texto, sobrescribir=True).status_code == 200

    enrich = _leer("protrack").enrichment_config
    assert enrich["frequency"] == 12
    assert enrich["auth_pass"] == "ClaveSecretaDelDiccionario"


# ─── Regla 2: todo o nada ────────────────────────────────────────────────────

def test_si_falla_a_mitad_no_queda_nada_aplicado(cliente, auth, monkeypatch):
    """
    Una configuración a medias es peor que no haber importado: el hub arrancaría
    "funcionando" y despacharía mal. Se simula un fallo en el tercer proveedor.
    """
    from app.api.routers import config_backup as cb

    texto = """
formato: 1
proveedores:
- {nombre: uno, entorno: test, tipo: pull}
- {nombre: dos, entorno: test, tipo: pull}
- {nombre: tres, entorno: test, tipo: pull}
"""
    original = cb._aplicar_proveedor
    llamadas = {"n": 0}

    def falla_en_el_tercero(conf, deseado):
        llamadas["n"] += 1
        if llamadas["n"] == 3:
            raise RuntimeError("falla simulada a mitad del import")
        return original(conf, deseado)

    monkeypatch.setattr(cb, "_aplicar_proveedor", falla_en_el_tercero)

    r = _importar(cliente, auth, texto)
    assert r.status_code == 500
    assert "como estaba" in r.json()["detail"]

    for nombre in ("uno", "dos", "tres"):
        assert _leer(nombre) is None, f"{nombre} quedó escrito pese al rollback"


# ─── Regla 4: aplica lo que mostró la simulación ─────────────────────────────

def test_sin_sobrescribir_no_toca_lo_que_ya_existe(cliente, auth, proveedor_completo):
    texto = _yaml_de(cliente, auth).replace("intervalo_ejecucion_seg: 55",
                                            "intervalo_ejecucion_seg: 30")
    r = _importar(cliente, auth, texto, sobrescribir=False)
    assert r.json()["actualizados"] == []
    assert _leer("protrack").run_interval_sec == 55


def test_lo_que_simula_es_lo_que_aplica(cliente, auth, proveedor_completo):
    """
    La simulación y el import comparten motor. Si fueran dos códigos distintos,
    lo que se ve en pantalla sería una promesa y no una garantía.
    """
    texto = _yaml_de(cliente, auth).replace("intervalo_ejecucion_seg: 55",
                                            "intervalo_ejecucion_seg: 30")
    simulado = _simular(cliente, auth, texto, sobrescribir=True).json()
    aplicado = _importar(cliente, auth, texto, sobrescribir=True).json()
    assert [a["proveedor"] for a in simulado["actualizados"]] == aplicado["actualizados"]
    assert [n["proveedor"] for n in simulado["nuevos"]] == aplicado["creados"]


def test_aplica_la_configuracion_general(cliente, auth):
    from app.database import get_session
    from app.models.config_models import SystemSettings

    texto = """
formato: 1
proveedores: []
configuracion_general:
  retencion_crudos_dias: 15
  retencion_base_horas: 8
"""
    assert _importar(cliente, auth, texto).status_code == 200

    db = get_session("system_config", "global")
    settings = db.query(SystemSettings).first()
    assert settings.audit_retention_days == 15
    assert settings.retencion_horas_db == 8
    db.close()


def test_un_archivo_invalido_se_rechaza_antes_de_escribir(cliente, auth):
    assert _importar(cliente, auth, "formato: 99\nproveedores: []").status_code == 400


def test_la_mudanza_completa_reconstruye_todo(cliente, auth, proveedor_completo):
    """
    El caso real: servidor nuevo, base vacía, se importa el respaldo. Tiene que
    quedar todo salvo las credenciales, que se cargan a mano.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    texto = _yaml_de(cliente, auth)

    db = get_session("system_config", "global")
    db.query(ProviderConfig).delete()
    db.commit()
    db.close()

    r = _importar(cliente, auth, texto)
    assert r.json()["creados"] == ["protrack/test"]

    conf = _leer("protrack")
    assert len(conf.mapping_schema["trigger_rules"]) == 1
    assert conf.mapping_schema["base_mapping"]["chassis_number"] == "imei"
    assert conf.run_interval_sec == 55
    assert conf.enrichment_config["key_path"] == "imei"
    assert not conf.rc_password_enc, "Un servidor nuevo no puede tener credenciales"


def test_el_import_deja_rastro_en_el_log(cliente, auth, caplog):
    """Pediste seguimiento en consola: cada creación y cada cambio se registra."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.api.routers.config_backup"):
        _importar(cliente, auth, YAML_MINIMO)

    texto = caplog.text
    assert "IMPORT DE CONFIGURACIÓN — inicio" in texto
    assert "creado nuevo_avl/test" in texto
    assert "IMPORT DE CONFIGURACIÓN — terminado" in texto


# ═══════════════════════════════════════════════════════════════════════════
# A4 — Precedencia: qué valor manda de verdad
#
# Hay parámetros definibles en el panel Y en el .env, y la precedencia no es
# uniforme. Eso ya costó una sorpresa cara (WEBHOOK_RATE_LIMIT_PER_MIN=600 en el
# .env mientras el código decía 12000). La solución no fue cambiar quién gana
# —lo específico tiene que poder pisar a lo general— sino mostrarlo.
#
# La propiedad crítica que protegen estos tests: el endpoint le PREGUNTA al
# mismo código que usa la app. Si recalculara la precedencia por su cuenta,
# habría dos fuentes de verdad y el panel podría mentir, que es exactamente el
# problema que viene a resolver.
# ═══════════════════════════════════════════════════════════════════════════


def _precedencia(cliente, auth):
    r = cliente.get("/api/config/precedencia", auth=auth)
    assert r.status_code == 200
    return r.json()


def test_la_precedencia_exige_autenticacion(cliente):
    assert cliente.get("/api/config/precedencia").status_code == 401


def test_el_panel_gana_sobre_el_entorno_en_el_limite_push(cliente, auth, monkeypatch):
    """
    Precedencia real: la columna del proveedor pisa a la variable global. Existe
    para poder subirle el techo a un proveedor sin aflojar el de todos.
    """
    from app.core import rate_limit
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MIN", "12000")
    rate_limit._db_limit_cache.clear()

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="conlimite", env="test", rate_limit_per_min=40000))
    db.commit()
    db.close()

    fila = _precedencia(cliente, auth)["limite_push"]["por_proveedor"][0]
    assert fila["vigente"] == 40000
    assert fila["origen"] == "panel"
    assert fila["en_panel"] == 40000
    assert fila["en_entorno"] == "12000"


def test_sin_valor_en_el_panel_manda_el_entorno(cliente, auth, monkeypatch):
    """El caso que causó la sorpresa: el panel vacío y el .env decidiendo."""
    from app.core import rate_limit
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MIN", "600")
    rate_limit._db_limit_cache.clear()

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="sinlimite", env="test", rate_limit_per_min=None))
    db.commit()
    db.close()

    fila = _precedencia(cliente, auth)["limite_push"]["por_proveedor"][0]
    assert fila["vigente"] == 600
    assert fila["origen"] == "entorno (.env)"
    assert fila["en_panel"] is None


def test_el_valor_vigente_es_el_que_devuelve_el_codigo_real(cliente, auth, monkeypatch):
    """
    La propiedad que sostiene todo: el panel muestra lo que devuelve
    rate_limit._limit(), no una reimplementación de la precedencia.
    """
    from app.core import rate_limit
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    monkeypatch.setenv("WEBHOOK_RATE_LIMIT_PER_MIN", "7777")
    rate_limit._db_limit_cache.clear()

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="verificar", env="test"))
    db.commit()
    db.close()

    reportado = _precedencia(cliente, auth)["limite_push"]["por_proveedor"][0]["vigente"]
    assert reportado == rate_limit._limit("verificar")


def test_avisa_que_rc_use_mock_fuerza_simulado_en_todos(cliente, auth, monkeypatch):
    """
    Único parámetro donde el entorno gana: rc_soap.py evalúa
    `RC_USE_MOCK or self.use_mock`, así que con la variable en True ningún
    proveedor despacha aunque el panel diga lo contrario. Sin este aviso,
    alguien podría creer que está enviando a RC y no estar enviando nada.
    """
    monkeypatch.setenv("RC_USE_MOCK", "True")
    simulado = _precedencia(cliente, auth)["modo_simulado"]
    assert simulado["fuerza_simulado_global"] is True
    assert "TODOS" in simulado["precedencia"]


def test_con_rc_use_mock_apagado_manda_el_panel(cliente, auth, monkeypatch):
    monkeypatch.setenv("RC_USE_MOCK", "False")
    assert _precedencia(cliente, auth)["modo_simulado"]["fuerza_simulado_global"] is False


def test_informa_la_precedencia_de_cada_parametro_en_texto(cliente, auth):
    """El panel tiene que poder explicar el orden, no solo mostrar el resultado."""
    d = _precedencia(cliente, auth)
    for clave in ("limite_push", "liberacion_tanda", "modo_simulado", "motor_cola", "app_env"):
        assert d[clave]["precedencia"], f"Falta explicar la precedencia de {clave}"
        assert d[clave]["descripcion"]


def test_app_env_se_reporta_como_solo_del_entorno(cliente, auth, monkeypatch):
    """No se configura en el panel: mostrarlo como configurable sería mentir."""
    monkeypatch.setenv("APP_ENV", "production")
    app_env = _precedencia(cliente, auth)["app_env"]
    assert app_env["vigente"] == "production"
    assert app_env["origen"] == "entorno (.env)"


# ═══════════════════════════════════════════════════════════════════════════
# El export no inventa valores
#
# Bug real encontrado en producción local: el export hacía
# `conf.provider_type or "pull"`. Un proveedor con el tipo sin definir salía
# como PULL, y al importar ese archivo un proveedor PUSH quedaba convertido en
# PULL — o sea, dejaba de recibir por el webhook. La simulación lo mostró antes
# de aplicarlo, pero el archivo nunca debió traer ese dato.
# ═══════════════════════════════════════════════════════════════════════════


def _forzar_null(nombre, columna):
    """
    Deja una columna en NULL por SQL crudo.

    El ORM no lo permite: el modelo declara `default="pull"` y SQLAlchemy lo
    completa al insertar. El NULL real aparece en filas creadas ANTES de que la
    columna existiera, y ese es el caso que hay que reproducir.
    """
    from app.database import get_session
    from sqlalchemy import text

    db = get_session("system_config", "global")
    db.execute(text(f"UPDATE provider_config SET {columna} = NULL "
                    f"WHERE provider_name = :n"), {"n": nombre})
    db.commit()
    db.close()


def test_un_tipo_sin_definir_no_se_exporta_como_pull(cliente, auth):
    """El caso exacto del bug: no adivinar el modo de ingesta."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="sintipo", env="test"))
    db.commit()
    db.close()
    _forzar_null("sintipo", "provider_type")

    prov = _descargar(cliente, auth)["proveedores"][0]
    assert "tipo" not in prov, f"El export inventó un tipo: {prov.get('tipo')}"


def test_importar_un_archivo_sin_tipo_no_convierte_un_push_en_pull(cliente, auth):
    """
    La consecuencia del bug: un PUSH que pasa a PULL deja de recibir del webhook
    y se pone a sondear una URL que no existe. Silencioso y grave.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="schmitz", env="test", provider_type="push",
                          enable_state_dedup=False))
    db.commit()
    db.close()

    texto = """
formato: 1
proveedores:
- nombre: schmitz
  entorno: test
  intervalo_ejecucion_seg: 9
"""
    assert _importar(cliente, auth, texto, sobrescribir=True).status_code == 200

    conf = _leer("schmitz")
    assert conf.provider_type == "push", "Convirtió el PUSH en otra cosa"
    assert conf.run_interval_sec == 9, "No aplicó lo que sí venía en el archivo"


def test_un_null_en_el_archivo_no_pisa_un_valor_real(cliente, auth):
    """
    Un null significa que el servidor de origen tampoco lo tenía, no que haya
    que borrarlo acá. Escribirlo dejaría la integración peor que antes.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="conservar", env="test", provider_type="push",
                          run_interval_sec=55, queue_backend="sqlite"))
    db.commit()
    db.close()

    texto = """
formato: 1
proveedores:
- nombre: conservar
  entorno: test
  tipo: null
  intervalo_ejecucion_seg: null
  motor_cola: null
"""
    assert _importar(cliente, auth, texto, sobrescribir=True).status_code == 200

    conf = _leer("conservar")
    assert conf.provider_type == "push"
    assert conf.run_interval_sec == 55
    assert conf.queue_backend == "sqlite"


def test_el_limite_push_vacio_si_es_un_dato_y_viaja(cliente, auth):
    """
    Única excepción: vacío significa "sin límite propio, que mande el .env".
    Es información, no ausencia de dato, así que sale explícito en el archivo.
    """
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="sinlimite", env="test", rate_limit_per_min=None))
    db.commit()
    db.close()

    prov = _descargar(cliente, auth)["proveedores"][0]
    assert "limite_push_por_min" in prov
    assert prov["limite_push_por_min"] is None


def test_una_deduplicacion_sin_definir_no_se_exporta_como_apagada(cliente, auth):
    """bool(None) da False: exportarlo así apagaría el dedup al restaurar."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="sindedup", env="test"))
    db.commit()
    db.close()
    _forzar_null("sindedup", "enable_state_dedup")

    assert "deduplicacion" not in _descargar(cliente, auth)["proveedores"][0]


def test_los_campos_definidos_si_se_exportan(cliente, auth):
    """Omitir lo indefinido no puede convertirse en omitir lo definido."""
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="completo", env="test", provider_type="push",
                          is_active=True, use_mock=False, enable_state_dedup=False,
                          run_interval_sec=5, queue_backend="sqlite"))
    db.commit()
    db.close()

    prov = _descargar(cliente, auth)["proveedores"][0]
    assert prov["tipo"] == "push"
    assert prov["activo"] is True
    assert prov["modo_simulado"] is False
    assert prov["deduplicacion"] is False, "Un false explícito tiene que viajar"
    assert prov["intervalo_ejecucion_seg"] == 5


def test_avisa_en_el_log_cuando_cambia_el_modo_de_ingesta(cliente, auth, caplog):
    """Cambiar el tipo cambia cómo entra la telemetría: no puede pasar mudo."""
    import logging

    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    db.add(ProviderConfig(provider_name="cambiante", env="test", provider_type="push"))
    db.commit()
    db.close()

    texto = "formato: 1\nproveedores:\n- {nombre: cambiante, entorno: test, tipo: pull}"
    with caplog.at_level(logging.WARNING, logger="app.api.routers.config_backup"):
        _importar(cliente, auth, texto, sobrescribir=True)

    assert "modo de ingesta" in caplog.text
    assert "push → pull" in caplog.text
