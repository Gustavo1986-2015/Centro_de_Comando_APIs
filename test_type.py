import sys
from zeep import Client

def check_type():
    c = Client('http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc?wsdl')
    print(c.get_type('ns2:Event').signature())

if __name__ == "__main__":
    check_type()
