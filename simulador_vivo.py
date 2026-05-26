import requests
import time
import random
import datetime

# Placas de prueba solicitadas
PLACAS = ["A1A123", "GDG848", "XX0001"]

# Coordenadas base (Buenos Aires)
BASE_LAT = -34.603722
BASE_LON = -58.381592

WEBHOOK_URL = "http://localhost:8000/webhook/schmitz"

def generar_evento(placa):
    # Generar tiempos actuales
    ahora_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    
    # Mover levemente las coordenadas para simular movimiento
    lat = BASE_LAT + random.uniform(-0.05, 0.05)
    lon = BASE_LON + random.uniform(-0.05, 0.05)
    
    # Simular otros datos
    velocidad = random.randint(40, 100)
    bateria = random.randint(12, 14)
    rumbo = random.choice([0, 90, 180, 270])
    ignicion = random.choice([True, False])

    payload = {
      "ChassisNumber": placa,
      "CtuId": 12345678,
      "Plate": placa,
      "ReceiveTime": ahora_utc,
      "DeviceTime": ahora_utc,
      "StatusData": [
        {
          "Position": {
            "GPSDateTime": ahora_utc,
            "DateTime": ahora_utc,
            "Latitude": round(lat, 6),
            "Longitude": round(lon, 6),
            "GPSHeading": str(rumbo),
            "GPSMilage": {
              "exists": True,
              "Value": random.randint(50000, 150000)
            },
            "Location": {
              "City": "Buenos Aires"
            },
            "Altitude": random.randint(0, 500)
          },
          "SensorStatus": {
            "DateTime": ahora_utc,
            "IsIgnitionOn": ignicion,
            "Battery": {
              "ExternalPowerSupplyVoltage": bateria
            }
          },
          "EBS": {
            "DateTime": ahora_utc,
            "Velocity": str(velocidad),
            "Milage": random.randint(50000, 150000)
          }
        }
      ],
      "Reason": {
        "Item": True,
        "ItemElementName": "0"
      }
    }
    
    return payload

print("=== INICIANDO SIMULADOR EN VIVO (SCHMITZ) ===")
print("Presiona Ctrl+C para detener.")

while True:
    placa = random.choice(PLACAS)
    evento = generar_evento(placa)
    
    print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] Enviando patente {placa}...")
    
    try:
        response = requests.post(WEBHOOK_URL, json=evento)
        if response.status_code == 200:
            print(f" -> ÉXITO (HTTP 200). Respuesta: {response.text}")
        else:
            print(f" -> ERROR HTTP {response.status_code}. Respuesta: {response.text}")
    except Exception as e:
        print(f" -> ERROR de conexión: {str(e)}")
        
    # Esperar 30 segundos
    time.sleep(30)
