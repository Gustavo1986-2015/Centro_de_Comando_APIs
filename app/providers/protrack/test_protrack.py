"""
Script de prueba rápida de conectividad con la API Protrack365.

Uso:
    python -m app.providers.protrack.test_protrack [env]

    env = test (por defecto) | prod

Verifica:
  1. Obtencion de token
  2. Listado de dispositivos
  3. Posicion actual de los IMEIs encontrados
  4. Mapeo al modelo canonico
"""

import asyncio
import sys
import os
import json
from pathlib import Path

# Configurar salida UTF-8 en Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Cargar .env desde la raíz del proyecto
env_path = Path(__file__).parent.parent.parent.parent / ".env"
if env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(env_path))

from app.providers.protrack.client import get_protrack_client
from app.providers.protrack.mapper import map_protrack_track


async def main():
    target_env = sys.argv[1] if len(sys.argv) > 1 else "test"
    print(f"\n{'='*60}")
    print(f"  Test de conectividad Protrack365 - entorno: {target_env.upper()}")
    print(f"{'='*60}\n")

    try:
        client = get_protrack_client(target_env)
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # -- 1. Token ----------------------------------------------------
    print("[Paso 1] Obteniendo access_token...")
    try:
        token = await client.get_token()
        print(f"   OK  Token obtenido: {token[:20]}...  (expira en ~2h)\n")
    except Exception as e:
        print(f"   FALLO  Error al obtener token: {e}")
        sys.exit(1)

    # -- 2. Dispositivos ---------------------------------------------
    print("[Paso 2] Listando dispositivos...")
    try:
        devices = await client.get_devices()
        print(f"   OK  {len(devices)} dispositivos encontrados.\n")
        for d in devices[:5]:
            print(f"      IMEI={d.get('imei')}  Patente={d.get('platenumber') or '(vacia)'}  Tipo={d.get('devicetype')}")
        if len(devices) > 5:
            print(f"      ... y {len(devices) - 5} mas.")
    except Exception as e:
        print(f"   FALLO  Error al listar dispositivos: {e}")
        devices = []

    if not devices:
        print("\nWARN: Sin dispositivos disponibles. Finalizando test.")
        return

    # -- 3. Track ----------------------------------------------------
    imeis = [d["imei"] for d in devices if d.get("imei")]
    print(f"\n[Paso 3] Consultando posicion actual de {len(imeis)} IMEI(s)...")
    try:
        tracks = await client.get_track(imeis)
        print(f"   OK  {len(tracks)} registros de posicion recibidos.\n")
    except Exception as e:
        print(f"   FALLO  Error al obtener track: {e}")
        tracks = []

    # -- 4. Mapping --------------------------------------------------
    if tracks:
        print("[Paso 4] Mapeando al modelo canonico (primeros 3)...")
        device_index = {d.get("imei"): d for d in devices}

        for t in tracks[:3]:
            imei_key = t.get("imei", "")
            dev_info = device_index.get(imei_key, {})
            try:
                canonical = map_protrack_track(t, dev_info)
                print(f"\n   IMEI: {imei_key}")
                print(f"   chassis_number : {canonical.chassis_number}")
                print(f"   lat/lon        : {canonical.latitude}, {canonical.longitude}")
                print(f"   speed          : {canonical.speed} km/h")
                print(f"   ignition       : {canonical.ignition}")
                print(f"   date (UTC)     : {canonical.date}")
                print(f"   code           : {canonical.code}")
                print(f"   vehicle_type   : {canonical.vehicle_type}")
                print(f"   vehicle_model  : {canonical.vehicle_model}")
            except Exception as map_err:
                print(f"   FALLO mapping para IMEI {imei_key}: {map_err}")

    print(f"\n{'='*60}")
    print("  Test completado exitosamente.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
