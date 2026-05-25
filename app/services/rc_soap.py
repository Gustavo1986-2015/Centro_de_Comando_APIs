import httpx
import logging
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET
from app.schemas.canonical import RCCanonicalModel

logger = logging.getLogger(__name__)

class RCSOAPClient:
    def __init__(self, username: str = "demo", password: str = "demo", endpoint: str = "http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc"):
        self.username = username
        self.password = password
        self.endpoint = endpoint
        self._token = None
        self._token_expires_at = None

    async def _authenticate(self):
        """Simula la obtención del token SOAP (válido por 24h)."""
        logger.info("Autenticando contra Recurso Confiable...")
        # Simulación de respuesta
        self._token = "mock_token_12345"
        # Renovar 30 minutos antes de expirar (23.5 horas desde ahora)
        self._token_expires_at = datetime.now() + timedelta(hours=23, minutes=30)
        logger.info(f"Token obtenido exitosamente. Expira a las: {self._token_expires_at}")

    async def get_token(self) -> str:
        """Devuelve el token en caché, o lo renueva si expiró."""
        if not self._token or not self._token_expires_at or datetime.now() >= self._token_expires_at:
            await self._authenticate()
        return self._token

    def _build_xml(self, token: str, event: RCCanonicalModel) -> str:
        """Construye el XML estricto para RC."""
        asset = event.chassis_number or ""
        altitude = event.altitude if event.altitude is not None else "0"
        battery = event.battery if event.battery is not None else "0"
        code = event.code or "1"
        direction = event.course if event.course is not None else "0"
        
        if event.date:
            date_str = event.date.strftime("%Y-%m-%dT%H:%M:%S")
        else:
            date_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            
        humidity = event.humidity if event.humidity is not None else "0"
        ignition = "true" if event.ignition else "false"
        latitude = event.latitude if event.latitude is not None else "0"
        longitude = event.longitude if event.longitude is not None else "0"
        odometer = event.odometer if event.odometer is not None else "0"
        serialNumber = event.serial_number or ""
        shipment = event.shipment or ""
        speed = event.speed if event.speed is not None else "0"
        temperature = event.temperature if event.temperature is not None else "0"
        vehicleType = event.vehicle_type or ""
        vehicleBrand = event.vehicle_brand or ""
        vehicleModel = event.vehicle_model or ""

        xml = f"""<tem:events>
<iron:Event>
<iron:altitude>{altitude}</iron:altitude>
<iron:asset>{asset}</iron:asset>
<iron:battery>{battery}</iron:battery>
<iron:code>{code}</iron:code>
<iron:customer>
<iron:id></iron:id>
<iron:name></iron:name>
</iron:customer>
<iron:date>{date_str}</iron:date>
<iron:direction>{direction}</iron:direction>
<iron:humidity>{humidity}</iron:humidity>
<iron:ignition>{ignition}</iron:ignition>
<iron:latitude>{latitude}</iron:latitude>
<iron:longitude>{longitude}</iron:longitude>
<iron:odometer>{odometer}</iron:odometer>
<iron:serialNumber>{serialNumber}</iron:serialNumber>
<iron:shipment>{shipment}</iron:shipment>
<iron:speed>{speed}</iron:speed>
<iron:temperature>{temperature}</iron:temperature>
<iron:vehicleType>{vehicleType}</iron:vehicleType>
<iron:vehicleBrand>{vehicleBrand}</iron:vehicleBrand>
<iron:vehicleModel>{vehicleModel}</iron:vehicleModel>
</iron:Event>
</tem:events>"""
        return xml

    async def send_event(self, event: RCCanonicalModel):
        """
        Envía el evento a RC.
        Devuelve (success: bool, job_id: str, raw_response: str)
        """
        try:
            token = await self.get_token()
            xml_payload = self._build_xml(token, event)
            
            headers = {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": '"http://rc-mock-url.com/soap/ReportEvent"'
            }

            # En producción, descomentar la llamada real y extraer el job_id del XML/JSON
            # async with httpx.AsyncClient() as client:
            #     response = await client.post(self.endpoint, content=xml_payload, headers=headers, timeout=10.0)
            #     response.raise_for_status()
            #     raw_response = response.text
            
            # Simulación de éxito simulando la respuesta JSON que provee RC internamente
            mock_job_id = f"job_mock_{int(datetime.now().timestamp())}"
            mock_json_response = f'{{"timestamp": "{datetime.now(timezone.utc).isoformat()}", "level": "INFO", "event_type": "batch_sent", "status": "success", "job_id": "{mock_job_id}"}}'
            
            return True, mock_job_id, mock_json_response

        except Exception as e:
            logger.error(f"Error al enviar evento a RC: {str(e)}")
            return False, None, str(e)

rc_client = RCSOAPClient()
