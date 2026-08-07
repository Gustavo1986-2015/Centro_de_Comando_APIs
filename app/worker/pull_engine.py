import asyncio
import logging
import hashlib
import time
import httpx
import json
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from jsonpath_ng import parse

from app.database import get_session
from app.models.config_models import ProviderConfig, ProviderDictionary
from app.models.db_models import NormalizedRCEvent
from app.core.dynamic_mapper import DynamicMapper
from app.core.crypto import decrypt

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CACHÉ DE TOKENS (P1-4)
# Evita pedir un token nuevo en cada ciclo de PULL. Sin esto, con un intervalo
# de 11s se generan ~7.800 llamadas/día a /api/authorization con la misma cuenta,
# lo que agota el rate limit del proveedor y provoca fallos intermitentes de auth.
# ─────────────────────────────────────────────────────────────────────────────
_TOKEN_CACHE: dict[str, dict] = {}   # {cache_key: {"token": str, "expires_at": float}}
_TOKEN_LOCK = asyncio.Lock()

# Fallback SOLO por si el proveedor no incluye 'expires_in' en la respuesta
# (no debería pasar, pero mejor no reventar el flujo si ocurre).
# El TTL real se toma del campo expires_in de cada respuesta de /api/authorization.
TOKEN_TTL_FALLBACK_SECONDS = 1500

# Margen de seguridad: renovar un poco antes del vencimiento real para evitar
# que un token expire a mitad de una llamada en curso.
TOKEN_SAFETY_MARGIN_SECONDS = 120


class ProviderAuthError(Exception):
    """La autenticación con el proveedor falló. Aborta el ciclo, no encola nada."""
    pass


class ProviderResponseError(Exception):
    """El proveedor devolvió una respuesta de error en lugar de datos."""
    pass


def dynamic_md5(pwd: str) -> tuple[str, str]:
    """
    Genera (unix_time, signature) para autenticación MD5 de doble paso.
    signature = md5(md5(password) + unix_time)

    P0-3: si no hay password, lanza excepción en lugar de devolver firma vacía.
    Una firma vacía hace que el proveedor rechace la auth y el flujo continuaba
    silenciosamente hasta generar eventos basura.
    """
    current_unix_time = str(int(time.time()))
    if not pwd:
        raise ProviderAuthError(
            "Password vacía o no descifrable. No se puede generar la firma MD5. "
            "Verificar que MASTER_ENC_KEY sea la misma con la que se cifraron "
            "las credenciales y que la config del proveedor tenga password."
        )
    pass_md5 = hashlib.md5(pwd.encode()).hexdigest()
    signature = hashlib.md5((pass_md5 + current_unix_time).encode()).hexdigest()
    return current_unix_time, signature


def _is_error_response(data) -> tuple[bool, str]:
    """
    P2-9: Detecta si la respuesta del proveedor es un error en lugar de datos.

    Convención de la mayoría de APIs de tracking (Protrack incluida):
      {"code": 0, "record": [...]}          → éxito
      {"code": 10005, "message": "..."}     → error

    Retorna (es_error, mensaje).
    """
    if not isinstance(data, dict):
        return False, ""

    code = data.get("code")
    # code presente y distinto de 0 (int o str) → error del proveedor
    if code is not None:
        try:
            if int(code) != 0:
                msg = data.get("message") or data.get("msg") or str(data)
                return True, f"code={code} message={msg}"
        except (ValueError, TypeError):
            pass

    # Respuesta con 'message'/'error' pero sin datos útiles
    if ("message" in data or "error" in data) and not any(
        isinstance(v, list) for v in data.values()
    ):
        return True, str(data.get("message") or data.get("error"))

    return False, ""


async def _get_protrack_token(base_url: str, account: str, pwd: str) -> str:
    """
    Obtiene un access_token de Protrack, reutilizando el cacheado si sigue vigente.

    P1-4: caché con TTL para no agotar el rate limit del proveedor.
    P0-2: lanza ProviderAuthError si no se puede obtener el token, en lugar de
          devolver vacío y dejar que el caller haga una llamada rota.
    """
    cache_key = f"{base_url}|{account}"

    async with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached["expires_at"] > time.time():
            return cached["token"]

        # Token vencido o inexistente: pedir uno nuevo
        unix_time, signature = dynamic_md5(pwd)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                auth_resp = await client.get(
                    f"{base_url}/api/authorization",
                    params={"time": unix_time, "account": account, "signature": signature},
                )
                auth_resp.raise_for_status()
                auth_data = auth_resp.json()
        except httpx.HTTPError as e:
            raise ProviderAuthError(
                f"Error de red al solicitar token a {base_url}/api/authorization: {e}"
            ) from e
        except json.JSONDecodeError as e:
            raise ProviderAuthError(
                f"Respuesta de authorization no es JSON válido: {e}"
            ) from e

        if auth_data.get("code") != 0:
            raise ProviderAuthError(
                f"El proveedor rechazó la autenticación. "
                f"URL={base_url}/api/authorization account={account} "
                f"respuesta={auth_data}"
            )

        record = auth_data.get("record") or {}
        token = record.get("access_token")
        if not token:
            raise ProviderAuthError(
                f"Authorization respondió code=0 pero sin access_token: {auth_data}"
            )

        # TTL real informado por el proveedor (Protrack: 7200s = 2h documentadas).
        # Fallback solo si el proveedor omitiera el campo.
        expires_in = record.get("expires_in")
        try:
            expires_in = int(expires_in)
            if expires_in <= 0:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning(
                f"Respuesta de authorization sin 'expires_in' válido, "
                f"usando fallback de {TOKEN_TTL_FALLBACK_SECONDS}s: {auth_data}"
            )
            expires_in = TOKEN_TTL_FALLBACK_SECONDS

        # Renovar un poco antes del vencimiento real (margen de seguridad)
        ttl_efectivo = max(expires_in - TOKEN_SAFETY_MARGIN_SECONDS, 60)

        _TOKEN_CACHE[cache_key] = {
            "token": token,
            "expires_at": time.time() + ttl_efectivo,
        }
        logger.info(
            f"Token de proveedor renovado para {account}. "
            f"Proveedor informó expires_in={expires_in}s, "
            f"se cachea {ttl_efectivo}s (próxima renovación en ~{ttl_efectivo // 60} min)."
        )
        return token


def _invalidate_token(base_url: str, account: str):
    """Fuerza la renovación del token en el próximo ciclo (ej. tras un 401/token expirado)."""
    _TOKEN_CACHE.pop(f"{base_url}|{account}", None)


async def execute_fetch(fetch_config: dict) -> dict | list:
    """
    Ejecuta una petición HTTP saliente según la configuración visual del proveedor.

    P0-2: si la autenticación falla, lanza ProviderAuthError y NO hace la llamada.
          Antes solo logueaba y seguía, generando un request sin token cuya
          respuesta de error terminaba encolada y enviada a RC.
    """
    url = fetch_config.get("url")
    if not url:
        raise ValueError("fetch_config sin 'url'")

    method = fetch_config.get("method", "GET").upper()
    auth_type = fetch_config.get("auth_type", "none")

    headers = {}
    if fetch_config.get("headers"):
        try:
            headers = json.loads(fetch_config.get("headers"))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Headers configurados no son JSON válido, se ignoran: {e}")

    params = {}

    # ── Autenticación ────────────────────────────────────────────────────────
    if auth_type == "md5_dynamic":
        user = fetch_config.get("auth_user", "")
        pwd = fetch_config.get("auth_pass", "")
        unix_time, signature = dynamic_md5(pwd)   # lanza si pwd está vacía
        params["time"] = unix_time
        params["account"] = user
        params["signature"] = signature

    elif auth_type == "bearer":
        token = fetch_config.get("bearer_token", "")
        if not token:
            raise ProviderAuthError("auth_type=bearer pero bearer_token está vacío.")
        headers["Authorization"] = f"Bearer {token}"

    elif auth_type == "protrack":
        # Flujo de doble paso: /api/authorization → access_token → endpoint real
        user = fetch_config.get("auth_user", "")
        pwd = fetch_config.get("auth_pass", "")
        if not user:
            raise ProviderAuthError("auth_type=protrack pero auth_user está vacío.")

        parsed_uri = urlparse(url)
        base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"

        # Puede lanzar ProviderAuthError — se propaga y aborta el ciclo
        params["access_token"] = await _get_protrack_token(base_url, user, pwd)

    # ── Petición ─────────────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=30) as client:
        if method == "GET":
            parsed = urlparse(url)
            query_params = dict(parse_qsl(parsed.query))
            if params:
                query_params.update(params)   # los params generados pisan los literales de la URL
            new_query = urlencode(query_params)
            new_url = urlunparse(
                (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
            )
            resp = await client.get(new_url, headers=headers)
        else:
            body = fetch_config.get("body", "{}")
            try:
                json_body = json.loads(body)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Body configurado no es JSON válido, se envía vacío: {e}")
                json_body = {}
            resp = await client.post(url, params=params, headers=headers, json=json_body)

        resp.raise_for_status()
        data = resp.json()

    # ── Validación de la respuesta del proveedor (P2-9) ──────────────────────
    is_error, err_msg = _is_error_response(data)
    if is_error:
        # Si el error es de token, invalidar caché para renovar en el próximo ciclo
        if auth_type == "protrack" and "access_token" in err_msg.lower():
            parsed_uri = urlparse(url)
            _invalidate_token(
                f"{parsed_uri.scheme}://{parsed_uri.netloc}",
                fetch_config.get("auth_user", ""),
            )
        raise ProviderResponseError(err_msg)

    return data


async def dictionary_sync_loop(provider_name: str, env: str):
    """
    Sincroniza metadatos del proveedor (ej. IMEI → Placa) periódicamente.

    P1-5: si la sincronización falla o devuelve vacío, reintenta en 5 minutos
          en lugar de dormir las N horas configuradas. Antes, un fallo en el
          primer arranque dejaba el diccionario vacío 24h, y sin diccionario
          el PULL no puede inyectar los IDs que el proveedor exige.
    P1-7: usa las credenciales propias del enrichment_config si están cargadas,
          y solo hereda las de fetch_config como fallback.
    """
    logger.info(f"[{provider_name.upper()}-{env}] Iniciando Tarea A: Sincronizador de Diccionario")

    RETRY_ON_FAILURE_SECONDS = 300   # 5 min

    while True:
        sync_ok = False
        frequency_hours = 24

        try:
            db_global = get_session("system_config", "global")
            try:
                config = (
                    db_global.query(ProviderConfig)
                    .filter_by(provider_name=provider_name, env=env)
                    .first()
                )
                is_active = bool(config and config.is_active)
                enrich = (config.enrichment_config or {}) if is_active else {}
                fetch_c = _load_fetch_config(config) if is_active else {}
            finally:
                db_global.close()

            if not is_active:
                await asyncio.sleep(60)
                continue

            if not enrich.get("enabled") or not enrich.get("url"):
                await asyncio.sleep(60)
                continue

            frequency_hours = int(enrich.get("frequency", 24))

            # P1-7: credenciales propias del diccionario, con fallback a las del PULL
            # P1-7: herencia de credenciales del PULL de telemetría.
            #
            # IMPORTANTE: el dashboard guarda "none" (string) cuando el usuario no
            # elige autenticación para el diccionario. Como "none" es truthy en Python,
            # un `enrich.get("auth_type") or fetch_c.get(...)` NUNCA heredaba y el
            # diccionario terminaba llamando con el ACCESS_TOKEN literal de la URL,
            # obteniendo code=10011 access_token error del proveedor.
            # Por eso "none" y vacío se tratan igual: como "no configurado, heredá".
            enrich_auth = (enrich.get("auth_type") or "").strip().lower()
            if enrich_auth in ("", "none"):
                auth_type = fetch_c.get("auth_type", "none")
            else:
                auth_type = enrich_auth

            fetch_cfg = {
                "url": enrich.get("url"),
                "method": enrich.get("method", "GET"),
                "auth_type": auth_type,
                # Credenciales propias del diccionario si están cargadas; si no, las del PULL
                "auth_user": enrich.get("auth_user") or fetch_c.get("auth_user", ""),
                "auth_pass": enrich.get("auth_pass") or fetch_c.get("auth_pass", ""),
                "bearer_token": enrich.get("bearer_token") or fetch_c.get("bearer_token", ""),
            }

            data = await execute_fetch(fetch_cfg)

            key_path = enrich.get("key_path", "")
            val_path = enrich.get("value_path", "")

            if not (key_path and val_path):
                logger.warning(
                    f"[{provider_name.upper()}-{env}] Diccionario sin key_path/value_path configurados."
                )
                await asyncio.sleep(RETRY_ON_FAILURE_SECONDS)
                continue

            key_expr = parse(key_path.replace(".0.", ".[*]."))
            val_expr = parse(val_path.replace(".0.", ".[*]."))

            keys = [m.value for m in key_expr.find(data)]
            vals = [m.value for m in val_expr.find(data)]

            if len(keys) != len(vals) or len(keys) == 0:
                logger.warning(
                    f"[{provider_name.upper()}-{env}] Diccionario: extracción inconsistente "
                    f"(keys={len(keys)}, values={len(vals)}). Reintento en {RETRY_ON_FAILURE_SECONDS}s."
                )
                await asyncio.sleep(RETRY_ON_FAILURE_SECONDS)
                continue

            db_global = get_session("system_config", "global")
            try:
                db_global.query(ProviderDictionary).filter_by(
                    provider_name=provider_name, env=env
                ).delete()
                for i in range(len(keys)):
                    k_str = str(keys[i]).strip()
                    if not k_str:
                        continue
                    v_str = str(vals[i]).strip() or "0"
                    db_global.add(
                        ProviderDictionary(
                            provider_name=provider_name,
                            env=env,
                            dict_key=k_str,
                            dict_value=v_str,
                        )
                    )
                db_global.commit()
                sync_ok = True
                logger.info(
                    f"[{provider_name.upper()}-{env}] Diccionario actualizado: "
                    f"{len(keys)} registros guardados."
                )
            finally:
                db_global.close()

        except ProviderAuthError as e:
            logger.error(
                f"[{provider_name.upper()}-{env}] Diccionario: fallo de autenticación. {e}"
            )
        except ProviderResponseError as e:
            logger.error(
                f"[{provider_name.upper()}-{env}] Diccionario: el proveedor devolvió error. {e}"
            )
        except Exception as e:
            logger.error(
                f"[{provider_name.upper()}-{env}] Diccionario: error inesperado: {e}",
                exc_info=True,
            )

        # P1-5: dormir el intervalo completo solo si la sincronización tuvo éxito
        if sync_ok:
            await asyncio.sleep(frequency_hours * 3600)
        else:
            logger.warning(
                f"[{provider_name.upper()}-{env}] Diccionario no sincronizado. "
                f"Reintento en {RETRY_ON_FAILURE_SECONDS}s."
            )
            await asyncio.sleep(RETRY_ON_FAILURE_SECONDS)


def _load_fetch_config(config: ProviderConfig) -> dict:
    """
    Carga fetch_config descifrando si corresponde.

    P2-8: fallback consistente. Si el descifrado falla (ej. MASTER_ENC_KEY distinta),
    cae a la versión en plano en lugar de devolver {} silenciosamente.
    """
    fetch_cfg_enc = config.fetch_config_enc
    if fetch_cfg_enc:
        decrypted_str = decrypt(fetch_cfg_enc)
        if decrypted_str:
            try:
                return json.loads(decrypted_str)
            except json.JSONDecodeError:
                logger.warning(
                    f"[{config.provider_name}-{config.env}] fetch_config descifrado no es JSON. "
                    "Se usa la versión en plano."
                )
                return config.fetch_config or {}
        else:
            logger.error(
                f"[{config.provider_name}-{config.env}] No se pudo descifrar fetch_config_enc. "
                "Verificar MASTER_ENC_KEY. Se usa la versión en plano como fallback."
            )
            return config.fetch_config or {}
    return config.fetch_config or {}


async def telemetry_poll_loop(provider_name: str, env: str):
    """
    Hace PULL de telemetría al endpoint configurado y encola los datos.

    P1-6: si el proveedor exige IDs (ej. imeis=) y el diccionario está vacío,
          NO hace la llamada. Antes llamaba igual, el proveedor devolvía un error,
          y ese error terminaba encolado y enviado a RC como evento UNKNOWN.
    """
    logger.info(f"[{provider_name.upper()}-{env}] Iniciando Tarea B: Sondeo PULL Telemetría")

    while True:
        interval_sec = 30
        try:
            db_global = get_session("system_config", "global")
            try:
                config = (
                    db_global.query(ProviderConfig)
                    .filter_by(provider_name=provider_name, env=env)
                    .first()
                )
                is_active = bool(config and config.is_active)
                if is_active:
                    fetch_config = _load_fetch_config(config)
                    mapping_schema = config.mapping_schema or {}
                    interval_sec = config.run_interval_sec or 30
                    enable_state_dedup = config.enable_state_dedup
                    requires_ids = bool((config.enrichment_config or {}).get("enabled"))
                else:
                    fetch_config, mapping_schema = {}, {}
                    enable_state_dedup, requires_ids = True, False
            finally:
                db_global.close()

            if not is_active:
                await asyncio.sleep(10)
                continue

            if not fetch_config.get("url"):
                logger.warning(
                    f"[{provider_name.upper()}-{env}] Sin URL de extracción configurada."
                )
                await asyncio.sleep(30)
                continue

            # Leer los IDs del diccionario (ej. IMEIs de Protrack)
            db_global = get_session("system_config", "global")
            try:
                dict_rows = (
                    db_global.query(ProviderDictionary)
                    .filter_by(provider_name=provider_name, env=env)
                    .all()
                )
                ids = [r.dict_key for r in dict_rows]
            finally:
                db_global.close()

            fetch_cfg = dict(fetch_config)
            url_already_has_ids = "imeis=" in fetch_cfg.get("url", "")

            if ids and not url_already_has_ids:
                # Lotes de 100 (límite habitual de estas APIs)
                batches = [ids[i:i + 100] for i in range(0, len(ids), 100)]
                for batch in batches:
                    fc = dict(fetch_cfg)
                    separator = "&" if "?" in fc["url"] else "?"
                    fc["url"] += f"{separator}imeis={','.join(batch)}"
                    data = await execute_fetch(fc)
                    await process_and_enqueue(
                        provider_name, env, data, mapping_schema, enable_state_dedup
                    )

            elif not ids and requires_ids and not url_already_has_ids:
                # P1-6: el diccionario está habilitado pero vacío. No llamar.
                logger.warning(
                    f"[{provider_name.upper()}-{env}] Diccionario vacío y el proveedor "
                    f"requiere IDs. Se omite este ciclo de PULL para no generar eventos "
                    f"inválidos. Esperando que el sincronizador de diccionario complete."
                )
                await asyncio.sleep(interval_sec)
                continue

            else:
                data = await execute_fetch(fetch_cfg)
                await process_and_enqueue(
                    provider_name, env, data, mapping_schema, enable_state_dedup
                )

            await asyncio.sleep(interval_sec)

        except ProviderAuthError as e:
            logger.error(
                f"[{provider_name.upper()}-{env}] PULL abortado por fallo de autenticación: {e}"
            )
            await asyncio.sleep(60)
        except ProviderResponseError as e:
            logger.error(
                f"[{provider_name.upper()}-{env}] PULL abortado: el proveedor devolvió error: {e}"
            )
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(
                f"[{provider_name.upper()}-{env}] Error en Sondeo PULL: {e}", exc_info=True
            )
            await asyncio.sleep(60)


async def process_and_enqueue(
    provider_name: str,
    env: str,
    data: dict | list,
    mapping_schema: dict,
    enable_state_dedup: bool = True,
):
    """
    Mapea la respuesta del proveedor al modelo canónico y encola los eventos.

    P0-1: descarta respuestas que sean errores del proveedor en lugar de tratarlas
          como un evento. Antes, un {"code": 10005, "message": "..."} caía en
          `items = [data]`, se mapeaba a un evento con todo en None/0, y se
          despachaba a RC como telemetría real con chassis UNKNOWN.
    """
    from app.core.auditor import log_raw_payload
    from app.worker.processor import trigger_worker
    from app.core.state_dedup import should_emit_event, get_base_code

    # ── P0-1: nunca encolar una respuesta de error como si fuera telemetría ──
    is_error, err_msg = _is_error_response(data)
    if is_error:
        logger.error(
            f"[{provider_name.upper()}-{env}] Respuesta de error del proveedor descartada, "
            f"NO se encola ni se envía a RC: {err_msg}"
        )
        return

    # ── Extraer la lista de items ────────────────────────────────────────────
    items = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                items = v
                break
        if not items:
            # P0-1: un dict sin ninguna lista adentro NO es un lote de telemetría.
            # Solo se acepta como item único si contiene campos de datos reales.
            if _looks_like_telemetry(data, mapping_schema):
                items = [data]
            else:
                logger.warning(
                    f"[{provider_name.upper()}-{env}] Respuesta sin lista de registros y "
                    f"sin campos de telemetría reconocibles. Se descarta: {str(data)[:200]}"
                )
                return
    elif isinstance(data, list):
        items = data

    if not items:
        return

    # Auditoría cruda en audit/ (JSONL)
    for item in items:
        log_raw_payload(provider_name, env, item)

    db_provider = get_session(provider_name, env)
    base_code = get_base_code(mapping_schema)

    try:
        events_to_add = []
        for item in items:
            try:
                canonical_list = DynamicMapper.map_payload_multi(
                    item, mapping_schema, provider_name, env
                )
                if not canonical_list:
                    continue
            except Exception as e:
                logger.warning(
                    f"[{provider_name.upper()}-{env}] Error mapeando item, se omite: {e}"
                )
                continue

            for canonical in canonical_list:
                if not should_emit_event(
                    provider=provider_name,
                    env=env,
                    chassis=canonical.chassis_number,
                    code=canonical.code,
                    base_code=base_code,
                    mapping_schema=mapping_schema,
                    enabled=enable_state_dedup,
                ):
                    continue

                events_to_add.append(
                    NormalizedRCEvent(
                        provider=provider_name,
                        status="pending",
                        raw_data=json.dumps(item, ensure_ascii=False),
                        chassis_number=canonical.chassis_number,
                        latitude=canonical.latitude,
                        longitude=canonical.longitude,
                        speed=canonical.speed,
                        code=canonical.code,
                        date=canonical.date,
                        altitude=canonical.altitude,
                        battery=canonical.battery,
                        course=canonical.course,
                        humidity=canonical.humidity,
                        ignition=canonical.ignition,
                        odometer=canonical.odometer,
                        temperature=canonical.temperature,
                        serial_number=canonical.serial_number,
                        shipment=canonical.shipment,
                        vehicle_type=canonical.vehicle_type,
                        vehicle_brand=canonical.vehicle_brand,
                        vehicle_model=canonical.vehicle_model,
                    )
                )

        if events_to_add:
            db_provider.add_all(events_to_add)
            db_provider.commit()
            trigger_worker(provider_name, env)
    except Exception as e:
        logger.error(f"[{provider_name.upper()}-{env}] Error encolando PULL: {e}", exc_info=True)
    finally:
        db_provider.close()


def _looks_like_telemetry(data: dict, mapping_schema: dict) -> bool:
    """
    P0-1: Heurística para decidir si un dict suelto es un registro de telemetría
    o una respuesta de control/error del proveedor.

    Se considera telemetría si contiene al menos uno de los campos que el
    mapping_schema espera, o campos de posición típicos.
    """
    if not isinstance(data, dict) or not data:
        return False

    # Campos que el mapeo configurado espera encontrar
    expected_fields = set()
    base = mapping_schema.get("base_mapping", mapping_schema) or {}
    for v in base.values():
        if isinstance(v, str) and v:
            expected_fields.add(v.split(".")[0].lower())

    # Campos de posición habituales en cualquier proveedor GPS
    common_fields = {
        "imei", "latitude", "longitude", "lat", "lng", "lon",
        "gpstime", "devicetime", "speed", "deviceid", "serial", "plate",
    }

    data_keys = {str(k).lower() for k in data.keys()}
    return bool(data_keys & (expected_fields | common_fields))
