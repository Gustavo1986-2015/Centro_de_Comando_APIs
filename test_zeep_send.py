import os
from zeep import Client
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

RC_USERNAME = os.getenv("RC_USERNAME", "AC_avl_GustavoAC")
RC_PASSWORD = os.getenv("RC_PASSWORD", "RhVS_467FeNH_4")

def test_send():
    print(f"Auth: {RC_USERNAME} / {RC_PASSWORD}")
    client = Client('http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc?wsdl')
    
    print("Getting token...")
    token = client.service.GetUserToken(RC_USERNAME, RC_PASSWORD)
    print(f"Token received: {token}")
    
    if not token:
        print("Failed to get token!")
        return
        
    event_dict = {
        'altitude': "0",
        'asset': "GDG848",
        'battery': "14",
        'code': "IgnitionAlarm",
        'course': "21",
        'customer': {'id': '', 'name': ''},
        'date': datetime.now(),
        'direction': "21",
        'humidity': "0",
        'ignition': "false",
        'latitude': "51.883451",
        'longitude': "4.971413",
        'odometer': "126113",
        'serialNumber': "12345678",
        'shipment': "",
        'speed': "0",
        'temperature': "0",
        'vehicleType': "BOX_SEMITRAILER",
        'vehicleBrand': "SCHMITZ_CARGOBULL_AG",
        'vehicleModel': "CTU_3"
    }
    
    print("Sending GPSAssetTracking...")
    res = client.service.GPSAssetTracking(token, [event_dict])
    print(f"Response: {res}")

if __name__ == "__main__":
    test_send()
