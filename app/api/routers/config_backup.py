"""
Respaldo de la configuración del hub en un archivo YAML legible.

Para qué existe: si hay que mudar de servidor, rearmar 50 integraciones a mano
—cada mapeo, cada regla de disparo, cada URL, cada intervalo— es inviable. Este
archivo las reconstruye. Y como es texto plano y ordenado, además sirve de mapa:
se lee, se compara contra el del mes pasado y se ve qué cambió.

QUÉ NO LLEVA, a propósito:

  · Credenciales. Ninguna. Ni cifradas.
    Las contraseñas de la base están cifradas con MASTER_ENC_KEY, que es de ESE
    servidor. Exportar esos blobs daría un archivo inútil en el servidor nuevo,
    y peor: el import "funcionaría" y el fallback taparía que quedaron
    ilegibles. Así que no se exportan. Se recargan a mano —son tres o cuatro
    campos por proveedor, no la integración entera— y el archivo queda inerte:
    se guarda donde sea, se commitea, se manda por mail.

  · El diccionario IMEI→patente.
    Lo escribe únicamente la sincronización automática (pull_engine), o sea que
    se regenera solo en el primer ciclo. Exportarlo sería miles de líneas que
    envejecen mal.

  · Estadísticas y eventos. Son datos, no configuración.

El archivo SÍ lleva URLs, usuarios y la estructura de las integraciones. No es
secreto, pero es información operativa del cliente: por eso la descarga exige
contraseña de administrador.
"""
import io
import json
import logging
import os
from datetime import datetime

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from app.core.auditor import log_admin_action
from app.core.auth import verify_dashboard_auth
from app.core import config_cache
from app.core.crypto import decrypt, encrypt
from app.database import get_session
from app.models.config_models import ProviderConfig, SystemSettings
from app.version import __version__

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Respaldo de Configuración"])

# Versión del FORMATO del archivo, no del hub. Sube solo cuando cambia la forma
# del YAML de un modo que un import viejo no sabría leer. El import la compara y
# se niega a procesar un archivo más nuevo que él.
FORMATO_ACTUAL = 1

# Claves que NUNCA salen, estén donde estén. La lista se aplica de forma
# recursiva sobre fetch_config y enrichment_config, que guardan mezclado lo
# estructural (url, método, usuario) con lo secreto. Sellar el bloque entero
# perdería la URL, que es justo lo que hace falta ver en el mapa.
# Campos que se guardan como 0/1 en SQLite y tienen que salir como true/false.
CAMPOS_BOOLEANOS = frozenset({"activo", "modo_simulado", "deduplicacion"})

CLAVES_SECRETAS = frozenset({
    "auth_pass", "password", "pass", "bearer_token", "token",
    "secret", "api_key", "apikey", "x-api-key",
})


def _sin_secretos(valor):
    """Copia una estructura anidada dejando fuera cualquier clave secreta."""
    if isinstance(valor, dict):
        return {
            k: _sin_secretos(v)
            for k, v in valor.items()
            if str(k).strip().lower() not in CLAVES_SECRETAS
        }
    if isinstance(valor, list):
        return [_sin_secretos(v) for v in valor]
    return valor


def _leer_json_config(valor_enc, valor_plano) -> dict:
    """
    Lee un campo de configuración que puede estar cifrado o en plano.

    Replica el criterio de pull_engine._load_fetch_config: si el descifrado
    falla, cae a la versión en plano en lugar de devolver {} en silencio. Acá
    importa el doble, porque un {} silencioso produciría un export incompleto
    que nadie notaría hasta el día de la mudanza.
    """
    if valor_enc:
        try:
            descifrado = decrypt(valor_enc)
            if descifrado:
                return json.loads(descifrado)
        except Exception as e:
            logger.warning(f"No se pudo descifrar una configuración al exportar: {e}")
    if isinstance(valor_plano, dict):
        return valor_plano
    if isinstance(valor_plano, str) and valor_plano.strip():
        try:
            return json.loads(valor_plano)
        except json.JSONDecodeError:
            return {}
    return {}


def _credenciales_faltantes(conf: ProviderConfig, fetch: dict, enrich: dict) -> list[str]:
    """
    Qué hay que tipear a mano después de importar, por proveedor.

    Se calcula mirando qué credenciales EXISTEN hoy, no adivinando: si este
    proveedor no usa API key de webhook, no aparece en la lista. Así el operador
    de la mudanza sabe exactamente qué le falta y no revisa campos que nadie usa.
    """
    faltantes = []
    if conf.rc_password_enc or conf.rc_password:
        faltantes.append("contraseña de Recurso Confiable")
    if conf.webhook_auth_secret_enc:
        faltantes.append(f"API key del webhook (header {conf.webhook_auth_header or 'x-api-key'})")
    if fetch.get("auth_pass"):
        faltantes.append("contraseña del PULL de telemetría")
    if fetch.get("bearer_token"):
        faltantes.append("bearer token del PULL de telemetría")
    if enrich.get("auth_pass"):
        faltantes.append("contraseña del sincronizador de diccionario")
    if enrich.get("bearer_token"):
        faltantes.append("bearer token del sincronizador de diccionario")
    return faltantes


def _proveedor_a_dict(conf: ProviderConfig) -> dict:
    """Un proveedor en la forma que va al YAML: legible y sin credenciales."""
    fetch = _leer_json_config(conf.fetch_config_enc, conf.fetch_config)
    enrich = conf.enrichment_config if isinstance(conf.enrichment_config, dict) else {}
    mapeo = conf.mapping_schema if isinstance(conf.mapping_schema, dict) else {}

    salida = {
        "nombre": conf.provider_name,
        "entorno": conf.env,
    }

    # NUNCA se inventa un valor que la base no tiene. Antes acá había un
    # `conf.provider_type or "pull"`: un proveedor con el tipo sin definir se
    # exportaba como PULL, y al importar ese archivo un proveedor PUSH quedaba
    # convertido en PULL — o sea, dejaba de recibir. Un campo sin valor se
    # OMITE del archivo, y el import saltea lo que no está: prefiere no tocar
    # antes que adivinar.
    opcionales = {
        "tipo": conf.provider_type,
        "activo": conf.is_active,
        "modo_simulado": conf.use_mock,
        "deduplicacion": getattr(conf, "enable_state_dedup", None),
        "intervalo_ejecucion_seg": conf.run_interval_sec,
        "intervalo_purga_min": conf.purge_interval_min,
        "motor_cola": conf.queue_backend,
        "webhook_header": conf.webhook_auth_header,
        "rc_usuario": conf.rc_user,
    }
    for clave, valor in opcionales.items():
        if valor is None:
            continue
        salida[clave] = bool(valor) if clave in CAMPOS_BOOLEANOS else valor

    # Este sí va siempre, incluso en null: vacío significa "sin límite propio,
    # que mande la variable de entorno", y es información, no ausencia de dato.
    salida["limite_push_por_min"] = conf.rate_limit_per_min

    if fetch:
        salida["telemetria"] = _sin_secretos(fetch)
    if enrich:
        salida["diccionario"] = _sin_secretos(enrich)
    if mapeo:
        salida["mapeo"] = _sin_secretos(mapeo)

    faltantes = _credenciales_faltantes(conf, fetch, enrich)
    if faltantes:
        salida["credenciales_a_cargar"] = faltantes

    return salida


class _DumperSeguro(yaml.SafeDumper):
    """
    Dumper que entrecomilla toda cadena que YAML volvería a leer como otro tipo.

    Sin esto el export corrompe datos en silencio. Los `rc_code` son cadenas y
    algunos llevan cero inicial: YAML 1.1 lee `07` como el entero 7 (octal) y
    `010` como 8. Un export→import convertiría la regla de disparo del código
    "07" en el código 7, y la regla dejaría de coincidir sin ningún error.
    Pasa lo mismo con "true", "no", "null", "1.5" y las fechas.

    `09` zafa por casualidad —9 no es un dígito octal— pero no se puede confiar
    en la casualidad para un archivo cuya razón de ser es reconstruir el sistema.
    """


def _representar_str(dumper: yaml.SafeDumper, valor: str):
    # Se le pregunta al propio resolutor de YAML qué tipo le daría a este texto
    # si estuviera sin comillas. Si no es una cadena, se fuerzan las comillas.
    resuelto = dumper.resolve(yaml.ScalarNode, valor, (True, False))
    forzar = resuelto != "tag:yaml.org,2002:str"

    # Y además se entrecomilla todo lo que sean puros dígitos, aunque el
    # resolutor lo dé por texto. Dos razones: que no queden `rc_code: 09` sin
    # comillas al lado de `rc_code: '1'` con comillas, que se lee como un
    # descuido; y que si alguien edita el archivo a mano y escribe 07 en vez de
    # 09, no se le convierta en el entero 7 sin aviso. Los códigos son
    # identificadores: siempre van entre comillas.
    if not forzar and valor.isdigit():
        forzar = True

    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", valor, style="'" if forzar else None
    )


_DumperSeguro.add_representer(str, _representar_str)


def _o_defecto(valor, defecto):
    """
    Valor de la base, o el default del código si la columna quedó en NULL.

    La fila inicial de system_settings la inserta la migración con SQL crudo y
    solo tres columnas; el resto queda en NULL hasta que alguien las toque desde
    el panel. Exportar esos NULL y volver a importarlos pisaría los defaults con
    nada, que es peor que no haberlos exportado.
    """
    return defecto if valor is None else valor


# Columna del modelo -> clave del YAML, con su default. Un solo mapa para las
# dos direcciones: si estuvieran duplicados, exportar e importar podrian
# desincronizarse y un campo viajaria de ida pero no de vuelta.
_CLAVES_SETTINGS = {
    "active_queue_backend": ("motor_cola_activo", "sqlite"),
    "audit_retention_days": ("retencion_crudos_dias", 30),
    "processed_retention_days": ("retencion_procesados_dias", 30),
    "processed_logs_enabled": ("respaldo_procesados_activo", True),
    "retencion_horas_db": ("retencion_base_horas", 2),
    "export_max_days": ("tope_dias_por_descarga", 7),
    "rc_liberacion_tanda": ("rc_liberacion_tanda", 500),
    "rc_max_reintentos": ("rc_max_reintentos", 4),
    "rc_fallos_circuito": ("rc_fallos_circuito", 5),
    "rc_recuperacion_umbral_seg": ("rc_recuperacion_umbral_seg", 600),
}


def _settings_a_dict(settings: SystemSettings | None) -> dict:
    if not settings:
        return {}
    salida = {}
    for columna, (clave, defecto) in _CLAVES_SETTINGS.items():
        valor = _o_defecto(getattr(settings, columna, None), defecto)
        salida[clave] = bool(valor) if isinstance(defecto, bool) else valor
    return salida


# Variables de entorno que PISAN los defaults del código. Sin ellas el mapa está
# incompleto: un servidor nuevo con la misma configuración en base pero distinto
# .env se comporta distinto. Ya pasó con WEBHOOK_RATE_LIMIT_PER_MIN=600, que
# habría hecho fallar la certificación aunque el código dijera 12000.
#
# La lista se mantiene a mano a propósito. Descubrirlas por reflexión sobre
# os.getenv() dejaría afuera las que lee el entorno sin pasar por el código
# —TZ es la más importante, la fija el Dockerfile y decide qué devuelve
# datetime.now()— y ese es justo el tipo de omisión que no se nota hasta la
# mudanza.
VARIABLES_ENTORNO = (
    "APP_ENV",
    "TZ",
    "QUEUE_BACKEND",
    "WEBHOOK_RATE_LIMIT_PER_MIN",
    "WEBHOOK_QUEUE_MAXSIZE",
    "THREAD_POOL_SIZE",
    "PULL_ALLOW_INTERNAL_URLS",
    "INSPECTOR_ALLOW_INSECURE_TLS",
    "LOG_FILE_PATH",
    "RC_ENDPOINT",
    "RC_USERNAME",
    "RC_USE_MOCK",
    "RC_WSDL_TIMEOUT",
    "RC_OPERATION_TIMEOUT",
    "RC_LIBERACION_TANDA",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_DB",
    "DASHBOARD_USER",
)

# De estas sale ÚNICAMENTE el nombre. Nunca el valor, ni truncado, ni con un
# hash: saber cuáles hay que recrear alcanza, y así el archivo sigue siendo
# inerte —guardable, commiteable, enviable por mail—.
VARIABLES_SECRETAS = (
    "MASTER_ENC_KEY",
    "DASHBOARD_PASSWORD",
    "RC_PASSWORD",
    "RC_TOKEN_ENC_KEY",
    "REDIS_PASSWORD",
    "SCHMITZ_API_KEY",
)


def _entorno_a_dict() -> dict:
    """Qué variables están definidas hoy, con su valor salvo las secretas."""
    definidas = {}
    sin_definir = []
    for nombre in VARIABLES_ENTORNO:
        valor = os.environ.get(nombre)
        if valor is None or valor == "":
            sin_definir.append(nombre)
        else:
            definidas[nombre] = valor

    secretas_definidas = [n for n in VARIABLES_SECRETAS if os.environ.get(n)]
    secretas_sin_definir = [n for n in VARIABLES_SECRETAS if not os.environ.get(n)]

    return {
        "definidas": definidas,
        "secretas_definidas": secretas_definidas,
        "secretas_sin_definir": secretas_sin_definir,
        "sin_definir": sin_definir,
    }


def construir_export() -> dict:
    """
    Arma el diccionario completo del respaldo.

    Separada del endpoint para poder verificarla en los tests sin levantar HTTP,
    y para que el import pueda reutilizar la misma forma al comparar.
    """
    db = get_session("system_config", "global")
    try:
        proveedores = (
            db.query(ProviderConfig)
            .order_by(ProviderConfig.provider_name, ProviderConfig.env)
            .all()
        )
        settings = db.query(SystemSettings).first()
        return {
            "formato": FORMATO_ACTUAL,
            "exportado": datetime.now().astimezone().isoformat(timespec="seconds"),
            "hub_version": __version__,
            "incluye_credenciales": False,
            "proveedores": [_proveedor_a_dict(p) for p in proveedores],
            "configuracion_general": _settings_a_dict(settings),
            "entorno": _entorno_a_dict(),
        }
    finally:
        db.close()


ENCABEZADO = """\
# Respaldo de configuración — Hub Telemático Assistcargo
#
# Reconstruye las integraciones en un servidor nuevo: mapeos, reglas de
# disparo, URLs, intervalos y flags.
#
# NO contiene ninguna contraseña. Después de importar hay que cargarlas a mano;
# cada proveedor lista las suyas en 'credenciales_a_cargar'.
#
# El diccionario IMEI→patente tampoco está: lo regenera solo el sincronizador
# automático en el primer ciclo.
"""


def _a_yaml(datos) -> str:
    return yaml.dump(
        datos,
        Dumper=_DumperSeguro,   # entrecomilla lo que YAML releería como otro tipo
        allow_unicode=True,     # las URLs y nombres con acentos se leen, no se escapan
        sort_keys=False,        # el orden de las claves es deliberado
        default_flow_style=False,
        width=100,
    )


def _serializar(datos: dict) -> str:
    """
    Arma el YAML por secciones, con comentarios y aire entre proveedores.

    PyYAML escupe un bloque corrido: con 50 integraciones son miles de líneas
    sin un solo respiro, imposible de recorrer con la vista. Como el archivo
    existe para leerse, se serializa por partes y se intercalan comentarios.
    Todo lo agregado son comentarios y líneas en blanco, así que el resultado
    sigue siendo YAML válido y vuelve a parsear idéntico.
    """
    cabecera = {k: datos[k] for k in
                ("formato", "exportado", "hub_version", "incluye_credenciales")}
    partes = [ENCABEZADO, _a_yaml(cabecera)]

    partes.append(
        "\n# ─── Integraciones ──────────────────────────────────────────────────\n"
        "# Una entrada por proveedor y entorno. 'credenciales_a_cargar' lista lo\n"
        "# que hay que tipear a mano después de importar.\n"
    )
    if datos["proveedores"]:
        partes.append("proveedores:\n")
        # Cada proveedor se serializa solo para poder separarlos con una línea
        # en blanco. yaml.dump sobre la lista entera no permite intercalar nada.
        for i, prov in enumerate(datos["proveedores"]):
            if i:
                partes.append("\n")
            bloque = _a_yaml([prov])
            partes.append(bloque)
    else:
        partes.append("proveedores: []\n")

    partes.append(
        "\n# ─── Configuración general ──────────────────────────────────────────\n"
    )
    partes.append(_a_yaml({"configuracion_general": datos["configuracion_general"]}))

    partes.append(
        "\n# ─── Entorno ────────────────────────────────────────────────────────\n"
        "# Estas variables PISAN los defaults del código: un servidor con la misma\n"
        "# configuración en base pero distinto .env se comporta distinto.\n"
        "# De las secretas sale solo el nombre, nunca el valor.\n"
    )
    partes.append(_a_yaml({"entorno": datos["entorno"]}))

    return "".join(partes)


@router.get("/api/config/export")
def exportar_configuracion(
    request: Request,
    _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """Descarga la configuración completa del hub como YAML, sin credenciales."""
    datos = construir_export()

    log_admin_action(
        "export_configuracion",
        {"proveedores": len(datos["proveedores"])},
        request,
        _auth.username,
    )

    marca = datetime.now().strftime("%Y-%m-%d")
    nombre = f"configuracion_hub_{marca}.yaml"
    return StreamingResponse(
        io.BytesIO(_serializar(datos).encode("utf-8")),
        media_type="application/x-yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# ─── Import: simulación ──────────────────────────────────────────────────────
#
# El import escribe la configuración de los proveedores: es el endpoint más
# peligroso del sistema. Un archivo equivocado puede dejar 50 integraciones
# apuntando a ningún lado, y el hub seguiría "funcionando" sin despachar nada.
# Por eso primero se simula: se ve campo por campo qué se va a pisar, y recién
# después se aplica.

# Campos escalares del YAML y su columna. La identidad de una integración es
# (nombre, entorno), NUNCA el id: los ids son de cada base y en el servidor
# nuevo no coinciden con nada.
CAMPOS_SIMPLES = {
    "tipo": "provider_type",
    "activo": "is_active",
    "modo_simulado": "use_mock",
    "deduplicacion": "enable_state_dedup",
    "intervalo_ejecucion_seg": "run_interval_sec",
    "intervalo_purga_min": "purge_interval_min",
    "motor_cola": "queue_backend",
    "limite_push_por_min": "rate_limit_per_min",
    "webhook_header": "webhook_auth_header",
    "rc_usuario": "rc_user",
}


# Único campo donde null es un valor legítimo y no una ausencia de dato.
CAMPOS_ANULABLES = frozenset({"limite_push_por_min"})


class ImportPayload(BaseModel):
    """
    El YAML llega como texto en el cuerpo, no como archivo subido.

    Así el mismo endpoint sirve para el panel y para un curl, y no hace falta
    python-multipart, que hoy no es dependencia del proyecto.
    """
    contenido: str
    sobrescribir: bool = False


def _parsear_yaml(texto: str) -> dict:
    """Lee el YAML y valida que sea un respaldo de este formato."""
    try:
        datos = yaml.safe_load(texto)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"El archivo no es YAML válido: {e}")

    if not isinstance(datos, dict):
        raise HTTPException(
            status_code=400,
            detail="El archivo no tiene la forma de un respaldo de configuración.",
        )

    formato = datos.get("formato")
    if formato is None:
        raise HTTPException(
            status_code=400,
            detail="Falta el campo 'formato'. ¿Es un respaldo generado por este hub?",
        )
    if not isinstance(formato, int) or formato > FORMATO_ACTUAL:
        # Un archivo más nuevo puede traer campos que esta versión no entiende.
        # Aplicarlo a medias sería peor que no aplicarlo.
        raise HTTPException(
            status_code=400,
            detail=(
                f"El respaldo es de formato {formato} y esta versión del hub entiende "
                f"hasta el {FORMATO_ACTUAL}. Actualizá el hub antes de importar."
            ),
        )

    proveedores = datos.get("proveedores")
    if not isinstance(proveedores, list):
        raise HTTPException(
            status_code=400, detail="El respaldo no tiene una lista de proveedores."
        )

    for i, prov in enumerate(proveedores):
        if not isinstance(prov, dict) or not prov.get("nombre") or not prov.get("entorno"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"El proveedor en la posición {i + 1} no tiene nombre o entorno. "
                    f"Esos dos campos son los que identifican la integración."
                ),
            )

    return datos


def _diferencias_proveedor(deseado: dict, actual: ProviderConfig) -> list[dict]:
    """
    Qué cambiaría en un proveedor que ya existe, campo por campo.

    Se devuelve el detalle y no un "hay cambios" genérico: la simulación existe
    para poder mirar exactamente qué se pisa antes de aceptarlo.
    """
    cambios = []

    for clave, columna in CAMPOS_SIMPLES.items():
        if clave not in deseado:
            continue
        nuevo = deseado[clave]
        if nuevo is None and clave not in CAMPOS_ANULABLES:
            continue
        viejo = getattr(actual, columna, None)
        # Los booleanos llegan como bool del YAML y como 0/1 desde SQLite.
        if isinstance(nuevo, bool) or isinstance(viejo, bool):
            iguales = bool(nuevo) == bool(viejo)
        else:
            iguales = nuevo == viejo
        if not iguales:
            cambios.append({"campo": clave, "actual": viejo, "nuevo": nuevo})

    if "mapeo" in deseado:
        actual_mapeo = actual.mapping_schema if isinstance(actual.mapping_schema, dict) else {}
        if deseado["mapeo"] != actual_mapeo:
            cambios.append({
                "campo": "mapeo",
                "actual": f"{len(actual_mapeo.get('trigger_rules') or [])} regla(s) de disparo",
                "nuevo": f"{len(deseado['mapeo'].get('trigger_rules') or [])} regla(s) de disparo",
            })

    for clave, enc, plano in (
        ("telemetria", "fetch_config_enc", "fetch_config"),
        ("diccionario", None, "enrichment_config"),
    ):
        if clave not in deseado:
            continue
        actual_bloque = _leer_json_config(
            getattr(actual, enc, None) if enc else None, getattr(actual, plano, None)
        )
        # Se comparan ambos lados SIN secretos: el YAML nunca los trae, así que
        # incluirlos daría "cambió" siempre y el operador aprendería a ignorar
        # el aviso, que es la peor forma de que un aviso exista.
        if _sin_secretos(deseado[clave]) != _sin_secretos(actual_bloque):
            cambios.append({
                "campo": clave,
                "actual": actual_bloque.get("url") or "(sin configurar)",
                "nuevo": (deseado[clave] or {}).get("url") or "(sin configurar)",
            })

    return cambios


def _diferencias_generales(deseado: dict, actual: SystemSettings | None) -> list[dict]:
    if not deseado or not actual:
        return []
    vigente = _settings_a_dict(actual)
    return [
        {"campo": k, "actual": vigente.get(k), "nuevo": v}
        for k, v in deseado.items()
        if k in vigente and vigente.get(k) != v
    ]


def _comparar_entorno(entorno: dict) -> dict:
    """
    Qué variables del respaldo faltan o difieren en ESTE servidor.

    No se importan nunca: viven en el .env, fuera del alcance de la app. Pero
    avisar es la mitad del trabajo. Un servidor con la misma configuración en
    base y distinto .env se comporta distinto, y eso ya pasó con
    WEBHOOK_RATE_LIMIT_PER_MIN=600 cuando el código decía 12000.
    """
    definidas = entorno.get("definidas") or {}
    faltantes, distintas = [], []

    for nombre, valor_origen in definidas.items():
        actual = os.environ.get(nombre)
        if not actual:
            faltantes.append({"variable": nombre, "valor_en_el_respaldo": valor_origen})
        elif str(actual) != str(valor_origen):
            distintas.append({
                "variable": nombre,
                "valor_en_el_respaldo": valor_origen,
                "valor_en_este_servidor": actual,
            })

    return {
        "faltantes": faltantes,
        "distintas": distintas,
        "secretas_faltantes": [
            n for n in (entorno.get("secretas_definidas") or []) if not os.environ.get(n)
        ],
        "nota": (
            "Estas variables no se importan: viven en el .env del servidor. "
            "Hay que ajustarlas a mano antes de dar por buena la migración."
        ),
    }


def analizar_import(datos: dict, sobrescribir: bool) -> dict:
    """
    Compara el respaldo contra la base actual SIN escribir nada.

    Es el motor que van a compartir la simulación y el import real: el import
    aplica exactamente lo que esto reporta. Si fueran dos códigos distintos, lo
    que se ve en pantalla sería una promesa y no una garantía.
    """
    db = get_session("system_config", "global")
    try:
        existentes = {(p.provider_name, p.env): p for p in db.query(ProviderConfig).all()}
        settings = db.query(SystemSettings).first()

        nuevos, actualizados, sin_cambios, omitidos = [], [], [], []

        for prov in datos["proveedores"]:
            etiqueta = f"{prov['nombre']}/{prov['entorno']}"
            actual = existentes.get((prov["nombre"], prov["entorno"]))

            if actual is None:
                nuevos.append({
                    "proveedor": etiqueta,
                    "tipo": prov.get("tipo", "pull"),
                    "reglas_de_disparo": len((prov.get("mapeo") or {}).get("trigger_rules") or []),
                    "credenciales_a_cargar": prov.get("credenciales_a_cargar", []),
                })
                continue

            cambios = _diferencias_proveedor(prov, actual)
            if not cambios:
                sin_cambios.append(etiqueta)
            elif sobrescribir:
                actualizados.append({"proveedor": etiqueta, "cambios": cambios})
            else:
                omitidos.append({
                    "proveedor": etiqueta,
                    "cambios": cambios,
                    "motivo": "ya existe y no se pidió sobrescribir",
                })

        return {
            "formato": datos.get("formato"),
            "exportado": datos.get("exportado"),
            "hub_version_origen": datos.get("hub_version"),
            "sobrescribir": sobrescribir,
            "nuevos": nuevos,
            "actualizados": actualizados,
            "sin_cambios": sin_cambios,
            "omitidos": omitidos,
            "configuracion_general": _diferencias_generales(
                datos.get("configuracion_general") or {}, settings
            ),
            "entorno_a_revisar": _comparar_entorno(datos.get("entorno") or {}),
            "resumen": (
                f"{len(nuevos)} a crear · {len(actualizados)} a actualizar · "
                f"{len(sin_cambios)} sin cambios · {len(omitidos)} omitido(s)"
            ),
        }
    finally:
        db.close()


@router.post("/api/config/import/simular")
def simular_import(
    body: ImportPayload,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """Dice qué haría el import, sin escribir absolutamente nada."""
    datos = _parsear_yaml(body.contenido)
    analisis = analizar_import(datos, body.sobrescribir)

    log_admin_action(
        "simular_import_configuracion",
        {"resumen": analisis["resumen"], "sobrescribir": body.sobrescribir},
        request,
        _auth.username,
    )
    return analisis


# ─── Import: aplicación real ─────────────────────────────────────────────────
#
# Cuatro reglas, todas innegociables:
#
#   1. Confirmación escrita. Hay que tipear IMPORTAR. Un clic accidental no
#      puede reescribir la configuración de 50 integraciones.
#   2. Todo o nada. Si el proveedor 30 de 50 falla, se deshace todo. Una
#      configuración a medias es peor que no haber importado: el hub arrancaría
#      "funcionando" y despacharía mal.
#   3. Las credenciales existentes NO se tocan. El YAML no las trae, y "no
#      traer" jamás puede significar "borrar" — sería la peor sorpresa posible.
#      Importar sobre una base con credenciales cargadas las deja intactas.
#   4. Todo queda en el log. Cada proveedor creado o modificado, con sus campos.

CONFIRMACION_REQUERIDA = "IMPORTAR"

# Columnas que guardan credenciales. El import no las escribe NUNCA, ni siquiera
# al crear: un proveedor nuevo nace sin credenciales y se cargan a mano.
COLUMNAS_CREDENCIALES = (
    "rc_password", "rc_password_enc", "webhook_auth_secret_enc",
)

# Claves secretas dentro de los bloques JSON. Al actualizar se conservan las que
# ya estaban: el YAML nunca las trae, así que pisar el bloque entero borraría la
# contraseña del PULL sin que nadie lo pida.
CLAVES_SECRETAS_EN_BLOQUE = ("auth_pass", "bearer_token", "password", "token")


class ImportEjecutar(ImportPayload):
    confirmacion: str = ""


def _fusionar_conservando_secretos(deseado: dict, actual: dict) -> dict:
    """
    Bloque nuevo con los valores del YAML, conservando los secretos actuales.

    El YAML trae url, método, auth_type y auth_user, pero nunca la contraseña.
    Escribir el bloque tal cual borraría la credencial que ya estaba cargada y
    la integración dejaría de autenticar, sin que el operador haya pedido eso.
    """
    fusionado = dict(deseado or {})
    for clave in CLAVES_SECRETAS_EN_BLOQUE:
        if clave in (actual or {}) and actual[clave]:
            fusionado[clave] = actual[clave]
    return fusionado


def _aplicar_proveedor(conf: ProviderConfig, deseado: dict) -> list[str]:
    """Escribe los campos del YAML sobre un ProviderConfig. Devuelve qué tocó."""
    tocados = []

    for clave, columna in CAMPOS_SIMPLES.items():
        if clave not in deseado:
            continue
        valor = deseado[clave]
        # Un null en el archivo no puede pisar un valor real: significa que el
        # servidor de origen tampoco lo tenía. La excepción es el límite push,
        # donde vacío es un dato ("sin límite propio, manda el entorno").
        if valor is None and clave not in CAMPOS_ANULABLES:
            continue
        setattr(conf, columna, valor)
        tocados.append(clave)

    if "mapeo" in deseado:
        conf.mapping_schema = deseado["mapeo"]
        tocados.append("mapeo")

    if "telemetria" in deseado:
        actual = _leer_json_config(conf.fetch_config_enc, conf.fetch_config)
        fusionado = _fusionar_conservando_secretos(deseado["telemetria"], actual)
        # Se guarda cifrado y se borra el plano, igual que hace el panel
        # (admin_config.py:143-144): dos rutas distintas escribiendo el mismo
        # campo de formas distintas sería una fuente de bugs silenciosos.
        conf.fetch_config_enc = encrypt(json.dumps(fusionado))
        conf.fetch_config = None
        tocados.append("telemetria")

    if "diccionario" in deseado:
        actual = conf.enrichment_config if isinstance(conf.enrichment_config, dict) else {}
        conf.enrichment_config = _fusionar_conservando_secretos(deseado["diccionario"], actual)
        tocados.append("diccionario")

    return tocados


@router.post("/api/config/import")
def ejecutar_import(
    body: ImportEjecutar,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """Aplica el respaldo. Exige confirmación escrita y es todo o nada."""
    if body.confirmacion.strip().upper() != CONFIRMACION_REQUERIDA:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Para aplicar el respaldo hay que escribir '{CONFIRMACION_REQUERIDA}' "
                f"en el campo de confirmación."
            ),
        )

    datos = _parsear_yaml(body.contenido)
    # El plan sale del MISMO motor que usa la simulación: lo que el operador vio
    # en pantalla es exactamente lo que se aplica, no una aproximación.
    plan = analizar_import(datos, body.sobrescribir)

    logger.info(
        f"IMPORT DE CONFIGURACIÓN — inicio | usuario={_auth.username} | "
        f"origen=hub {plan.get('hub_version_origen')} exportado {plan.get('exportado')} | "
        f"sobrescribir={body.sobrescribir} | plan: {plan['resumen']}"
    )

    a_crear = {p["proveedor"] for p in plan["nuevos"]}
    a_actualizar = {p["proveedor"] for p in plan["actualizados"]}

    db = get_session("system_config", "global")
    creados, actualizados, generales = [], [], []
    try:
        existentes = {(p.provider_name, p.env): p for p in db.query(ProviderConfig).all()}

        for prov in datos["proveedores"]:
            etiqueta = f"{prov['nombre']}/{prov['entorno']}"

            if etiqueta in a_crear:
                nuevo = ProviderConfig(provider_name=prov["nombre"], env=prov["entorno"])
                tocados = _aplicar_proveedor(nuevo, prov)
                db.add(nuevo)
                creados.append(etiqueta)
                logger.info(
                    f"IMPORT | creado {etiqueta} | campos: {', '.join(tocados)} | "
                    f"credenciales pendientes de carga manual: "
                    f"{', '.join(prov.get('credenciales_a_cargar') or []) or 'ninguna'}"
                )

            elif etiqueta in a_actualizar:
                anterior = existentes[(prov["nombre"], prov["entorno"])]
                tipo_previo = anterior.provider_type
                tocados = _aplicar_proveedor(anterior, prov)
                actualizados.append(etiqueta)
                logger.info(f"IMPORT | actualizado {etiqueta} | campos: {', '.join(tocados)}")

                # Cambiar el tipo cambia CÓMO entra la telemetría: un PUSH que
                # pasa a PULL deja de recibir del webhook y se pone a sondear una
                # URL que puede no existir. No puede pasar como una línea más.
                if "tipo" in tocados and tipo_previo != prov.get("tipo"):
                    logger.warning(
                        f"IMPORT | ATENCIÓN: {etiqueta} cambió de modo de ingesta "
                        f"{tipo_previo} → {prov.get('tipo')}. Verificá que sea lo buscado: "
                        f"un PUSH convertido en PULL deja de recibir por el webhook."
                    )

            else:
                logger.info(f"IMPORT | sin tocar {etiqueta}")

        cambios_generales = plan["configuracion_general"]
        if cambios_generales:
            settings = db.query(SystemSettings).first()
            inverso = {clave: col for col, (clave, _) in _CLAVES_SETTINGS.items()}
            for cambio in cambios_generales:
                columna = inverso.get(cambio["campo"])
                if columna and hasattr(settings, columna):
                    setattr(settings, columna, cambio["nuevo"])
                    generales.append(cambio["campo"])
                    logger.info(
                        f"IMPORT | configuración general: {cambio['campo']} "
                        f"{cambio['actual']} → {cambio['nuevo']}"
                    )

        # Un solo commit al final: si algo revienta a mitad, el rollback deja la
        # base exactamente como estaba y no queda una configuración a medias.
        db.commit()

    except Exception as e:
        db.rollback()
        logger.error(
            f"IMPORT DE CONFIGURACIÓN — FALLÓ y se revirtió TODO. "
            f"La configuración quedó como estaba. Motivo: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"El import falló y se deshizo por completo: la configuración quedó "
                f"como estaba antes. Motivo: {e}"
            ),
        )
    finally:
        db.close()

    config_cache.invalidate()

    credenciales = [
        {"proveedor": p["proveedor"], "faltan": p["credenciales_a_cargar"]}
        for p in plan["nuevos"] if p.get("credenciales_a_cargar")
    ]

    resumen = (
        f"{len(creados)} creado(s) · {len(actualizados)} actualizado(s) · "
        f"{len(generales)} ajuste(s) generales"
    )
    logger.info(f"IMPORT DE CONFIGURACIÓN — terminado | {resumen}")
    if credenciales:
        logger.warning(
            "IMPORT | Hay integraciones SIN credenciales. No van a autenticar hasta "
            "que se carguen a mano desde el panel: "
            + " · ".join(c["proveedor"] for c in credenciales)
        )

    log_admin_action(
        "import_configuracion",
        {"resumen": resumen, "creados": creados, "actualizados": actualizados},
        request,
        _auth.username,
    )

    return {
        "ok": True,
        "resumen": resumen,
        "creados": creados,
        "actualizados": actualizados,
        "configuracion_general": generales,
        "credenciales_a_cargar": credenciales,
        "entorno_a_revisar": plan["entorno_a_revisar"],
    }


# ─── Precedencia: qué manda de verdad, el .env o el panel ────────────────────
#
# Hay parámetros que viven en los dos lados y la precedencia NO es uniforme:
# en unos gana la base y en otro gana el entorno. Eso ya causó una sorpresa cara
# (WEBHOOK_RATE_LIMIT_PER_MIN=600 en el .env mientras el código decía 12000).
#
# La solución no es cambiar quién gana —lo específico tiene que poder pisar a lo
# general, para eso existe el control por proveedor— sino mostrarlo.
#
# REGLA: este endpoint le PREGUNTA al mismo código que usa la aplicación
# (rate_limit._limit, processor.obtener_parametros_rc). No recalcula la
# precedencia por su cuenta. Si la recalculara habría dos fuentes de verdad y el
# panel podría mentir, que es justamente el problema que viene a resolver.


def _origen(valor_vigente, valor_env, valor_base) -> str:
    """De dónde salió el valor que la app está usando ahora mismo."""
    if valor_base is not None and str(valor_vigente) == str(valor_base):
        return "panel"
    if valor_env is not None and str(valor_vigente) == str(valor_env):
        return "entorno (.env)"
    return "default del código"


@router.get("/api/config/precedencia")
def precedencia_de_parametros(_auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    """
    Valor vigente de cada parámetro duplicado y de dónde sale.

    Se consulta a las mismas funciones que usa el hub en producción, no a una
    copia de la lógica.
    """
    from app.core import rate_limit
    from app.worker.processor import obtener_parametros_rc

    db = get_session("system_config", "global")
    try:
        settings = db.query(SystemSettings).first()
        proveedores = db.query(ProviderConfig).all()

        limites = []
        for p in proveedores:
            vigente = rate_limit._limit(p.provider_name)
            limites.append({
                "proveedor": f"{p.provider_name}/{p.env}",
                "vigente": vigente,
                "origen": _origen(
                    vigente,
                    os.environ.get(f"WEBHOOK_RATE_LIMIT_{p.provider_name.upper()}")
                    or os.environ.get("WEBHOOK_RATE_LIMIT_PER_MIN"),
                    p.rate_limit_per_min,
                ),
                "en_panel": p.rate_limit_per_min,
                "en_entorno": (
                    os.environ.get(f"WEBHOOK_RATE_LIMIT_{p.provider_name.upper()}")
                    or os.environ.get("WEBHOOK_RATE_LIMIT_PER_MIN")
                ),
            })

        params = obtener_parametros_rc()
        tanda_vigente = params.get("liberacion_tanda")
        tanda_base = getattr(settings, "rc_liberacion_tanda", None)

        return {
            "limite_push": {
                "descripcion": "Peticiones por minuto aceptadas en el webhook",
                "precedencia": "panel → WEBHOOK_RATE_LIMIT_<PROVEEDOR> → WEBHOOK_RATE_LIMIT_PER_MIN → código",
                "por_proveedor": limites,
            },
            "liberacion_tanda": {
                "descripcion": "Eventos liberados por ciclo tras una caída de RC",
                "precedencia": "panel → RC_LIBERACION_TANDA → código",
                "vigente": tanda_vigente,
                "origen": _origen(
                    tanda_vigente, os.environ.get("RC_LIBERACION_TANDA"), tanda_base
                ),
                "en_panel": tanda_base,
                "en_entorno": os.environ.get("RC_LIBERACION_TANDA"),
            },
            "modo_simulado": {
                "descripcion": "Si se despacha a RC de verdad o se simula",
                # Único caso donde el entorno gana: rc_soap.py:513 evalúa
                # `RC_USE_MOCK or self.use_mock`, así que con la variable en True
                # ningún proveedor puede despachar aunque el panel diga lo contrario.
                "precedencia": "RC_USE_MOCK fuerza simulado en TODOS; si está en False, manda el panel",
                "rc_use_mock": os.environ.get("RC_USE_MOCK", "False"),
                "fuerza_simulado_global": (
                    os.environ.get("RC_USE_MOCK", "False").lower() == "true"
                ),
                "por_proveedor": [
                    {"proveedor": f"{p.provider_name}/{p.env}", "en_panel": bool(p.use_mock)}
                    for p in proveedores
                ],
            },
            "motor_cola": {
                "descripcion": "Backend de la cola de eventos",
                "precedencia": "panel → QUEUE_BACKEND (solo si la base no responde) → sqlite",
                "por_proveedor": [
                    {"proveedor": f"{p.provider_name}/{p.env}", "en_panel": p.queue_backend}
                    for p in proveedores
                ],
                "en_entorno": os.environ.get("QUEUE_BACKEND"),
            },
            "app_env": {
                "descripcion": "Modo de ejecución. Solo del entorno, no se configura en el panel",
                "precedencia": "solo APP_ENV",
                "vigente": os.environ.get("APP_ENV", "development"),
                "origen": "entorno (.env)",
            },
        }
    finally:
        db.close()
