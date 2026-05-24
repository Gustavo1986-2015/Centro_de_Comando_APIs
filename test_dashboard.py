from fastapi.testclient import TestClient
from main import app
import sys

client = TestClient(app)

def test_dashboard():
    # 1. Probar HTML renderizado
    resp_html = client.get("/dashboard")
    if resp_html.status_code == 200 and "<html" in resp_html.text:
        print("[OK] Dashboard HTML cargó correctamente.")
    else:
        print("[ERROR] Falló carga HTML.")
        sys.exit(1)

    # 2. Probar API de Stats
    resp_api = client.get("/api/stats")
    if resp_api.status_code == 200:
        data = resp_api.json()
        print(f"[OK] API Stats responde. Pendientes: {data['pending']}, Enviados: {data['sent']}, Fallidos: {data['failed']}")
    else:
        print("[ERROR] Falló API Stats.")
        sys.exit(1)

if __name__ == "__main__":
    test_dashboard()
