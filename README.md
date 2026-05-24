# Hub de Integración Telemática y Centro de Comando

Microservicio on-premise desarrollado con FastAPI, SQLite y Vanilla JS que actúa como un hub centralizador de eventos telemáticos.

## Características Principales
*   **Recepción de Webhooks**: Endpoint asíncrono para recibir eventos (inicialmente Schmitz Cargobull v1.35).
*   **Mapeo y Normalización**: Transformación de payloads crudos a un modelo canónico centralizado (RC Canonical Model).
*   **Auditoría Dinámica**: Almacenamiento rotativo en archivos `.jsonl` separados por proveedor.
*   **Persistencia y Encolamiento**: Base de datos SQLite para mantener estado de los eventos recibidos y su estado de procesamiento.
*   **Cliente SOAP (Próximamente)**: Worker en background para la comunicación con Recurso Confiable.
*   **Dashboard Reactivo (Próximamente)**: Interfaz de Centro de Comando en tiempo real.

## Tecnologías Utilizadas
*   Python 3
*   FastAPI
*   SQLAlchemy
*   SQLite
*   HTTPX
*   Pydantic

## Despliegue en Windows (Producción)

Para instalar el Hub como un servicio de Windows que arranque automáticamente en segundo plano (24/7) y sobreviva a reinicios, usaremos **NSSM** (Non-Sucking Service Manager).

### 1. Instalar el Servicio (NSSM)
1. Descarga [NSSM](http://nssm.cc/download).
2. Abre la consola de Windows (CMD o PowerShell) como Administrador.
3. Navega a la carpeta de NSSM y ejecuta:
   ```cmd
   nssm install HubTelematico
   ```
4. Se abrirá una interfaz gráfica. Configura lo siguiente:
   * **Path**: La ruta absoluta a tu ejecutable de Python (ej. `C:\Ruta\A\python.exe` o de tu entorno virtual).
   * **Arguments**: `-m uvicorn main:app --host 0.0.0.0 --port 8000`
   * **Details > Display name**: `Centro de Comando APIs (Hub Telemático)`
   * **Details > Description**: `Microservicio backend de integración GPS para Assistcargo`
   * **Details > Startup type**: `Automatic`
5. Haz clic en "Install service".

Para iniciarlo manualmente por primera vez:
```cmd
nssm start HubTelematico
```

### 2. Crear el Lanzador de Escritorio (.exe)
Hemos provisto un script `launcher.py` que abre automáticamente el navegador apuntando al Dashboard. Para convertirlo en un icono clickeable `.exe`:

1. Instala PyInstaller:
   ```cmd
   pip install pyinstaller
   ```
2. Compila el ejecutable sin ventana de consola (`--noconsole`) y en un solo archivo (`--onefile`):
   ```cmd
   pyinstaller --onefile --noconsole --name "Centro_Comando_Assistcargo" launcher.py
   ```
3. Encontrarás tu `.exe` dentro de la carpeta `dist/`. ¡Puedes crear un acceso directo de ese archivo en tu escritorio!
