import sys
from zeep import Client

def check_sig():
    c = Client('http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc?wsdl')
    # get the port/binding
    binding = list(c.wsdl.bindings.values())[0]
    
    print("=== GetUserToken ===")
    print(binding.get("GetUserToken").input.signature())
    
    print("\n=== GPSAssetTracking ===")
    print(binding.get("GPSAssetTracking").input.signature())

if __name__ == "__main__":
    check_sig()
