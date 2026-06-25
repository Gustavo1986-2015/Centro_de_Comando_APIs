import os
import json
import traceback
from app.providers.schmitz.mapper import map_schmitz_payload
import logging
logger = logging.getLogger(__name__)

import sys

PAYLOADS_DIR = os.getenv("SCHMITZ_PAYLOADS_DIR")

def test_real_data():
    if not PAYLOADS_DIR:
        print("SCHMITZ_PAYLOADS_DIR no seteado. Saltando test (no hay payloads disponibles).")
        sys.exit(0)
    
    if not os.path.isdir(PAYLOADS_DIR):
        print(f"SCHMITZ_PAYLOADS_DIR='{PAYLOADS_DIR}' no es un directorio valido. Saltando test.")
        sys.exit(0)
        
    if not os.listdir(PAYLOADS_DIR):
        logger.warning(f"El directorio '{PAYLOADS_DIR}' existe pero esta vacio. Saltando test.")
        sys.exit(0)
        
    print(f"Probando mapper con payloads de: {PAYLOADS_DIR}")
    base_dir = PAYLOADS_DIR
    total_files = 0
    success = 0
    failures = 0
    
    # Recorrer todos los archivos
    for root, dirs, files in os.walk(base_dir):
        for f in files:
            if f.endswith(".json"):
                total_files += 1
                filepath = os.path.join(root, f)
                try:
                    with open(filepath, "r", encoding="utf-8") as file:
                        payload = json.load(file)
                        
                    # Probar mapeo
                    mapped_list = map_schmitz_payload(payload)
                    if not mapped_list:
                        print(f"  [WARN] Payload {os.path.basename(root)}/{f} no genero eventos canonicos.")
                        failures += 1
                        continue
                        
                    mapped_data = mapped_list[0]
                    success += 1
                    
                    # Imprimir datos clave para visualizarlos en logs
                    codes = [m.code for m in mapped_list]
                    print(f"[OK] {os.path.basename(root)}/{f} -> "
                          f"Chassis: {mapped_data.chassis_number}, "
                          f"Lat: {mapped_data.latitude}, Speed: {mapped_data.speed}, "
                          f"Date: {mapped_data.date}, "
                          f"Eventos generados: {len(mapped_list)}, "
                          f"Codes: {codes}")
                          
                except Exception as e:
                    logger.warning(f"Error de conversión de tipo: {e}")
                    failures += 1
                    print(f"[ERROR] {os.path.basename(root)}/{f}: {str(e)}")
                    traceback.print_exc()

    print("\n--- RESUMEN ---")
    print(f"Total archivos JSON: {total_files}")
    print(f"Mapeos Exitosos: {success}")
    print(f"Fallos: {failures}")

if __name__ == "__main__":
    test_real_data()
