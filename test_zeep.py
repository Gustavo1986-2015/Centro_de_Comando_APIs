import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

from zeep import Client
from zeep.cache import SqliteCache
from zeep.transports import Transport
import httpx

def explore_wsdl():
    wsdl_url = 'http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc?wsdl'
    client = Client(wsdl_url)
    
    print("SERVICIOS Y MÉTODOS:")
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            operations = port.binding._operations.values()
            for operation in operations:
                print(f" - {operation.name}")
                
    print("\n")

if __name__ == "__main__":
    explore_wsdl()
