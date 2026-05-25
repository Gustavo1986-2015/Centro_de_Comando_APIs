import asyncio
from datetime import datetime, timezone, timedelta
import logging

from zeep.plugins import HistoryPlugin
from app.services.rc_soap import rc_client
from app.schemas.canonical import RCCanonicalModel

history = HistoryPlugin()

async def run_test():
    # Asegurarnos de que el cliente zeep esté instanciado y no use mock
    import app.services.rc_soap as rc_soap
    rc_soap.RC_USE_MOCK = False
    
    rc_client._get_zeep_client()
    rc_client._zeep_client.plugins = [history]
    
    event = RCCanonicalModel(
        chassis_number="GDG123",
        date=datetime.now(timezone.utc),
        latitude=-34.603722,
        longitude=-58.381592,
        speed=114,
        code="0",
        ignition=False,
        serial_number="190299",
        altitude=387,
        battery=55,
        odometer=97284,
        temperature=32.7,
        humidity=33,
        course=62
    )
    
    print("Enviando evento...")
    # Prueba envolver en Event
    rc_soap.rc_client._token = "test"
    rc_soap.rc_client._token_expires_at = datetime.now() + timedelta(days=1)
    
    event_dict = {
        'asset': event.chassis_number or "",
        'code': event.code or "1",
        'customer': {'id': '', 'name': ''},
        'date': event.date,
        'direction': str(event.course) if event.course is not None else "0",
        'ignition': bool(event.ignition),
        'latitude': float(event.latitude) if event.latitude is not None else 0.0,
        'longitude': float(event.longitude) if event.longitude is not None else 0.0,
        'speed': int(event.speed) if event.speed is not None else 0,
        'altitude': int(event.altitude),
        'battery': int(event.battery),
        'humidity': int(event.humidity),
        'odometer': int(event.odometer),
        'temperature': float(event.temperature),
        'serialNumber': str(event.serial_number)
    }
    
    res = rc_client._zeep_client.service.GPSAssetTracking("test", {'Event': [event_dict]})
    print("Enviado")
    
    if history.last_sent:
        from lxml import etree
        print("====== REQUEST XML ======")
        print(etree.tostring(history.last_sent['envelope'], pretty_print=True).decode('utf-8'))

if __name__ == "__main__":
    asyncio.run(run_test())
