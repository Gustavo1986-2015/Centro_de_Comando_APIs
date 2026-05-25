import asyncio
import logging
import sys

# Forzar logs a stdout
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

from app.services.rc_soap import rc_client
from app.schemas.canonical import RCCanonicalModel
from datetime import datetime

async def test():
    event = RCCanonicalModel(
        chassis_number="GDG848",
        latitude=51.883451,
        longitude=4.971413,
        speed=0,
        code="IgnitionAlarm",
        date=datetime.now(),
        altitude=0,
        battery=14,
        course=21,
        humidity=0,
        ignition=False,
        odometer=126113,
        temperature=0,
        serial_number="12345678",
        shipment="",
        vehicle_type="BOX_SEMITRAILER",
        vehicle_brand="SCHMITZ_CARGOBULL_AG",
        vehicle_model="CTU_3"
    )
    res = await rc_client.send_event(event)
    print("RESULTADO:", res)

if __name__ == "__main__":
    asyncio.run(test())
