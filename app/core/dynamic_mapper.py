import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import dateutil.parser

from app.schemas.canonical import RCCanonicalModel

logger = logging.getLogger("DynamicMapper")

_VALORES_VERDADEROS = {"true", "1", "on", "yes", "y", "si", "sí", "t"}
_VALORES_FALSOS = {"false", "0", "off", "no", "n", "f", ""}


def _a_booleano(valor):
    """
    Interpreta el estado de un sensor booleano tolerando las formas en que los
    proveedores lo reportan: booleano nativo, número, o texto.

    bool() no sirve acá: bool("false") y bool("0") son True, así que un
    proveedor que reporte la ignición como cadena mandaría a RC el estado
    invertido. Es dato incorrecto, no ausente, que es peor.
    """
    if valor is None:
        return None
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return valor != 0

    texto = str(valor).strip().lower()
    if texto in _VALORES_VERDADEROS:
        return True
    if texto in _VALORES_FALSOS:
        return False

    # Valor no reconocido: se informa y se trata como no disponible en lugar de
    # inventar un estado que el operador podría leer como real.
    logger.warning(f"Valor booleano no reconocido en telemetría: {valor!r}. Se envía como nulo.")
    return None


class DynamicMapper:
    """
    Clase utilitaria para transformar dinámicamente un payload crudo de un proveedor
    (ej: Wialon, Protrack, etc.) al modelo canónico de RC (RCCanonicalModel)
    basado en un esquema de rutas JSON configurado previamente.
    """

    @staticmethod
    def _extract_value(payload: Dict[str, Any], path: str) -> Any:
        """
        Extrae un valor del payload anidado usando una ruta con notación de punto.
        Soporta rutas alternativas separadas por '||' (ej: 'position.lat || lat').
        Soporta índices numéricos (ej: 'records.0.latitude').
        """
        if not path:
            return None
            
        paths = [p.strip() for p in path.split('||')]
        
        for p in paths:
            if not p:
                continue
            keys = p.split('.')
            curr = payload
            found = True
            for k in keys:
                if isinstance(curr, dict) and k in curr:
                    curr = curr[k]
                elif isinstance(curr, list) and k.isdigit() and int(k) < len(curr):
                    curr = curr[int(k)]
                else:
                    found = False
                    break
            if found and curr is not None:
                return curr
                
        return None

    @staticmethod
    def _evaluate_rule(payload: Dict[str, Any], field: str, operator: str, value: str) -> bool:
        """Evalúa si un campo del payload cumple la condición de una trigger rule."""
        raw = payload.get(field)
        
        if operator == "exists":
            return raw is not None
        if operator == "not_exists":
            return raw is None
        if raw is None:
            return False
        
        raw_str = str(raw).strip()
        
        if operator == "eq":
            return raw_str == str(value).strip()
        if operator == "neq":
            return raw_str != str(value).strip()
        
        # Operadores numéricos
        try:
            raw_f = float(raw)
            val_f = float(value)
            if operator == "gt":  return raw_f >  val_f
            if operator == "lt":  return raw_f <  val_f
            if operator == "gte": return raw_f >= val_f
            if operator == "lte": return raw_f <= val_f
        except (ValueError, TypeError):
            pass
        
        return False

    @staticmethod
    def map_payload(payload: Dict[str, Any], schema: Dict[str, str], provider_name: str = None, env: str = None, require_dict_match: bool = False) -> RCCanonicalModel:
        """
        Mapea dinámicamente un payload JSON entrante al modelo estándar (RCCanonicalModel)
        basándose en un esquema de rutas configurado en la base de datos por el usuario.
        """
        
        def parse_date(date_str: Any) -> Optional[datetime]:
            if not date_str:
                return None
            try:
                # Si es un timestamp UNIX numérico puro
                if str(date_str).replace('.', '', 1).isdigit():
                    return datetime.fromtimestamp(float(date_str), tz=timezone.utc)
                dt = dateutil.parser.parse(str(date_str))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception as e:
                logger.warning(f"Error de conversión de tipo: {e}")
                return None
                
        def parse_float(val: Any) -> Optional[float]:
            if val is None or str(val).strip().lower() in ["", "null", "none"]:
                return None
            try:
                return float(val)
            except Exception as e:
                logger.warning(f"Error de conversión de tipo: {e}")
                return None

        # Buscar imei original para contingencia PULL y preservación
        original_imei = "UNKNOWN"
        inner_payload = payload.get("payload", payload) if isinstance(payload, dict) else payload
        
        for imei_key in ["imei", "serial_number", "serial", "device_id"]:
            if isinstance(inner_payload, dict):
                val = inner_payload.get(imei_key)
                if not val and "record" in inner_payload and isinstance(inner_payload["record"], list) and len(inner_payload["record"]) > 0:
                    val = inner_payload["record"][0].get(imei_key)
                if val:
                    original_imei = str(val)
                    break

        # Identificador principal
        chassis_val = DynamicMapper._extract_value(payload, schema.get("chassis_number", ""))
        chassis_number = str(chassis_val) if chassis_val is not None else original_imei
        
        # --- NUEVO: Inyección al Vuelo (Diccionario de Metadatos) ---
        if provider_name and env and chassis_number != "UNKNOWN":
            from app.database import get_session
            from app.models.config_models import ProviderDictionary
            db_global = get_session("system_config", "global")
            try:
                dict_entry = db_global.query(ProviderDictionary).filter_by(
                    provider_name=provider_name, 
                    env=env, 
                    dict_key=chassis_number
                ).first()
                if dict_entry and dict_entry.dict_value:
                    chassis_number = dict_entry.dict_value
                    logger.debug(f"Inyección al vuelo: {chassis_val} reemplazado por {chassis_number}")
                elif require_dict_match:
                    # El proveedor usa diccionario y este ID no tiene traducción.
                    # Enviar el ID crudo a RC generaría un activo no identificable
                    # (o peor, varios distintos colapsados en el mismo valor).
                    # Se descarta el evento: es responsabilidad del proveedor
                    # completar la asignación en su plataforma.
                    logger.warning(
                        f"[{provider_name}-{env}] ID '{chassis_number}' sin traducción "
                        f"en el diccionario. Evento descartado, no se envía a RC."
                    )
                    return None
            except Exception as e:
                logger.warning(f"Excepción capturada en dynamic_mapper: {e}")
                logger.error(f"Error en inyección al vuelo para {provider_name}_{env}: {e}")
            finally:
                db_global.close()
        
        # Coordenadas y Velocidad
        latitude = parse_float(DynamicMapper._extract_value(payload, schema.get("latitude", "")))
        longitude = parse_float(DynamicMapper._extract_value(payload, schema.get("longitude", "")))
        speed = parse_float(DynamicMapper._extract_value(payload, schema.get("speed", ""))) or 0.0
        
        # Evento o Motivo (Siempre debe ser string, por defecto '1' para Reporte Periódico de Posición)
        code_raw = DynamicMapper._extract_value(payload, schema.get("code", ""))
        code = str(code_raw) if code_raw is not None else "1"
        
        # Fecha en UTC
        date_val = parse_date(DynamicMapper._extract_value(payload, schema.get("date", "")))
        
        # Campos accesorios opcionales
        altitude = parse_float(DynamicMapper._extract_value(payload, schema.get("altitude", "")))
        battery = parse_float(DynamicMapper._extract_value(payload, schema.get("battery", "")))
        course = parse_float(DynamicMapper._extract_value(payload, schema.get("course", "")))
        humidity = parse_float(DynamicMapper._extract_value(payload, schema.get("humidity", "")))
        odometer = parse_float(DynamicMapper._extract_value(payload, schema.get("odometer", "")))
        temperature = parse_float(DynamicMapper._extract_value(payload, schema.get("temperature", "")))
        
        ignition_raw = DynamicMapper._extract_value(payload, schema.get("ignition", ""))
        ignition = _a_booleano(ignition_raw)
        
        serial_num = DynamicMapper._extract_value(payload, schema.get("serial_number", ""))
        if serial_num is None and original_imei != "UNKNOWN":
            serial_num = original_imei
            
        shipment_num = DynamicMapper._extract_value(payload, schema.get("shipment", ""))
        veh_type = DynamicMapper._extract_value(payload, schema.get("vehicle_type", ""))
        veh_brand = DynamicMapper._extract_value(payload, schema.get("vehicle_brand", ""))
        veh_model = DynamicMapper._extract_value(payload, schema.get("vehicle_model", ""))
        
        return RCCanonicalModel(
            chassis_number=chassis_number,
            latitude=latitude,
            longitude=longitude,
            speed=speed,
            code=code,
            date=date_val,
            altitude=altitude,
            battery=battery,
            course=course,
            humidity=humidity,
            ignition=ignition,
            odometer=odometer,
            temperature=temperature,
            serial_number=str(serial_num) if serial_num is not None else None,
            shipment=str(shipment_num) if shipment_num is not None else None,
            vehicle_type=str(veh_type) if veh_type is not None else None,
            vehicle_brand=str(veh_brand) if veh_brand is not None else None,
            vehicle_model=str(veh_model) if veh_model is not None else None
        )

    @staticmethod
    def map_payload_multi(
        payload: Dict[str, Any],
        full_schema: Dict[str, Any],
        provider_name: str = None,
        env: str = None,
        require_dict_match: bool = False
    ) -> list:
        """
        Motor de Reglas de Disparo.
        
        Dado un payload entrante y un schema con base_mapping + trigger_rules,
        retorna una LISTA de RCCanonicalModel — uno por cada regla activa que
        haga match, más el evento de posición base (default_rule).
        
        Compatibilidad hacia atrás: si full_schema no tiene 'base_mapping',
        se trata como schema plano (sin trigger_rules).
        """
        # Compatibilidad: schema plano (formato anterior) → sin reglas
        if "base_mapping" not in full_schema:
            base_schema  = full_schema
            trigger_rules = []
            default_rule  = {"enabled": True, "rc_code": "1", "fire_when": "always"}
        else:
            base_schema   = full_schema.get("base_mapping", {})
            trigger_rules = full_schema.get("trigger_rules", [])
            default_rule  = full_schema.get("default_rule", {
                "enabled": True, "rc_code": "1", "fire_when": "always"
            })

        results = []

        # 1. Generar evento base (posición GPS)
        if default_rule.get("enabled", True):
            base_schema_with_code = dict(base_schema)
            base_event = DynamicMapper.map_payload(
                payload, base_schema_with_code, provider_name, env, require_dict_match
            )
            # Sin traducción en el diccionario: se descarta el payload completo.
            # No tiene sentido emitir los eventos de trigger de un activo que RC
            # no puede identificar.
            if base_event is None:
                return []
            
            # ¿El usuario mapeó explícitamente el 'code' en Tab 3 y NO está usando reglas dinámicas?
            mapped_code_key = base_schema.get("code")
            is_static_code_mapped = bool(mapped_code_key and str(mapped_code_key).strip())
            
            if trigger_rules or not is_static_code_mapped:
                # Impone regla base si el multiplexor está activo o si no mapeó el code explícitamente
                base_event.code = str(default_rule.get("rc_code", "1"))
            elif is_static_code_mapped and not base_event.code:
                # Fallback de seguridad si el JSON vino sin la llave
                base_event.code = str(default_rule.get("rc_code", "1"))
                
            results.append(base_event)

        # 2. Evaluar trigger_rules — solo las habilitadas que hacen match
        for rule in trigger_rules:
            if not rule.get("enabled", True):
                continue
            
            field    = rule.get("field", "")
            operator = rule.get("operator", "eq")
            value    = rule.get("value", "1")
            rc_code  = rule.get("rc_code", "1")
            
            if not field:
                continue
            
            if DynamicMapper._evaluate_rule(payload, field, operator, value):
                # Clonar el evento base y cambiar solo el code
                trigger_event = DynamicMapper.map_payload(
                    payload, base_schema_with_code if "base_mapping" in full_schema else full_schema,
                    provider_name, env, require_dict_match
                )
                if trigger_event is None:
                    continue
                trigger_event.code = str(rc_code)
                results.append(trigger_event)

        # Garantía: si nada generó eventos (default desactivado y sin matches),
        # generar al menos el evento base con code="1"
        if not results:
            # El primer argumento es el payload, no el schema. Pasar el schema
            # hacía que no se encontrara ningún campo: el evento salía con
            # chassis_number="UNKNOWN" y coordenadas nulas, y además esquivaba
            # el control de traducción del diccionario (que se saltea cuando el
            # chassis ya es "UNKNOWN"). Resultado: un activo no identificable
            # llegaba a RC pese a la regla de "sin traducción no se envía".
            fallback = DynamicMapper.map_payload(
                payload, base_schema, provider_name, env, require_dict_match
            )
            if fallback is None:
                return []
            fallback.code = "1"
            results.append(fallback)

        return results
