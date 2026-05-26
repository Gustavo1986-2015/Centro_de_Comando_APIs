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
    PLACAS = [f"TEST-{str(i).zfill(3)}" for i in range(1, 26)]
    SEGUNDOS_ESPERA = 2
else:
    PLACAS = ["RHR5776", "GDG8486", "JMC1236"]
    SEGUNDOS_ESPERA = 2

WEBHOOK_URL = "http://localhost:8000/schmitz/webhook?env=test"

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
        
    # Reemplazar todos los tiempos por la hora actual UTC
    ahora_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    update_datetime_recursive(payload, ahora_utc)
    
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
                errores += 1
                
        elapsed = time.time() - start_time
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Ráfaga completada en {elapsed:.2f} segundos. Éxitos: {exitos} | Errores: {errores}. Esperando {SEGUNDOS_ESPERA} segundos...")
    else:
        placa = random.choice(PLACAS)
        evento, tipo_evento, archivo = generar_evento(placa)
        print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] Enviando {placa} | Archivo base: {archivo} | Tipo Alarma: {tipo_evento}")
        try:
            response = requests.post(WEBHOOK_URL, json=evento, timeout=5)
            if response.status_code in [200, 202]:
                print(f" -> ÉXITO. Respuesta: {response.text}")
            else:
                print(f" -> ERROR HTTP {response.status_code}. Respuesta: {response.text}")
        except Exception as e:
            print(f" -> ERROR de conexión: {str(e)}")
        
    time.sleep(SEGUNDOS_ESPERA)
