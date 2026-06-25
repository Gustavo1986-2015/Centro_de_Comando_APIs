import os
import json
import traceback
from app.providers.schmitz.mapper import map_schmitz_payload
import logging
logger = logging.getLogger(__name__)


def test_real_data():
    base_dir = r"C:\Users\gustavogomez\Downloads\Quickstart_RESTPushAPI_v_1_35_v01_eng\Demo_Payloads"
    
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
                    mapped_data = map_schmitz_payload(payload)
                    success += 1
                    
                    # Imprimir datos clave para visualizarlos en logs
                    print(f"[OK] {os.path.basename(root)}/{f} -> "
                          f"Chassis: {mapped_data.chassis_number}, "
                          f"Lat: {mapped_data.latitude}, Speed: {mapped_data.speed}, "
                          f"Date: {mapped_data.date}")
                          
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
