# Centro de Comando APIs | Assistcargo

Hub Telemático corporativo para recibir, parsear y encolar eventos GPS provenientes de APIs de terceros (Push), transformándolos en un Modelo Canónico y enviándolos asíncronamente a Recurso Confiable.

## 🚀 Arquitectura del Sistema
El sistema ha sido diseñado para escalar a más de 15 proveedores simultáneos con cero pérdida de rendimiento, empleando un diseño moderno y seguro.

- **Colas Híbridas (Per-Provider):** Cada proveedor puede configurar de manera granular su motor de colas. La `QueueFactory` ruteará los eventos de alto tráfico hacia **Redis** (in-memory) y los de tráfico estable hacia **SQLite** (`schmitz_prod.db`), maximizando el rendimiento sin perder flexibilidad.
- **Auto-Escalado Dinámico (El Director):** El Worker ha sido rediseñado como un despachador inteligente. Si detecta cientos de eventos encolados, omite los descansos (Burst Mode) e invoca múltiples trabajadores (`asyncio.gather`) de manera dinámica (ej. 5 procesos paralelos). Esto derrite las colas masivas instantáneamente bajando la latencia a menos de un segundo.
- **Guardado Atómico en Lote (Batch Save):** Para evitar los cuellos de botella de disco ("database is locked"), el Director agrupa las respuestas del Web Service y realiza una única escritura masiva a la base de datos por lote, eliminando el IO iterativo.
- **Reintentos Asíncronos con Backoff Lineal:** Cuando un envío falla por problemas temporales de red o autenticación, el evento permanece en base de datos como `pending`. Se encola con tiempos de espera progresivos (1° intento: +10s, 2° intento: +45s, 3° intento: +120s, 4° intento: +300s, máx 4 intentos). El worker omite de forma inteligente estos eventos hasta cumplir su backoff, dejando que el resto del tráfico fluya de inmediato.
- **Modelo Canónico (Pydantic):** Validación estricta. Todo lo que ingresa de un externo se transforma a un formato estándar de Assistcargo antes de viajar a Recurso Confiable.
- **Seguridad Perimetral (Toggle Switch):** Los Webhooks receptores cuentan con validación de "API Keys" mediante cabeceras HTTP, las cuales pueden activarse/desactivarse en caliente desde el archivo `.env` para facilitar pruebas.
- **Auditoría Dinámica y Recuperación Ante Desastres:** Cada payload crudo recibido se guarda instantáneamente en un `.jsonl` rotativo por proveedor. Adicionalmente, ante apagones de servidor bruscos, el Hub recupera automáticamente eventos "atascados" devolviéndolos a la cola principal.

### 🛠️ Tecnologías Clave Utilizadas
- **Python 3.12+** / **FastAPI**: Backend de altísimo rendimiento asíncrono.
- **Zeep**: Cliente SOAP industrial para integración con Recurso Confiable con validación estricta de WSDL.
- **SQLAlchemy (SQLite Múltiple)**: Gestión concurrente de bases de datos locales sin cuellos de botella.
- **Uvicorn**: Servidor ASGI en producción.
- **HTML5/Vanilla CSS/JS**: Frontend puro sin frameworks pesados, con diseño "Dark Glassmorphism".

## 🎛️ Dashboard y Panel de Administración
El servidor incluye una interfaz web interactiva (Vanilla JS, CSS Premium, sin frameworks pesados) para la gestión visual del Hub.

- **Dashboard Principal:** Métricas en tiempo real (Pendientes, Enviados, Fallidos, Reintentos) y streaming de la última actividad global.
- **Doble Capa de Visibilidad de Latencia:** 
  - *Latencia de Transmisión:* Muestra en la columna Localización el retraso satelital/celular externo desde que el GPS del camión reportó el dato hasta que ingresó a Assistcargo.
  - *Latencia del Hub (Hub: Xs):* Muestra de forma destacada en verde brillante cuánto tiempo exacto demoró el Hub de Assistcargo en procesar y despachar el dato a RC una vez recibido en nuestra API.
- **Filtros Interactivos:** Filtrado dinámico instantáneo en el DOM por proveedor y por rangos de latencia de RC (Baja $\le$ 2s, Media 3-9s, Alta $\ge$ 10s).
- **Historial Diario Inteligente:** Pestaña dedicada con un registro histórico consolidado persistente (`daily_stats`) de procesados, enviados y fallidos. El sistema calcula matemáticamente la medianoche local para realizar un barrido perfecto "Hoy" sin importar la zona horaria UTC del servidor.
- **Configuración Global por API:** Panel visual para activar/desactivar el procesamiento de cada proveedor, establecer credenciales de RC, configurar el Motor de Colas (`sqlite` vs `redis`), y ajustar los intervalos de purga.
- **Visor de Bases de Datos (DB Viewer):** Herramienta administrativa para consultar, sin salir de la web, la estructura y los datos en vivo de cualquier tabla en cualquiera de las bases de datos dinámicas (`.db`) del ecosistema.
- **Logs de Auditoría:** Pantalla para inspeccionar en vivo los JSONs crudos que están ingresando al sistema.
- **Simulador de Webhooks Blindado:** Herramienta interna para inyectar payloads de prueba, que desvía sus operaciones hacia un entorno `test_unit` para nunca corromper los datos reales del cliente en el visor.

## 💻 Ejecución en Desarrollo (Local)
1. Instalar dependencias:
   ```bash
   pip install fastapi uvicorn sqlalchemy pydantic zeep
   ```
2. Configurar entorno seguro:
   Copia el archivo `.env.example` y renómbralo a `.env`. Coloca allí tus credenciales reales (este archivo es ignorado por git por seguridad).
3. Ejecutar el servidor web (con recarga automática):
   ```bash
   uvicorn main:app --port 8000 --reload
   ```
4. Abrir el panel de control: [http://localhost:8000/dashboard](http://localhost:8000/dashboard)

## 🌐 Despliegue en Producción (Servidor Windows / AWS)
*Esta sección detalla cómo mantener el sistema vivo 24/7 sin consolas abiertas.*

Dado que la aplicación web necesita que el motor de Python esté siempre encendido, se recomienda utilizar **NSSM (Non-Sucking Service Manager)** para encapsular el comando de inicio dentro de un Servicio Nativo de Windows.

1. Descarga NSSM (http://nssm.cc/).
2. En la terminal del servidor AWS ejecuta:
   ```cmd
   nssm install CentroComandoAPIs
   ```
3. En la ventana que aparece:
   - **Path:** Ruta al ejecutable de Python (ej. `C:\Python310\python.exe`)
   - **Arguments:** `-m uvicorn main:app --host 0.0.0.0 --port 8000`
   - **Directory:** Ruta a este proyecto (ej. `C:\Users\Administrador\Desktop\Centro_de_Comando_APIs`)
4. Inicia el servicio desde el Administrador de Servicios de Windows (services.msc) o con `nssm start CentroComandoAPIs`.

A partir de ese momento, el Hub Telemático arrancará automáticamente con Windows de manera invisible y silenciosa.

---
**Desarrollado por el Área de Integraciones GPS Assistcargo**
*Gustavo Gómez & Roberto Herrera ®*
