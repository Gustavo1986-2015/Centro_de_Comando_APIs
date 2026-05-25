import httpx
import logging
import os
import json
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from app.schemas.canonical import RCCanonicalModel
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RC_USERNAME = os.getenv("RC_USERNAME", "demo")
RC_PASSWORD = os.getenv("RC_PASSWORD", "demo")
RC_ENDPOINT = os.getenv("RC_ENDPOINT", "http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc")
RC_USE_MOCK = os.getenv("RC_USE_MOCK", "True").lower() == "true"

class RCSOAPClient:
    def __init__(self, username: str = RC_USERNAME, password: str = RC_PASSWORD, endpoint: str = RC_ENDPOINT):
        self.username = username
        self.password = password
        self.endpoint = endpoint
        self._token = None
        self._token_expires_at = None
        self._zeep_client = None

    def _get_zeep_client(self):
        if not self._zeep_client:
            from zeep import Client
            wsdl = self.endpoint + "?wsdl"
            self._zeep_client = Client(wsdl)
        return self._zeep_client

    def _authenticate_sync(self):
        """Autentica contra RC de forma síncrona usando Zeep."""
        logger.info("Autenticando contra Recurso Confiable (Zeep)...")
        client = self._get_zeep_client()
        res = client.service.GetUserToken(self.username, self.password)
        
        if not res or 'token' not in res:
            raise Exception("Respuesta inválida al solicitar token SOAP a RC")
            
        self._token = res['token']
        # Renovar 30 minutos antes de expirar (23.5 horas desde ahora)
        self._token_expires_at = datetime.now() + timedelta(hours=23, minutes=30)
        logger.info(f"Token real obtenido exitosamente. Expira a las: {self._token_expires_at}")

    def _get_token_sync(self) -> str:
        """Devuelve el token en caché, o lo renueva si expiró."""
        if not self._token or not self._token_expires_at or datetime.now() >= self._token_expires_at:
            self._authenticate_sync()
        return self._token

    def _send_sync(self, event: RCCanonicalModel):
        """Ejecuta la llamada SOAP síncrona."""
        token = self._get_token_sync()
        client = self._get_zeep_client()
        
        # Recurso Confiable asume UTC puro siempre, según documentación
        rc_date = event.date if event.date else datetime.now()
        
        # Mapeo estricto soportado por Zeep usando tipos nativos de Python
        event_dict = {
            'asset': event.chassis_number or "",
            'code': event.code or "1",
            'customer': {'id': '', 'name': ''},
            'date': rc_date, # Zeep lo formatea a xsd:dateTime automáticamente
            'direction': str(event.course) if event.course is not None else "0",
            'ignition': bool(event.ignition),
            'latitude': float(event.latitude) if event.latitude is not None else 0.0,
            'longitude': float(event.longitude) if event.longitude is not None else 0.0,
            'speed': int(event.speed) if event.speed is not None else 0,
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
        
        # Enviar
        res = client.service.GPSAssetTracking(token, [event_dict])
        return res

    async def send_event(self, event: RCCanonicalModel):
        """
        Envía el evento a RC.
        Devuelve (success: bool, job_id: str, raw_response: str)
        """
        try:
            if RC_USE_MOCK:
                # Simulación de éxito internamente (Sin conectarse a internet)
                mock_job_id = f"job_mock_{int(datetime.now().timestamp())}"
                mock_json_response = f'{{"timestamp": "{datetime.now(timezone.utc).isoformat()}", "level": "INFO", "event_type": "batch_sent", "status": "success", "job_id": "{mock_job_id}"}}'
                return True, mock_job_id, mock_json_response
            else:
                import asyncio
                # Delegar la llamada SOAP bloqueante a un thread separado para no congelar FastAPI
                res = await asyncio.to_thread(self._send_sync, event)
                
                raw_response = str(res)
                
                # Intentar parsear un ID de la respuesta
                job_id = "rc_prod_id"
                if isinstance(res, list) and len(res) > 0 and hasattr(res[0], '__getitem__') and "idJob" in res[0] and res[0]["idJob"]:
                    job_id = str(res[0]["idJob"])
                elif isinstance(res, dict) and "idJob" in res and res["idJob"]:
                    job_id = str(res["idJob"])
                elif isinstance(res, dict) and "job_id" in res:
                    job_id = res["job_id"]
                else:
                    job_id = f"rc_job_{int(datetime.now().timestamp())}"
                    
                return True, job_id, raw_response

        except Exception as e:
            logger.error(f"Error al enviar evento a RC: {str(e)}")
            return False, None, str(e)

rc_client = RCSOAPClient()
