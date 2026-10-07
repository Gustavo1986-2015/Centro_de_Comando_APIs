"""
Resolutor del nombre del tracker por la API de Tive (v1.9.4).

EL PROBLEMA

Los tramos de contenedor traen el número de serie del tracker
(Shipment.DeviceId) pero no su nombre, y en altamar el tracker no reporta:
el par serie -> nombre nunca se aprende por el webhook, y la posición del
envío —lo crucial de esta integración— no se podía enviar. La patente que va
a RC es SIEMPRE el nombre del tracker: nunca el contenedor ni la serie.

CÓMO FUNCIONA (diseño de docs/INFORME_v1.9.3_nombre_tracker_api_tive.md)

  1. El módulo (modulo.py), con el interruptor "Resolver nombres con la API
     de Tive" encendido, retiene el tramo sin par con su ingest_id y encola
     la serie. El webhook nunca espera a la API.
  2. Este bucle, cada INTERVALO_SEG, consulta las series encoladas:
       GET {base}/Devices/{serie}   con  x-tive-account-id = AccountId del tramo
     Ante un 400/404 (el equipo no está en esa cuenta): plan B, las demás
     cuentas de "List Accounts" (en caché 24 h).
  3. Con el nombre, guarda el par con origen 'api' y reprocesa los tramos
     retenidos por el mismo camino que la red de seguridad: mismo módulo,
     mismo ingest_id original, ON CONFLICT sobre ingest_id.
  4. Un tramo retenido más de RETENCION_TRAMOS_SEG se descarta con aviso.

FALLAS

  - Red o 5xx: reintento con espera creciente (ESPERAS_SEG).
  - 401: se invalida el token y se reintenta una vez.
  - 429: se respeta Retry-After para esa cuenta.
  - Sin credenciales: aviso (una vez por hora) y no se consulta nada.
  Las credenciales nunca se escriben en logs.

VERIFICACIÓN REAL HECHA POR EL USUARIO: serie 867860087520523 con
x-tive-account-id 10287 devolvió deviceName = J1208355. No se registró si
deviceName vino en la raíz o dentro de "data": la documentación lo muestra en
la raíz (DeviceResponse) y List Devices dentro de "data"; se soportan las dos.
"""
import asyncio
import json
import logging
import time
from urllib.parse import quote

import httpx

from app.core import oauth2
from app.providers.tive import estado, modulo

logger = logging.getLogger(__name__)

PROVEEDOR = "tive"
BASE_URL_POR_DEFECTO = "https://api.tive.com/public/v3"
TOKEN_URL_POR_DEFECTO = "https://api.tive.com/public/v3/authenticate"

INTERVALO_SEG = 60
# Muy por debajo del límite documentado (100 por minuto por cuenta y endpoint).
MAX_CONSULTAS_POR_CICLO = 10
RETENCION_TRAMOS_SEG = 24 * 3600
ESPERAS_SEG = (60, 300, 900, 3600)
ESPERA_NO_ENCONTRADA_SEG = 24 * 3600
CACHE_CUENTAS_SEG = 24 * 3600
TIMEOUT_SEG = 15

_cuentas_cache: dict[str, tuple[float, list[str]]] = {}
_pausas_por_cuenta: dict[tuple[str, str], float] = {}
_ultimo_aviso_sin_credenciales: dict[str, float] = {}


class _Pausa(Exception):
    """La API pidió esperar (429) o rechazó las credenciales: se corta el ciclo."""


# ── Configuración ────────────────────────────────────────────────────────────

def credenciales(config) -> dict | None:
    """
    client_id, client_secret y URLs, descifrados desde la configuración de la
    integración. None si faltan. El client_id es texto libre (puede tener
    espacios): no se valida como código.
    """
    from app.core.crypto import decrypt

    datos = {}
    if getattr(config, "fetch_config_enc", None):
        try:
            datos = json.loads(decrypt(config.fetch_config_enc) or "{}")
        except (ValueError, TypeError):
            datos = {}
    elif isinstance(getattr(config, "fetch_config", None), dict):
        datos = config.fetch_config
    client_id = datos.get("auth_user")
    secreto = datos.get("auth_pass")
    if not client_id or not secreto:
        return None
    return {
        "client_id": client_id,
        "client_secret": secreto,
        "token_url": datos.get("token_url") or TOKEN_URL_POR_DEFECTO,
        "base_url": (datos.get("url") or BASE_URL_POR_DEFECTO).rstrip("/"),
        "formato": (datos.get("token_body_format") or "multipart").lower(),
    }


def _configs_tive() -> list:
    from app.database import get_session
    from app.models.config_models import ProviderConfig

    db = get_session("system_config", "global")
    try:
        return db.query(ProviderConfig).filter(ProviderConfig.provider_name.ilike(PROVEEDOR)).all()
    finally:
        db.close()


# ── Respuesta de la API ──────────────────────────────────────────────────────

def extraer_nombre(cuerpo, serie: str) -> str | None:
    """
    deviceName de la respuesta, en la raíz o dentro de "data" (objeto o
    lista). Si la respuesta trae un deviceId distinto de la serie pedida, no
    se usa: sería el nombre de otro equipo.
    """
    candidatos = []
    if isinstance(cuerpo, dict):
        candidatos.append(cuerpo)
        datos = cuerpo.get("data")
        if isinstance(datos, dict):
            candidatos.append(datos)
        elif isinstance(datos, list):
            candidatos.extend(d for d in datos if isinstance(d, dict))
    for c in candidatos:
        nombre = c.get("deviceName")
        if not nombre or not str(nombre).strip():
            continue
        device_id = c.get("deviceId")
        if device_id is not None and str(device_id).strip() != serie:
            continue
        return str(nombre).strip()
    return None


def _espera(intentos: int) -> int:
    return ESPERAS_SEG[min(intentos, len(ESPERAS_SEG) - 1)]


def _cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SEG)


async def _get(cred: dict, env: str, ruta: str, cuenta: str | None, reintento_401: bool = True):
    """GET autenticado. Maneja 401 (una vez) y 429 (pausa la cuenta)."""
    from app.worker.pull_engine import _verificar_destino_permitido

    url = f"{cred['base_url']}{ruta}"
    _verificar_destino_permitido(url)
    token = await oauth2.get_oauth2_token(cred["token_url"], cred["client_id"],
                                          cred["client_secret"], cred["formato"])
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    if cuenta:
        headers["x-tive-account-id"] = str(cuenta)
    async with _cliente() as cliente:
        resp = await cliente.get(url, headers=headers)
    if resp.status_code == 401:
        oauth2.invalidar_token(cred["token_url"], cred["client_id"])
        if reintento_401:
            return await _get(cred, env, ruta, cuenta, reintento_401=False)
        raise _Pausa("la API de Tive rechazó las credenciales (HTTP 401 dos veces). "
                     "Revisar el client_id y el secreto cargados en el panel")
    if resp.status_code == 429:
        try:
            espera = int(resp.headers.get("Retry-After", "60"))
        except ValueError:
            espera = 60
        _pausas_por_cuenta[(env, str(cuenta))] = time.time() + espera
        raise _Pausa(f"la API de Tive pidió esperar {espera}s (HTTP 429) para la cuenta {cuenta}")
    return resp


async def _cuentas(cred: dict, env: str) -> list[str]:
    """IDs de las cuentas de la organización (List Accounts), en caché 24 h."""
    cache = _cuentas_cache.get(env)
    if cache and cache[0] > time.time():
        return cache[1]
    cuentas, pagina = [], 1
    while True:
        resp = await _get(cred, env, f"/Accounts?PageSize=50&PageNumber={pagina}", None)
        resp.raise_for_status()
        cuerpo = resp.json()
        datos = cuerpo.get("data") if isinstance(cuerpo, dict) else None
        for c in datos or []:
            if isinstance(c, dict) and c.get("accountId") is not None and not c.get("disabled"):
                cuentas.append(str(c["accountId"]))
        if not (isinstance(cuerpo, dict) and cuerpo.get("next")) or not datos or pagina >= 20:
            break
        pagina += 1
    _cuentas_cache[env] = (time.time() + CACHE_CUENTAS_SEG, cuentas)
    return cuentas


async def _buscar_en_cuenta(cred: dict, env: str, serie: str, cuenta: str) -> str | None:
    """Nombre del equipo en esa cuenta, o None si no está ahí (400/404)."""
    if _pausas_por_cuenta.get((env, str(cuenta)), 0) > time.time():
        raise _Pausa(f"cuenta {cuenta} en pausa por un 429 anterior")
    resp = await _get(cred, env, f"/Devices/{quote(serie, safe='')}", cuenta)
    if resp.status_code in (400, 403, 404):
        return None
    resp.raise_for_status()
    nombre = extraer_nombre(resp.json(), serie)
    if nombre is None:
        raise ValueError("respuesta sin deviceName para la serie consultada")
    return nombre


async def resolver_serie(cred: dict, env: str, serie: str, cuenta: str | None,
                         intentos: int) -> str | None:
    """Consulta una serie (cuenta del tramo, después plan B). Registra el resultado."""
    try:
        nombre = await _buscar_en_cuenta(cred, env, serie, cuenta) if cuenta else None
        cuenta_hallada = cuenta
        if nombre is None:
            # Plan B: el equipo pudo haber cambiado de cuenta.
            for otra in await _cuentas(cred, env):
                if otra == str(cuenta):
                    continue
                nombre = await _buscar_en_cuenta(cred, env, serie, otra)
                if nombre:
                    cuenta_hallada = otra
                    break
    except _Pausa:
        estado.marcar_consulta(env, serie, "reintentar", time.time() + _espera(intentos),
                               "pausa", sumar_intento=True)
        raise
    except (httpx.HTTPError, ValueError, oauth2.ProviderAuthError) as e:
        espera = _espera(intentos)
        estado.marcar_consulta(env, serie, "reintentar", time.time() + espera,
                               type(e).__name__, sumar_intento=True)
        logger.warning(f"[TIVE-{env}] No se pudo consultar la serie {serie} en la API de Tive "
                       f"({type(e).__name__}). Se reintenta en {espera}s.")
        return None

    if not nombre:
        estado.marcar_consulta(env, serie, "no_encontrada", time.time() + ESPERA_NO_ENCONTRADA_SEG,
                               "no está en ninguna cuenta accesible", sumar_intento=True)
        logger.warning(f"[TIVE-{env}] La serie {serie} no aparece en la cuenta {cuenta or '-'} ni en "
                       f"las demás cuentas accesibles con estas credenciales. Se reintenta en 24 h.")
        return None

    estado.aprender_par(env, serie, nombre, origen="api")
    estado.marcar_consulta(env, serie, "resuelta", time.time(), None, account_id=cuenta_hallada)
    logger.info(f"[TIVE-{env}] Nombre resuelto por la API de Tive: {serie} -> {nombre} "
                f"(cuenta {cuenta_hallada}).")
    return nombre


# ── Tramos retenidos ─────────────────────────────────────────────────────────

def _vencer_tramos(env: str) -> int:
    from app.core import descartes

    vencidos = 0
    corte = time.time() - RETENCION_TRAMOS_SEG
    for tramo in estado.tramos_pendientes(env):
        if tramo["recibido"] > corte:
            continue
        payload = tramo["payload"]
        envio = payload.get("Shipment") if isinstance(payload.get("Shipment"), dict) else {}
        motivo = "tramo retenido 24 h sin conocer el nombre del tracker"
        detalle = (f"(series={tramo['series']}, contenedor={envio.get('ContainerId') or '-'}, "
                   f"ingest_id={tramo['ingest_id']})")
        logger.warning(f"[TIVE-{env}] Descartado, NO se envía a RC: {motivo} {detalle} "
                       f"| envío={payload.get('ShipmentId') or '-'}")
        descartes.registrar(PROVEEDOR, env, "tive", motivo,
                            equipo=f"serie {tramo['series'][0]}" if tramo["series"] else None,
                            envio=payload.get("ShipmentId"), detalle=detalle)
        estado.quitar_tramo(env, tramo["ingest_id"])
        vencidos += 1
    return vencidos


async def liberar_tramos(env: str) -> int:
    """
    Reprocesa los tramos retenidos cuyo nombre ya se conoce, por el mismo
    camino que la red de seguridad: modulo.procesar con el ingest_id original
    y ON CONFLICT sobre ingest_id. Devuelve cuántas filas quedaron en la base.
    """
    from app.api.routers.dynamic_webhook import persistir_desde_red_de_seguridad

    listos = [t for t in estado.tramos_pendientes(env)
              if modulo.resolver_patentes(env, t["payload"])]
    if not listos:
        return 0
    insertados = await persistir_desde_red_de_seguridad(
        PROVEEDOR, env, [(t["payload"], t["ingest_id"]) for t in listos])
    for t in listos:
        estado.quitar_tramo(env, t["ingest_id"])
        logger.info(f"[TIVE-{env}] Tramo retenido liberado con patente "
                    f"{modulo.resolver_patentes(env, t['payload'])[0][0]} "
                    f"(ingest_id original {t['ingest_id']}).")
    if insertados:
        try:
            from app.worker.processor import trigger_worker
            trigger_worker(PROVEEDOR, env)
        except Exception as e:
            logger.warning(f"[TIVE-{env}] No se pudo despertar al worker: {e}")
    return insertados


# ── Ciclo ────────────────────────────────────────────────────────────────────

async def ciclo(config) -> dict:
    """Un ciclo para una integración tive/<env>. Devuelve un resumen."""
    env = config.env
    resumen = {"env": env, "vencidos": 0, "consultadas": 0, "resueltas": 0, "liberados": 0}
    resumen["vencidos"] = await asyncio.to_thread(_vencer_tramos, env)

    opciones = modulo.opciones_efectivas(getattr(config, "module_options", None))
    if not opciones["resolver_nombres_api"] or not config.is_active:
        return resumen

    pendientes = await asyncio.to_thread(estado.consultas_a_realizar, env, MAX_CONSULTAS_POR_CICLO)
    if pendientes:
        cred = credenciales(config)
        if cred is None:
            if time.time() - _ultimo_aviso_sin_credenciales.get(env, 0) > 3600:
                _ultimo_aviso_sin_credenciales[env] = time.time()
                logger.warning(
                    f"[TIVE-{env}] Hay {len(pendientes)} serie(s) esperando su nombre, pero no hay "
                    f"credenciales de la API de Tive cargadas en el panel. No se consulta nada.")
            return resumen
        for p in pendientes:
            resumen["consultadas"] += 1
            try:
                if await resolver_serie(cred, env, p["serie"], p["account_id"], p["intentos"]):
                    resumen["resueltas"] += 1
            except _Pausa as e:
                logger.warning(f"[TIVE-{env}] Consultas a la API de Tive en pausa: {e}.")
                break

    resumen["liberados"] = await liberar_tramos(env)
    return resumen


async def bucle() -> None:
    """Corre siempre, junto al reintentador de la red de seguridad."""
    while True:
        try:
            for config in await asyncio.to_thread(_configs_tive):
                await ciclo(config)
        except Exception as e:
            logger.error(f"[TIVE] Error en el resolutor de nombres: {type(e).__name__}: {e}")
        await asyncio.sleep(INTERVALO_SEG)
