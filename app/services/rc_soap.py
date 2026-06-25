import httpx
import logging
import os
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from app.schemas.canonical import RCCanonicalModel
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RC_USERNAME = os.getenv("RC_USERNAME", "demo")
RC_PASSWORD = os.getenv("RC_PASSWORD", "demo")
RC_ENDPOINT = os.getenv("RC_ENDPOINT", "http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc")
RC_USE_MOCK = os.getenv("RC_USE_MOCK", "False").lower() == "true"
APP_ENV = os.getenv("APP_ENV", "production").lower()

if RC_USE_MOCK:
    logger.warning(
        "⚠️ RC_USE_MOCK=True — los eventos NO se enviarán a Recurso Confiable. "
        "Solo usar en desarrollo."
    )
    if APP_ENV == "production":
        raise RuntimeError(
            "RC_USE_MOCK=True está prohibido en APP_ENV=production. "
            "Setea RC_USE_MOCK=False y configura credenciales RC reales."
        )

class RCSOAPClient:
    _global_zeep_client = None
    _global_lock = threading.RLock()

    def __init__(self, username: str = RC_USERNAME, password: str = RC_PASSWORD, endpoint: str = RC_ENDPOINT, use_mock: bool = False):
        self.username = username
        self.password = password
        self.endpoint = endpoint
        self.use_mock = use_mock
        self._token = None
        self._token_expires_at = None

    @classmethod
    def _get_zeep_client(cls, endpoint: str):
        # Verificación rápida sin lock (fast path)
        if cls._global_zeep_client:
            return cls._global_zeep_client
        # Inicialización con lock (slow path, solo primera vez)
        with cls._global_lock:
            if not cls._global_zeep_client:  # double-check dentro del lock
                from zeep import Client
                from zeep.transports import Transport
                import requests
                
                # Configurar timeout explícito para que el worker no se quede colgado
                session = requests.Session()
                session.timeout = (5, 25)  # (connect_timeout, read_timeout)
                transport = Transport(session=session)
                
                wsdl = endpoint + "?wsdl"
                cls._global_zeep_client = Client(wsdl, transport=transport)
        return cls._global_zeep_client

    def _load_token_from_cache(self):
        """Carga el token desde el archivo de caché en disco si existe y es válido."""
        cache_path = f"./db/rc_token_cache_{self.username}.json"
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            token = data.get("token")
            expires_at_str = data.get("expires_at")
            if token and expires_at_str:
                expires_at = datetime.fromisoformat(expires_at_str)
                # Si todavía quedan más de 10 minutos de validez del token
                if expires_at > datetime.now():
                    self._token = token
                    self._token_expires_at = expires_at
                    logger.info(f"Token recuperado de caché en disco. Vence el: {self._token_expires_at}")
                    return token
        except Exception as e:
            logger.warning(f"Excepción silenciada en ejecución: {e}")
            logger.warning(f"No se pudo leer el token de la caché en disco: {e}")
        return None

    def _save_token_to_cache(self, token: str, expires_at: datetime):
        """Guarda el token en el archivo de caché en disco."""
        cache_path = f"./db/rc_token_cache_{self.username}.json"
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "token": token,
                    "expires_at": expires_at.isoformat()
                }, f, indent=4)
            logger.info("Token guardado exitosamente en caché en disco.")
        except Exception as e:
            logger.warning(f"Excepción silenciada en ejecución: {e}")
            logger.warning(f"No se pudo guardar el token en la caché en disco: {e}")

    def _clear_token_cache(self):
        """Borra la caché de token en memoria y en disco."""
        self._token = None
        self._token_expires_at = None
        cache_path = f"./db/rc_token_cache_{self.username}.json"
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
                logger.info("Caché de token en disco eliminada.")
            except Exception as e:
                logger.debug(f"No se pudo eliminar archivo: {e}")
                logger.warning(f"No se pudo borrar el archivo de caché de token: {e}")

    def _authenticate_sync(self):
        """Autentica contra RC de forma síncrona usando Zeep con reintentos para mitigar colisiones."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"Autenticando contra Recurso Confiable (Zeep) - Intento {attempt + 1}...")
                client = self._get_zeep_client(self.endpoint)
                res = client.service.GetUserToken(self.username, self.password)
                
                from zeep.helpers import serialize_object
                res_native = serialize_object(res)
                
                if not res_native or not isinstance(res_native, dict):
                    raise Exception("Respuesta SOAP de autenticación inválida o vacía.")
                    
                token_val = res_native.get('token')
                if not token_val:
                    # Buscar detalle de excepción en la respuesta de login
                    exception_msg = "Credenciales incorrectas o error en el servicio de RC"
                    if "exception" in res_native and res_native["exception"]:
                        try:
                            key_vals = res_native["exception"].get("KeyValueOfstringstring", [])
                            if isinstance(key_vals, list) and len(key_vals) > 0:
                                exception_msg = ", ".join([f"{kv.get('Key')}: {kv.get('Value')}" for kv in key_vals if isinstance(kv, dict)])
                        except Exception as e:
                            logger.warning(f"Excepción silenciada en ejecución: {e}")
                            exception_msg = str(res_native["exception"])
                    raise Exception(f"Fallo de autenticación en RC: {exception_msg}")
                    
                self._token = token_val
                # Renovar 30 minutos antes de expirar (23.5 horas desde ahora)
                self._token_expires_at = datetime.now() + timedelta(hours=23, minutes=30)
                logger.info(f"Token real obtenido exitosamente. Expira a las: {self._token_expires_at}")
                
                # Guardar en disco
                self._save_token_to_cache(self._token, self._token_expires_at)
                return # Éxito, salir de los reintentos
                
            except Exception as e:
                logger.warning(f"Excepción silenciada en ejecución: {e}")
                err_str = str(e)
                if ("user_token_idx" in err_str or "duplicate key" in err_str.lower()) and attempt < max_retries - 1:
                    logger.warning(f"Colisión de token detectada en el servidor de RC (user_token_idx). Reintentando en 1.5 segundos...")
                    time.sleep(1.5)
                else:
                    raise e

    def _get_token_sync(self) -> str:
        """Devuelve el token en caché, o lo renueva si expiró, protegiendo con lock para evitar llamadas paralelas."""
        with self.__class__._global_lock:
            # 1. Intentar de memoria
            if self._token and self._token_expires_at and datetime.now() < self._token_expires_at:
                return self._token
                
            # 2. Intentar de disco
            cached_token = self._load_token_from_cache()
            if cached_token:
                return cached_token
                
            # 3. Si no hay token válido, autenticar
            self._authenticate_sync()
            return self._token

    def _send_batch_sync(self, events: list[RCCanonicalModel]):
        """Ejecuta la llamada SOAP síncrona para un lote de eventos."""
        token = self._get_token_sync()
        client = self._get_zeep_client(self.endpoint)
        
        event_dicts = []
        for event in events:
            # Recurso Confiable exige estricto UTC 0.
            # Convertimos o aseguramos que la fecha esté en UTC puro y le añadimos la Z del estándar ISO 8601
            base_date = event.date if event.date else datetime.now(timezone.utc)
            if base_date.tzinfo is None:
                base_date = base_date.replace(tzinfo=timezone.utc)
            else:
                base_date = base_date.astimezone(timezone.utc)
            
            # Sanitizar velocidad por si Schmitz envía literal "null"
            def clean_speed(s):
                if s is None:
                    return "0"
                s_str = str(s).strip().lower()
                if s_str in ["", "null", "none"]:
                    return "0"
                return s_str
            
            # Mapeo estricto soportado por Zeep usando tipos nativos y strings puros
            event_dict = {
                'asset': event.chassis_number or "",
                'code': event.code or "1",
                'customer': {'id': '', 'name': ''},
                'date': base_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                'direction': str(event.course) if event.course is not None else "0",
                'ignition': "true" if event.ignition else "false",
                'latitude': str(event.latitude) if event.latitude is not None else "0",
                'longitude': str(event.longitude) if event.longitude is not None else "0",
                'speed': clean_speed(event.speed),
            }
            
            if event.altitude is not None:
                event_dict['altitude'] = int(event.altitude)
            if event.battery is not None:
                event_dict['battery'] = int(event.battery)
            if event.humidity is not None:
                event_dict['humidity'] = int(event.humidity)
            if event.odometer is not None:
                event_dict['odometer'] = int(event.odometer)
            if event.temperature is not None:
                event_dict['temperature'] = float(event.temperature)
            if event.serial_number:
                event_dict['serialNumber'] = str(event.serial_number)
            if event.shipment:
                event_dict['shipment'] = str(event.shipment)
                
            event_dicts.append(event_dict)
        
        # Enviar (Zeep requiere mapear explícitamente el array a la llave 'Event' del esquema XML)
        res = client.service.GPSAssetTracking(token, {'Event': event_dicts})
        return res

    def _parse_single_response(self, res_item) -> tuple[bool, str, str]:
        if res_item is None:
            return False, f"rc_err_no_resp_{int(datetime.now().timestamp())}", "No se recibió respuesta del servidor RC"
            
        raw_response = str(res_item)
        id_job = None
        has_exception = False
        exception_msg = ""
        
        if isinstance(res_item, dict):
            id_job = res_item.get("idJob")
            if "exception" in res_item and res_item["exception"]:
                try:
                    key_vals = res_item["exception"].get("KeyValueOfstringstring", [])
                    if isinstance(key_vals, list) and len(key_vals) > 0:
                        has_exception = True
                        exception_msg = ", ".join([f"{kv.get('Key')}: {kv.get('Value')}" for kv in key_vals if isinstance(kv, dict)])
                except Exception as e:
                    logger.warning(f"Excepción silenciada en ejecución: {e}")
                    exception_msg = str(res_item["exception"])
                    if "KeyValueOfstringstring" in exception_msg:
                        has_exception = True
                        
        # Si detectamos un error de token o autenticación, invalidamos la caché
        err_lower = (exception_msg + " " + raw_response).lower()
        if any(w in err_lower for w in ["unknown_token", "userunk", "autentica", "token", "incorrecta", "contrase"]):
            logger.warning("Token de RC inválido o rechazado en producción. Limpiando caché de token.")
            self._clear_token_cache()
            
        success = True
        job_id = None
        
        # Si idJob es 0 (o None) o si contiene excepción de negocio, es fallido
        if id_job is not None:
            id_job_str = str(id_job)
            if id_job_str == "0" or has_exception:
                success = False
                job_id = f"rc_err_{int(datetime.now().timestamp())}"
                raw_response = f"Error RC: {exception_msg or raw_response}"
            else:
                success = True
                job_id = id_job_str
        else:
            if isinstance(res_item, dict) and "job_id" in res_item:
                job_id = str(res_item["job_id"])
            else:
                success = False
                job_id = f"rc_err_no_id_{int(datetime.now().timestamp())}"
                raw_response = f"Respuesta SOAP sin campo idJob: {raw_response}"
                
        return success, job_id, raw_response

    async def send_events_batch(self, events: list[RCCanonicalModel]):
        """
        Envía un lote de eventos a RC en una sola petición SOAP.
        Devuelve una lista de tuplas (success: bool, job_id: str, raw_response: str) en el mismo orden que 'events'.
        """
        if not events:
            return []
            
        try:
            if RC_USE_MOCK or self.use_mock:
                # Simulación de éxito para todo el lote
                results = []
                import random
                for ev in events:
                    mock_job_id = f"job_mock_{int(datetime.now().timestamp())}_{random.randint(100, 999)}"
                    mock_json_response = f'{{"timestamp": "{datetime.now(timezone.utc).isoformat()}", "level": "INFO", "event_type": "batch_sent", "status": "success", "job_id": "{mock_job_id}"}}'
                    results.append((True, mock_job_id, mock_json_response))
                return results
            else:
                import asyncio
                from zeep.helpers import serialize_object
                
                # Delegar la llamada SOAP bloqueante a un thread separado para no congelar FastAPI
                res = await asyncio.to_thread(self._send_batch_sync, events)
                
                # Convertir a objetos y listas nativos de Python
                res_native = serialize_object(res)
                
                # Asegurar que res_list es una lista
                if not isinstance(res_native, list):
                    res_list = [res_native]
                else:
                    res_list = res_native
                    
                results = []
                
                # Caso A: Retorna una sola respuesta global para todo el lote (comportamiento observado de RC)
                # O si no coincide el tamaño y es de tamaño 1
                if len(res_list) == 1 and len(events) > 1:
                    # Extraer el resultado global
                    res_item = res_list[0]
                    success, job_id, raw_response = self._parse_single_response(res_item)
                    # Aplicarlo a todos los eventos del lote
                    results = [(success, job_id, raw_response) for _ in events]
                else:
                    # Caso B: Mapeo posicional (un resultado por evento, o tamaño no coincide de otra forma)
                    for idx, ev in enumerate(events):
                        # Intentar obtener el resultado posicional
                        res_item = res_list[idx] if idx < len(res_list) else (res_list[0] if res_list else None)
                        success, job_id, raw_response = self._parse_single_response(res_item)
                        results.append((success, job_id, raw_response))
                        
                return results
                
        except Exception as e:
            logger.warning(f"Excepción silenciada en ejecución: {e}")
            logger.error(f"Error fatal al enviar lote SOAP a RC: {str(e)}")
            # Si toda la llamada falló (ej. error de conexión o credenciales incorrectas en GetUserToken)
            err_str = str(e)
            if "token" in err_str.lower() or "auth" in err_str.lower() or "GetUserToken" in err_str:
                self._clear_token_cache()
            return [(False, f"rc_conn_err_{int(datetime.now().timestamp())}", err_str) for _ in events]

    async def send_event(self, event: RCCanonicalModel):
        """
        Envía el evento a RC de forma individual (por compatibilidad).
        Devuelve (success: bool, job_id: str, raw_response: str)
        """
        results = await self.send_events_batch([event])
        if results:
            return results[0]
        return False, f"rc_err_empty_{int(datetime.now().timestamp())}", "No response from batch dispatcher"





def get_rc_client(username: str = None, password: str = None, use_mock: bool = False) -> RCSOAPClient:
    username = username or RC_USERNAME
    password = password or RC_PASSWORD
    return RCSOAPClient(username=username, password=password, use_mock=use_mock)
