import requests
import time
import random
import datetime
import json
import os
import glob
from concurrent.futures import ThreadPoolExecutor

# MODO ESTRÉS MASIVO
# Si es True, inyectará 25 patentes ficticias al azar cada 2 segundos.
# Si es False, usará las 3 placas originales cada 30 segundos.
MODO_ESTRES = True

if MODO_ESTRES:
    PLACAS = [f"TEST-{str(i).zfill(3)}" for i in range(1, 46)]
    SEGUNDOS_ESPERA = 10
else:
    PLACAS = ["RHR5776", "GDG8486", "JMC1236"]
    SEGUNDOS_ESPERA = 2

WEBHOOK_URL = "http://localhost:8000/schmitz/webhook?env=test"
# WEBHOOK_URL = "https://schmit-test.onrender.com/schmitz/webhook?env=test"
# Ruta a los payloads reales de prueba de Schmitz
DEMO_PAYLOADS_DIR = r"C:\Users\gustavogomez\Downloads\Quickstart_RESTPushAPI_v_1_35_v01_eng\Demo_Payloads"

# Cargar todos los archivos JSON de los subdirectorios
payload_files = glob.glob(os.path.join(DEMO_PAYLOADS_DIR, "**", "*.json"), recursive=True)

if not payload_files:
    print(f"Error: No se encontraron archivos JSON en {DEMO_PAYLOADS_DIR}")
    exit(1)

def update_datetime_recursive(obj, current_time_str):
    """Busca cualquier campo de fecha/hora y lo actualiza a la hora actual."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            # Actualizar cualquier campo que parezca ser de tiempo
            if k in ["ReceiveTime", "DeviceTime", "GPSDateTime", "DateTime"] and isinstance(v, str):
                obj[k] = current_time_str
            else:
                update_datetime_recursive(v, current_time_str)
    elif isinstance(obj, list):
        for item in obj:
            update_datetime_recursive(item, current_time_str)

def generar_evento(placa):
    # Elegir un payload real al azar
    json_path = random.choice(payload_files)
    
    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
        
    # Reemplazar la patente para nuestras pruebas
    if "ChassisNumber" in payload:
        payload["ChassisNumber"] = placa
    if "Plate" in payload:
        payload["Plate"] = placa
        
    # 1. Generar horario local de un país europeo aleatorio (offset +00:00 a +03:00) con dispersión temporal en el pasado
    # Restar de 0 a 600 segundos (hasta 10 minutos) para que las fechas GPS de los vehículos no coincidan exactamente
    segundos_pasado = random.randint(0, 600)
    ahora_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=segundos_pasado)
    
    offset_hours = random.choice([0, 1, 2, 3])
    local_time = ahora_utc + datetime.timedelta(hours=offset_hours)
    
    if offset_hours == 0:
        offset_str = "Z"
    else:
        offset_str = f"+{str(offset_hours).zfill(2)}:00"
        
    fecha_local_str = local_time.strftime(f"%Y-%m-%dT%H:%M:%S.0000000{offset_str}")
    update_datetime_recursive(payload, fecha_local_str)
    
    # 2. Generar coordenadas geográficas aleatorias dentro de Europa
    # Latitud aproximada: 40.0 (España/Italia) a 58.0 (Norte de Europa)
    # Longitud aproximada: -8.0 (Portugal/España) a 25.0 (Europa Oriental)
    lat_eur = round(random.uniform(40.0, 58.0), 6)
    lon_eur = round(random.uniform(-8.0, 25.0), 6)
    
    if "StatusData" in payload and isinstance(payload["StatusData"], list) and len(payload["StatusData"]) > 0:
        status_data_0 = payload["StatusData"][0]
        if "Position" not in status_data_0 or not isinstance(status_data_0["Position"], dict):
            status_data_0["Position"] = {}
        status_data_0["Position"]["Latitude"] = lat_eur
        status_data_0["Position"]["Longitude"] = lon_eur
    
    # Extraer de qué tipo fue el payload base para mostrarlo en consola
    tipo_evento = payload.get("Reason", {}).get("ItemElementName", "Desconocido")
    archivo_base = os.path.basename(json_path)
    
    return payload, tipo_evento, archivo_base

print(f"=== INICIANDO SIMULADOR AVANZADO EN VIVO ===")
print(f"Se encontraron {len(payload_files)} payloads reales de Schmitz.")
print(f"Modo Estrés: {'ACTIVADO (2 seg)' if MODO_ESTRES else 'DESACTIVADO (30 seg)'}")
print("Presiona Ctrl+C para detener.")

def enviar_evento_worker(placa):
    evento, tipo_evento, archivo = generar_evento(placa)
    try:
        response = requests.post(WEBHOOK_URL, json=evento, timeout=5)
        if response.status_code in [200, 202]:
            return True, None
        return False, f"HTTP {response.status_code}: {response.text}"
    except Exception as e:
        return False, str(e)

while True:
    if MODO_ESTRES:
        print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] Iniciando ráfaga de estrés concurrente: inyectando {len(PLACAS)} vehículos...")
        
        start_time = time.time()
        exitos = 0
        errores = 0
        
        # Enviar las peticiones HTTP en paralelo utilizando 25 hilos
        with ThreadPoolExecutor(max_workers=len(PLACAS)) as executor:
            resultados = list(executor.map(enviar_evento_worker, PLACAS))
            
        for success, err in resultados:
            if success:
                exitos += 1
            else:
                if errores < 3:
                    print(f" -> Detalle de Error: {err}")
                errores += 1
                
        elapsed = time.time() - start_time
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Ráfaga completada en {elapsed:.2f} segundos. Éxitos: {exitos} | Errores: {errores}. Esperando {SEGUNDOS_ESPERA} segundos...")
    else:
        placa = random.choice(PLACAS)
        evento, tipo_evento, archivo = generar_evento(placa)
        print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] Enviando {placa} | Archivo base: {archivo} | Tipo Alarma: {tipo_evento}")
        try:
            response = requests.post(WEBHOOK_URL, json=evento, timeout=30)
            if response.status_code in [200, 202]:
                print(f" -> ÉXITO. Respuesta: {response.text}")
            else:
                print(f" -> ERROR HTTP {response.status_code}. Respuesta: {response.text}")
        except Exception as e:
            print(f" -> ERROR de conexión: {str(e)}")
        
    time.sleep(SEGUNDOS_ESPERA)
