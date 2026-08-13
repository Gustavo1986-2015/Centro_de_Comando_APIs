import logging
from enum import Enum
from zeep.exceptions import Fault
import os
import json
import threading
import time
import hashlib
import base64
from datetime import datetime, timedelta, timezone
from app.schemas.canonical import RCCanonicalModel
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken

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

def _nombre_cache_seguro(username: str) -> str:
    """
    Nombre de archivo derivado del usuario, restringido a caracteres inertes.

    El usuario viene de la configuración del panel, así que no es entrada
    anónima, pero un valor con barras o puntos dobles escribiría fuera de db/.
    Un hash corto desambigua dos usuarios que se reduzcan al mismo texto.
    """
    import hashlib
    import re

    limpio = re.sub(r"[^A-Za-z0-9_-]", "_", username or "")[:40]
    firma = hashlib.sha256((username or "").encode("utf-8")).hexdigest()[:8]
    return f"rc_token_cache_{limpio}_{firma}.json"


class RCResponseCategory(str, Enum):
    """
    Clasificación de la respuesta de Recurso Confiable, según su ESTRUCTURA.

    Antes se decidía retry-vs-fail buscando palabras sueltas en el texto de la
    respuesta. Un error transitorio cuyo mensaje no contuviera esas palabras
    terminaba marcado como fallo permanente y se perdía en la purga de 24h.

    Las cuatro formas están definidas en el contrato D-TI-15 v14:

      SUCCESS   exception nil + idJob presente y distinto de 0
      BUSINESS  exception poblado, o idJob 0/ausente. Permanente: reenviar los
                mismos datos vuelve a fallar
      AUTH      subcaso de BUSINESS (SQL:USERUNK, "Autenticación incorrecta").
                Se resuelve renovando el token, así que sí se reintenta
      PROTOCOL  s:Fault de WCF, típicamente DeserializationFailed por un campo
                mal formado. Permanente por la misma razón que BUSINESS
      TRANSPORT sin envelope SOAP: timeout, conexión rechazada, 5xx. Transitorio

      SIMULADO  no hubo llamada: el proveedor está en modo simulado. No es una
                respuesta de RC. Se distingue de SUCCESS a propósito: en el log
                ambos casos se veían igual, y quien lee "SUCCESS" da por hecho
                que RC recibió el evento cuando en realidad nunca salió.
    """
    SUCCESS = "SUCCESS"
    SIMULADO = "SIMULADO"
    BUSINESS = "BUSINESS"
    AUTH = "AUTH"
    PROTOCOL = "PROTOCOL"
    TRANSPORT = "TRANSPORT"


# Categorías que ameritan reintentar. El resto es permanente: volver a enviar
# los mismos bytes produciría el mismo resultado.
CATEGORIAS_REINTENTABLES = {RCResponseCategory.TRANSPORT, RCResponseCategory.AUTH}

# Marcadores de fallo de autenticación según la tabla de errores controlados
# del contrato (D-TI-15 v14, pág. 11). Se comparan sobre los campos `key` y
# `message` de la excepción, no sobre el texto completo de la respuesta.
_MARCADORES_AUTH = (
    "sql:userunk",
    "autenticación incorrecta",
    "autentificación incorrecta",
    "unknown_token",
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
                
                # Timeouts de Zeep. IMPORTANTE: los dos parámetros del Transport
                # NO son connect/read como sugiere la intuición:
                #
                #   timeout            -> load_timeout: descarga y parseo del WSDL.
                #                         Ocurre una sola vez al construir el cliente
                #                         y es la operación MÁS pesada de todas.
                #   operation_timeout  -> cada llamada SOAP posterior.
                #
                # Tener load_timeout bajo hacía fallar el primer lote tras cada
                # arranque (RC tarda varios segundos en servir el WSDL) y mandaba
                # el lote completo a reintento sin necesidad.
                #
                # requests.Session no respeta un atributo `timeout`; solo lo usa
                # si se pasa explícito en cada request. Por eso no se setea acá.
                load_timeout = int(os.getenv("RC_WSDL_TIMEOUT", "45"))
                op_timeout   = int(os.getenv("RC_OPERATION_TIMEOUT", "30"))

                session = requests.Session()
                transport = Transport(
                    session=session,
                    timeout=load_timeout,
                    operation_timeout=op_timeout,
                )

                wsdl = endpoint + "?wsdl"
                logger.info(
                    f"Inicializando cliente SOAP RC (wsdl_timeout={load_timeout}s, "
                    f"operation_timeout={op_timeout}s)"
                )
                cls._global_zeep_client = Client(wsdl, transport=transport)
        return cls._global_zeep_client

    def _get_fernet(self):
        enc_key_str = os.getenv("RC_TOKEN_ENC_KEY")
        if enc_key_str:
            try:
                return Fernet(enc_key_str.encode())
            except Exception as e:
                logger.warning(f"RC_TOKEN_ENC_KEY es inválido: {e}. Usando fallback a RC_PASSWORD.")
        
        # Fallback: derivar clave usando RC_PASSWORD
        if not self.password:
            return None
            
        key = base64.urlsafe_b64encode(hashlib.sha256(self.password.encode()).digest())
        return Fernet(key)

    def _load_token_from_cache(self):
        """Carga el token desde el archivo de caché en disco si existe y es válido."""
        cache_path = os.path.join("db", _nombre_cache_seguro(self.username))
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "rb") as f:
                file_bytes = f.read()
                
            fernet = self._get_fernet()
            if not fernet:
                logger.warning("No hay clave de cifrado (ni password) para leer el caché.")
                return None
                
            try:
                decrypted = fernet.decrypt(file_bytes)
                data = json.loads(decrypted.decode("utf-8"))
            except InvalidToken:
                logger.warning(f"Token cache corrupto o clave rotada para {self.username}, purgando y re-autenticando.")
                try:
                    os.remove(cache_path)
                except Exception as e:
                    logger.debug(f"No se pudo borrar cache corrupto: {e}")
                return None
            except json.JSONDecodeError as e:
                logger.warning(f"Error decodificando JSON del caché cifrado: {e}")
                return None
                
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
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning(f"Excepción capturada en rc_soap: {e}")
            logger.warning(f"No se pudo leer el token de la caché en disco: {e}")
        return None

    def _save_token_to_cache(self, token: str, expires_at: datetime):
        """Guarda el token en el archivo de caché en disco."""
        fernet = self._get_fernet()
        if not fernet:
            logger.info("Modo solo-memoria: No hay clave de cifrado ni password, token no se guardará en disco.")
            return

        cache_path = os.path.join("db", _nombre_cache_seguro(self.username))
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            payload = json.dumps({
                "token": token,
                "expires_at": expires_at.isoformat()
            }).encode("utf-8")
            
            ciphertext = fernet.encrypt(payload)
            
            with open(cache_path, "wb") as f:
                f.write(ciphertext)
            logger.info("Token guardado exitosamente en caché en disco (cifrado AES).")
        except Exception as e:
            logger.warning(f"Excepción capturada en rc_soap: {e}")
            logger.warning(f"No se pudo guardar el token en la caché en disco: {e}")

    def _clear_token_cache(self):
        """Borra la caché de token en memoria y en disco."""
        self._token = None
        self._token_expires_at = None
        cache_path = os.path.join("db", _nombre_cache_seguro(self.username))
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
                            logger.warning(f"Excepción capturada en rc_soap: {e}")
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
                logger.warning(f"Excepción capturada en rc_soap: {e}")
                err_str = str(e)
                if ("user_token_idx" in err_str or "duplicate key" in err_str.lower()) and attempt < max_retries - 1:
                    logger.warning("Colisión de token detectada en el servidor de RC (user_token_idx). Reintentando en 1.5 segundos...")
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
            # Recurso Confiable exige UTC estricto, sin desplazamiento horario.
            # La fecha se serializa como YYYY-MM-DDTHH:MM:SS, SIN sufijo Z: así
            # lo especifica el contrato D-TI-15 v14 y así lo envía el cliente de
            # referencia en producción.
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
                # Sin sufijo Z: el contrato D-TI-15 v14 especifica
                # YYYY-MM-DDTHH:MM:SS en UTC, y así lo envía el cliente de
                # referencia en producción. RC valida el formato de forma
                # estricta y responde s:Fault DeserializationFailed si no
                # puede parsearlo como DateTime.
                'date': base_date.strftime("%Y-%m-%dT%H:%M:%S"),
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

    @staticmethod
    def _extraer_excepcion(res_item) -> tuple[bool, str]:
        """
        Extrae el arreglo KeyValueOfstringstring de la excepción, si existe.

        Devuelve (hay_excepcion, texto). Un `exception` presente pero con la
        lista vacía NO es error: así responde RC en el caso exitoso cuando el
        cliente SOAP materializa el nodo nil.
        """
        if not isinstance(res_item, dict):
            return False, ""

        excepcion = res_item.get("exception")
        if not excepcion:
            return False, ""

        try:
            pares = excepcion.get("KeyValueOfstringstring", [])
            if isinstance(pares, list) and pares:
                texto = ", ".join(
                    f"{kv.get('Key')}: {kv.get('Value')}"
                    for kv in pares if isinstance(kv, dict)
                )
                return True, texto
        except (AttributeError, TypeError) as e:
            logger.warning(f"Estructura de excepción inesperada en respuesta de RC: {e}")
            texto = str(excepcion)
            return ("KeyValueOfstringstring" in texto), texto

        return False, ""

    @staticmethod
    def _es_fallo_de_autenticacion(texto_excepcion: str) -> bool:
        """
        Distingue el subcaso AUTH dentro de los errores de negocio.

        Importa porque es el único permanente-en-apariencia que sí conviene
        reintentar: renovando el token, el mismo evento se acepta.
        """
        minusculas = (texto_excepcion or "").lower()
        return any(marcador in minusculas for marcador in _MARCADORES_AUTH)

    def _parse_single_response(self, res_item) -> tuple[bool, str, str, RCResponseCategory]:
        """
        Clasifica una respuesta de RC por su estructura.

        Retorna (exito, job_id, respuesta_cruda, categoria).
        """
        if res_item is None:
            # Sin respuesta no se puede afirmar que RC no la haya recibido:
            # se trata como transitorio para no descartar el evento.
            return (
                False,
                f"rc_err_no_resp_{int(datetime.now().timestamp())}",
                "No se recibió respuesta del servidor RC",
                RCResponseCategory.TRANSPORT,
            )

        raw_response = str(res_item)
        id_job = res_item.get("idJob") if isinstance(res_item, dict) else None
        hay_excepcion, texto_excepcion = self._extraer_excepcion(res_item)

        # ── Error de autenticación: renovar token y reintentar ───────────────
        if hay_excepcion and self._es_fallo_de_autenticacion(texto_excepcion):
            logger.warning(
                f"[RC:AUTH] Token rechazado por RC. Se limpia la caché para "
                f"renovarlo en el próximo intento. Detalle: {texto_excepcion}"
            )
            self._clear_token_cache()
            return (
                False,
                f"rc_auth_{int(datetime.now().timestamp())}",
                f"Error de autenticación RC: {texto_excepcion}",
                RCResponseCategory.AUTH,
            )

        # ── Rechazo de negocio: permanente ───────────────────────────────────
        if hay_excepcion:
            logger.error(
                f"[RC:BUSINESS] RC rechazó el evento de forma permanente. "
                f"No se reintenta. Detalle: {texto_excepcion}"
            )
            return (
                False,
                f"rc_business_{int(datetime.now().timestamp())}",
                f"Rechazo de negocio RC: {texto_excepcion}",
                RCResponseCategory.BUSINESS,
            )

        # ── idJob: el acuse de recibo ────────────────────────────────────────
        if id_job is not None:
            id_job_str = str(id_job)
            if id_job_str and id_job_str != "0":
                return True, id_job_str, raw_response, RCResponseCategory.SUCCESS

            logger.error(
                f"[RC:BUSINESS] RC devolvió idJob=0 sin excepción explícita. "
                f"El evento no fue registrado. Respuesta: {raw_response[:200]}"
            )
            return (
                False,
                f"rc_business_idjob0_{int(datetime.now().timestamp())}",
                f"RC devolvió idJob=0: {raw_response}",
                RCResponseCategory.BUSINESS,
            )

        # Compatibilidad con respuestas que traen job_id en lugar de idJob
        if isinstance(res_item, dict) and res_item.get("job_id"):
            return True, str(res_item["job_id"]), raw_response, RCResponseCategory.SUCCESS

        # ── Respuesta bien formada pero sin acuse ────────────────────────────
        # No se puede confirmar el registro, y tampoco descartarlo: ante la duda
        # se reintenta, porque la pérdida silenciosa es el error más costoso.
        logger.warning(
            f"[RC:TRANSPORT] Respuesta de RC sin idJob ni excepción. "
            f"Se reintentará. Respuesta: {raw_response[:200]}"
        )
        return (
            False,
            f"rc_err_no_id_{int(datetime.now().timestamp())}",
            f"Respuesta SOAP sin campo idJob: {raw_response}",
            RCResponseCategory.TRANSPORT,
        )

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
                    results.append((True, mock_job_id, mock_json_response, RCResponseCategory.SIMULADO))
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
                    success, job_id, raw_response, categoria = self._parse_single_response(res_item)
                    # Aplicarlo a todos los eventos del lote
                    results = [(success, job_id, raw_response, categoria) for _ in events]
                else:
                    # Caso B: Mapeo posicional (un resultado por evento, o tamaño no coincide de otra forma)
                    for idx, ev in enumerate(events):
                        # Intentar obtener el resultado posicional
                        res_item = res_list[idx] if idx < len(res_list) else (res_list[0] if res_list else None)
                        success, job_id, raw_response, categoria = self._parse_single_response(res_item)
                        results.append((success, job_id, raw_response, categoria))
                        
                return results
                
        except Fault as e:
            # s:Fault de WCF: el mensaje no se pudo deserializar (un campo con
            # formato inválido, típicamente la fecha). Reenviar exactamente los
            # mismos bytes produce el mismo error, así que no se reintenta.
            err_str = str(e)
            logger.error(
                f"[RC:PROTOCOL] RC rechazó el mensaje por formato. No se reintenta. "
                f"Revisar la serialización de los campos. Detalle: {err_str[:300]}"
            )
            return [
                (False, f"rc_protocol_{int(datetime.now().timestamp())}", err_str,
                 RCResponseCategory.PROTOCOL)
                for _ in events
            ]

        except Exception as e:
            # Todo lo demás (timeout, conexión rechazada, 5xx) es transitorio:
            # el evento no llegó a RC y volver a intentarlo tiene sentido.
            err_str = str(e)
            logger.warning(
                f"[RC:TRANSPORT] Fallo de transporte hacia RC. Se reintentará. "
                f"Detalle: {err_str[:300]}"
            )
            if "token" in err_str.lower() or "auth" in err_str.lower() or "GetUserToken" in err_str:
                self._clear_token_cache()
            return [
                (False, f"rc_conn_err_{int(datetime.now().timestamp())}", err_str,
                 RCResponseCategory.TRANSPORT)
                for _ in events
            ]

    async def send_event(self, event: RCCanonicalModel):
        """
        Envía el evento a RC de forma individual (por compatibilidad).

        Devuelve (success, job_id, raw_response) sin la categoría, para no
        romper a los llamadores que esperan tres elementos.
        """
        results = await self.send_events_batch([event])
        if results:
            # send_events_batch devuelve 4 elementos (con categoría); acá se
            # recortan a 3 para respetar el contrato de este método.
            primera = results[0]
            return primera[0], primera[1], primera[2]
        return False, f"rc_err_empty_{int(datetime.now().timestamp())}", "No response from batch dispatcher"





def get_rc_client(username: str = None, password: str = None, use_mock: bool = False) -> RCSOAPClient:
    username = username or RC_USERNAME
    password = password or RC_PASSWORD
    return RCSOAPClient(username=username, password=password, use_mock=use_mock)
