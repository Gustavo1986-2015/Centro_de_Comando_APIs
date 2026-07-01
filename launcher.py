import webbrowser
import time
import sys

def main():
    url = "http://localhost:8000/dashboard"
    print(f"Iniciando Centro de Comando...")
    print(f"Abriendo navegador en: {url}")
    
    # Pausa de 2 segundos para dar tiempo a que el servicio Windows (NSSM) esté listo
    # en caso de que se haya reiniciado el servidor recientemente.
    time.sleep(2) 
    webbrowser.open(url)
    
    # Termina la ejecución (ya que solo es un lanzador)
    sys.exit(0)

if __name__ == "__main__":
    main()
