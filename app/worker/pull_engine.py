import asyncio
import logging
import httpx
import json
from datetime import datetime, timezone
from jsonpath_ng import parse

from app.database import get_session
from app.models.config_models import ProviderConfig, ProviderDictionary
from app.models.db_models import NormalizedRCEvent
from app.core.dynamic_mapper import DynamicMapper

def dynamic_md5(pwd: str) -> tuple[str, str]:
    import time
    import hashlib
    current_unix_time = str(int(time.time()))
    if not pwd:
        return current_unix_time, ""
    pass_md5 = hashlib.md5(pwd.encode()).hexdigest()
    signature = hashlib.md5((pass_md5 + current_unix_time).encode()).hexdigest()
    return current_unix_time, signature

from app.core.dynamic_mapper import DynamicMapper

LAST_SEEN_TELEMETRY = {}

logger = logging.getLogger(__name__)

async def execute_fetch(fetch_config: dict) -> dict | list:
    """Ejecuta una petición HTTP basada en la configuración visual."""
    url = fetch_config.get("url")
    method = fetch_config.get("method", "GET").upper()
    auth_type = fetch_config.get("auth_type", "none")
    
    headers = {}
    if fetch_config.get("headers"):
        try:
            headers = json.loads(fetch_config.get("headers"))
        except Exception:
            pass
            
    params = {}
    
    # Procesar autenticación dinámica
    if auth_type == "md5_dynamic":
        user = fetch_config.get("auth_user", "")
        pwd = fetch_config.get("auth_pass", "")
        unix_time, signature = dynamic_md5(pwd)
        params["time"] = unix_time
        params["account"] = user
        params["signature"] = signature
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {fetch_config.get('bearer_token', '')}"
    elif auth_type == "protrack":
        # Flujo doble de Protrack
        user = fetch_config.get("auth_user", "")
        pwd = fetch_config.get("auth_pass", "")
        unix_time, signature = dynamic_md5(pwd)
        
        # 1. Obtener Token
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Extraemos la base url asumiendo que el usuario puso algo como http://api.protrack365.com/api/device/list
                from urllib.parse import urlparse
                parsed_uri = urlparse(url)
                base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
                
                auth_resp = await client.get(f"{base_url}/api/authorization", params={
                    "time": unix_time,
                    "account": user,
                    "signature": signature
                })
                auth_resp.raise_for_status()
                auth_data = auth_resp.json()
                if auth_data.get("code") == 0:
                    token = auth_data["record"]["access_token"]
                    params["access_token"] = token
                else:
                    logger.error(f"Fallo auth Protrack. URL: {base_url}/api/authorization | Params: time={unix_time}, account={user}, signature={signature} | Respuesta: {auth_data}")
        except Exception as e:
            logger.error(f"Error obteniendo token Protrack: {e}")
        
        
    async with httpx.AsyncClient(timeout=30) as client:
        if method == "GET":
            resp = await client.get(url, params=params, headers=headers)
        else:
            body = fetch_config.get("body", "{}")
            try:
                json_body = json.loads(body)
            except:
                json_body = {}
            resp = await client.post(url, params=params, headers=headers, json=json_body)
            
        resp.raise_for_status()
        return resp.json()

async def dictionary_sync_loop(provider_name: str, env: str):
    """
    Sincroniza metadatos (ej. IMEI -> Placa) una vez cada N horas.
    """
    logger.info(f"[{provider_name.upper()}-{env}] Iniciando Tarea A: Sincronizador de Diccionario")
    while True:
        try:
            db_global = get_session("system_config", "global")
            config = db_global.query(ProviderConfig).filter_by(provider_name=provider_name, env=env).first()
            if not config or not config.is_active:
                db_global.close()
                await asyncio.sleep(60)
                continue
                
            enrich = config.enrichment_config or {}
            fetch_c = config.fetch_config or {}
            db_global.close()
            
            if not enrich.get("enabled") or not enrich.get("url"):
                await asyncio.sleep(60)
                continue
                
            frequency_hours = int(enrich.get("frequency", 24))
            
            # Ejecutar Fetch (Heredamos la config de Auth de fetch_config si está vacía en enrich)
            fetch_cfg = {
                "url": enrich.get("url"),
                "method": enrich.get("method", "GET"),
                "auth_type": fetch_c.get("auth_type", "none"),
                "auth_user": fetch_c.get("auth_user", ""),
                "auth_pass": fetch_c.get("auth_pass", ""),
            }
            
            data = await execute_fetch(fetch_cfg)
            
            # Extraer Key -> Value usando JSONPath
            key_path = enrich.get("key_path", "")
            val_path = enrich.get("value_path", "")
            
            if key_path and val_path:
                key_expr = parse(key_path.replace('.0.', '.[*].')) 
                val_expr = parse(val_path.replace('.0.', '.[*].'))
                
                keys = [match.value for match in key_expr.find(data)]
                vals = [match.value for match in val_expr.find(data)]
                
                if len(keys) == len(vals) and len(keys) > 0:
                    db_global = get_session("system_config", "global")
                    try:
                        db_global.query(ProviderDictionary).filter_by(provider_name=provider_name, env=env).delete()
                        for i in range(len(keys)):
                            k_str = str(keys[i]).strip()
                            v_str = str(vals[i]).strip()
                            if k_str:
                                if not v_str:
                                    v_str = "0"
                                db_global.add(ProviderDictionary(
                                    provider_name=provider_name,
                                    env=env,
                                    dict_key=k_str,
                                    dict_value=v_str
                                ))
                        db_global.commit()
                        logger.info(f"[{provider_name.upper()}-{env}] Diccionario actualizado: {len(keys)} registros guardados.")
                    finally:
                        db_global.close()

            await asyncio.sleep(frequency_hours * 3600)
            
        except Exception as e:
            logger.error(f"[{provider_name.upper()}-{env}] Error en Sincronización Diccionario: {e}")
            await asyncio.sleep(300)

async def telemetry_poll_loop(provider_name: str, env: str):
    """
    Hace PULL de telemetría a un endpoint de lista/lote y encola los datos.
    """
    logger.info(f"[{provider_name.upper()}-{env}] Iniciando Tarea B: Sondeo PULL Telemetría")
    while True:
        try:
            db_global = get_session("system_config", "global")
            config = db_global.query(ProviderConfig).filter_by(provider_name=provider_name, env=env).first()
            if not config or not config.is_active:
                db_global.close()
                await asyncio.sleep(10)
                continue
                
            fetch_config = config.fetch_config or {}
            mapping_schema = config.mapping_schema or {}
            interval_sec = config.run_interval_sec or 30
            db_global.close()
            
            if not fetch_config.get("url"):
                await asyncio.sleep(30)
                continue
                
            db_global = get_session("system_config", "global")
            imeis = []
            try:
                dict_rows = db_global.query(ProviderDictionary).filter_by(provider_name=provider_name, env=env).all()
                imeis = [r.dict_key for r in dict_rows]
            finally:
                db_global.close()
            
            fetch_cfg = dict(fetch_config)
            # Adaptador universal para inyectar IDs (ej. Protrack imeis=)
            if imeis and ("imeis=" not in fetch_cfg.get("url", "")):
                batches = [imeis[i:i + 100] for i in range(0, len(imeis), 100)]
                for batch in batches:
                    fc = dict(fetch_cfg)
                    separator = "&" if "?" in fc["url"] else "?"
                    fc["url"] += f"{separator}imeis={','.join(batch)}"
                        
                    data = await execute_fetch(fc)
                    await process_and_enqueue(provider_name, env, data, mapping_schema)
            else:
                data = await execute_fetch(fetch_cfg)
                await process_and_enqueue(provider_name, env, data, mapping_schema)

            await asyncio.sleep(interval_sec)
            
        except Exception as e:
            logger.error(f"[{provider_name.upper()}-{env}] Error en Sondeo PULL: {e}")
            await asyncio.sleep(60)

async def process_and_enqueue(provider_name: str, env: str, data: dict|list, mapping_schema: dict):
    items = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                items = v
                break
        if not items:
            items = [data]
    elif isinstance(data, list):
        items = data

    if not items:
        return

    db_provider = get_session(provider_name, env)
    from app.worker.processor import trigger_worker
    try:
        events_to_add = []
        for item in items:
            wrapped_item = item 
            
            try:
                canonical_list = DynamicMapper.map_payload_multi(wrapped_item, mapping_schema, provider_name, env)
                
                if not canonical_list:
                    continue
                
                # Deduplicación basada en el primer evento (posición base)
                base_canonical = canonical_list[0]
                cache_key = f"{provider_name}_{env}_{base_canonical.chassis_number}"
                date_str = base_canonical.date
                if date_str and LAST_SEEN_TELEMETRY.get(cache_key) == date_str:
                    continue
                LAST_SEEN_TELEMETRY[cache_key] = date_str

            except Exception:
                continue

            # Agregar todos los eventos generados por este item
            for canonical in canonical_list:
                events_to_add.append(NormalizedRCEvent(
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
                ))
            
        if events_to_add:
            db_provider.add_all(events_to_add)
            db_provider.commit()
            trigger_worker(provider_name, env)
    except Exception as e:
        logger.error(f"Error encolando PULL: {e}")
    finally:
        db_provider.close()
