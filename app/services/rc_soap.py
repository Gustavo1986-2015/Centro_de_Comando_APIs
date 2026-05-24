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
        """Construye el XML omitiendo valores nulos."""
        # Se asume una estructura base, el formato real dependerá del WSDL de RC.
        root = ET.Element("soap:Envelope", {"xmlns:soap": "http://schemas.xmlsoap.org/soap/envelope/"})
        body = ET.SubElement(root, "soap:Body")
        report = ET.SubElement(body, "ReportEvent")
        
        ET.SubElement(report, "Token").text = token
        
        # Agregar dinámicamente solo si no es nulo
        event_dict = event.model_dump(exclude_none=True)
        for key, value in event_dict.items():
            if value is not None:
                # Convertir fechas a ISO string
                if isinstance(value, datetime):
                    value_str = value.isoformat()
                else:
                    value_str = str(value)
                    
                # RC suele usar prefijos como iron: o CamelCase
                # Para el ejemplo usamos el nombre tal cual
                ET.SubElement(report, key).text = value_str

        # Convertir a string
        return ET.tostring(root, encoding="utf-8").decode("utf-8")

    async def send_event(self, event: RCCanonicalModel) -> bool:
        """
        Envía el evento a RC.
        Devuelve True si fue exitoso, False en caso contrario.
        """
        try:
            token = await self.get_token()
            xml_payload = self._build_xml(token, event)
            
            headers = {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": '"http://rc-mock-url.com/soap/ReportEvent"'
            }

            # En producción, descomentar la llamada real
            # async with httpx.AsyncClient() as client:
            #     response = await client.post(self.endpoint, content=xml_payload, headers=headers, timeout=10.0)
            #     response.raise_for_status()
            
            # Simulación de éxito
            # logger.info(f"Evento {event.chassis_number} enviado a RC con éxito.")
            return True

        except Exception as e:
            logger.error(f"Error al enviar evento a RC: {str(e)}")
            return False

rc_client = RCSOAPClient()
