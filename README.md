# Centro de Comando APIs | Assistcargo

Hub Telemático corporativo centralizado para la recepción, transformación y orquestación de eventos GPS provenientes de APIs de terceros (Push y Pull). El sistema procesa los datos hacia un Modelo Canónico estandarizado y los despacha de manera asíncrona hacia Recurso Confiable (RC), ofreciendo paralelamente un **Dashboard de monitoreo táctico en tiempo real**.

---

## 🚀 Arquitectura y Capacidades Clave

El sistema ha sido diseñado desde cero para soportar un alto throughput (miles de eventos por segundo) sin pérdida de rendimiento ni interbloqueos, empleando un diseño moderno, asíncrono y seguro.

### 1. Ingesta Híbrida y Procesamiento Asíncrono
- **Motores PUSH (Webhooks):** Recepción pasiva de eventos. Validaciones de esquema estricto y encolado en milisegundos.
- **Motores PULL (Cron-Driven):** Tareas en segundo plano (vía `httpx` asíncrono) para orquestar consumos periódicos desde APIs externas (ej. Protrack). Extrae de forma dinámica listas de activos y maneja firmas de seguridad MD5 dinámicas al vuelo.
- **Auto-Escalado y Despertador Thread-Safe:** El *Worker* de despacho actúa como un motor inteligente. Utiliza eventos asíncronos (`asyncio.Event`) para despertar instantáneamente apenas ingresa tráfico. Ante ráfagas masivas (Burst Mode), despliega *ThreadPools* ampliados para derretir las colas locales bajando la latencia a menos de 0.25s.

### 2. Base de Datos Fragmentada (Sharding)
Para evitar el "Database Locked" característico de SQLite bajo estrés, se implementa **Sharding por Proveedor y Entorno**. Cada integración escribe exclusivamente en su propio archivo físico (ej. `protrack_prod.db`, `schmitz_test.db`). Esto garantiza que picos de tráfico en un proveedor no afecten el rendimiento ni la latencia de otros proveedores.

### 3. Modelo Canónico y Resiliencia
- **Validación Estricta:** Todo dato entrante se filtra mediante `Pydantic` hacia el **Modelo Canónico** de Assistcargo.
- **Reintentos Inteligentes (Backoff Lineal):** Si un envío a RC falla (timeout, error 500), el evento queda retenido y se reintenta progresivamente (Ej. +10s, +45s, +120s, +300s). El Worker aísla los eventos fallidos para que el tráfico nuevo (el "happy path") siga fluyendo inmediatamente.
- **Resguardo Crudo (Auditor):** Cada payload JSON original que ingresa se guarda en logs `.jsonl` rotativos antes del procesamiento, permitiendo auditoría y recuperación ante desastres sin pérdida de datos.

### 4. Monitoreo Táctico y Dashboard en Tiempo Real
- **Frontend SSE:** Un Dashboard moderno, estéticamente enriquecido, impulsado por *Server-Sent Events*. Provee telemetría en vivo y trazabilidad sin saturar el servidor mediante técnicas de "Long Polling".
- **Filtros contra Outliers:** Matemática defensiva integrada. Si un evento se desconecta de la red y entra como "zombie" días después, su latencia queda aislada del cálculo promedio global mediante un umbral seguro de **300 segundos**, garantizando que los KPIs operativos no se contaminen.

### 5. Seguridad End-to-End
- Todo el entorno de monitoreo web y APIs visualizadoras están protegidas por **HTTP Basic Authentication**.
- Incorpora un **Inspector de APIs** interno para pruebas técnicas (Postman-like) con un riguroso escudo **Anti-SSRF**, el cual bloquea categóricamente las consultas a redes locales, loopbacks o infraestructuras cloud.

---

## 🛠️ Stack Tecnológico
- **Lenguaje:** Python 3.10+
- **Framework Web:** FastAPI / Uvicorn (ASGI)
- **Base de Datos:** SQLite3 (Sharded) + SQLAlchemy ORM
- **Validación:** Pydantic
- **Integración SOAP:** Zeep (validación de WSDL industrial)
- **Frontend:** HTML5, CSS3 Vanilla, JavaScript (SSE, Fetch API)

---

## ⚙️ Puesta en Marcha (Quick Start)

1. **Clonar el repositorio y preparar el entorno:**
   ```bash
   git clone <repo-url>
   cd Centro_de_Comando_APIs
   python -m venv venv
   # Activar entorno virtual (Windows)
   venv\Scripts\activate
   ```

2. **Instalar Dependencias Curadas:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configurar el entorno (`.env`):**
   Crea un archivo `.env` en la raíz copiando la estructura de `.env.example`:
   ```env
   # Credenciales Dashboard
   DASHBOARD_USER=admin
   DASHBOARD_PASS=tu_password_seguro

   # Configuración General
   WORKER_INTERVAL_SEC=1.0
   ```

4. **Levantar el Servidor:**
   ```bash
   python main.py
   ```
   El dashboard estará disponible en: `http://localhost:8000/dashboard`

---

## 📁 Estructura del Proyecto

```text
/app
 ├── /api
 │    └── /routers       # Endpoints HTTP: webhooks (schmitz, protrack), dashboard, inspector
 ├── /core               # Configuración global, logger, auditoría, base de datos
 ├── /providers          # Lógica específica de cada proveedor (Mappers, Pullers)
 ├── /schemas            # Pydantic (Modelo Canónico)
 ├── /services           # SOAP Client (Zeep) hacia RC
 ├── /templates          # HTML/CSS del Dashboard
 └── /worker             # Background task dispatcher (processor.py)
/db                      # (Auto-generado) Bases de datos SQLite fragmentadas
/audit                   # (Auto-generado) Logs en bruto JSONL
main.py                  # Entrypoint de Uvicorn/FastAPI
requirements.txt         # Dependencias Python
```
