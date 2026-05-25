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
        
        # Mapeo a diccionario plano soportado por Zeep
        event_dict = {
            'altitude': str(event.altitude) if event.altitude is not None else "0",
            'asset': event.chassis_number or "",
            'battery': str(event.battery) if event.battery is not None else "0",
            'code': event.code or "1",
            'course': str(event.course) if event.course is not None else "0",
            'customer': {'id': '', 'name': ''},
            'date': rc_date.strftime("%Y-%m-%dT%H:%M:%S"),
            'direction': str(event.course) if event.course is not None else "0",
            'humidity': str(event.humidity) if event.humidity is not None else "0",
            'ignition': "true" if event.ignition else "false",
            'latitude': str(event.latitude) if event.latitude is not None else "0",
            'longitude': str(event.longitude) if event.longitude is not None else "0",
            'odometer': str(event.odometer) if event.odometer is not None else "0",
            'serialNumber': event.serial_number or "",
            'shipment': event.shipment or "",
            'speed': str(event.speed) if event.speed is not None else "0",
            'temperature': str(event.temperature) if event.temperature is not None else "0",
            'vehicleType': event.vehicle_type or "",
            'vehicleBrand': event.vehicle_brand or "",
            'vehicleModel': event.vehicle_model or ""
        }
        
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
