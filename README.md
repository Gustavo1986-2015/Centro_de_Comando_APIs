# Centro de Comando APIs | Assistcargo

Hub Telemático corporativo centralizado para la recepción, transformación y orquestación de eventos GPS provenientes de APIs de terceros (Push y Pull). El sistema procesa los datos hacia un Modelo Canónico estandarizado y los despacha de manera asíncrona hacia Recurso Confiable (RC), ofreciendo paralelamente un **Dashboard de monitoreo táctico en tiempo real**.

---

## 🚀 Arquitectura y Capacidades Clave

El sistema ha sido diseñado desde cero para soportar un alto throughput (miles de eventos por segundo) sin pérdida de rendimiento ni interbloqueos, empleando un diseño moderno, asíncrono y seguro.

### 1. Ingesta Híbrida y Procesamiento Asíncrono (Desacoplamiento)
- **El Patrón Traductor (Mappers):** La arquitectura aísla el conocimiento de cada proveedor. Se crea un único archivo `mapper.py` por proveedor que traduce su JSON propietario "crudo" al "Español" (el Modelo Canónico). Una vez traducido, el resto del sistema opera a ciegas de manera universal.
- **Motores PUSH (Webhooks - "Agujero Negro 202"):** Recepción pasiva de eventos blindada. Los routers actúan como un agujero negro fail-safe: absorben la data, la toleran (Pydantic `extra='ignore'`), encolan en milisegundos y responden HTTP 202 inmediatamente, descartando silenciosamente la basura ("Drop and Forget") para jamás bloquear al proveedor.
- **Motores PULL (Cron-Driven):** Tareas en segundo plano (vía `httpx` asíncrono) para orquestar consumos periódicos desde APIs externas.
- **Auto-Escalado y Protección de Ráfagas:** El *Worker* de despacho lee la cola agnóstica y empuja a Recurso Confiable. Para evitar saturar a RC tras una desconexión masiva (Queue Burst), implementa semáforos asíncronos (`asyncio.Semaphore`) que estrangulan y limitan la concurrencia máxima en paralelo.

### 2. Base de Datos Fragmentada (Sharding)
Para evitar el "Database Locked" característico de SQLite bajo estrés, se implementa **Sharding por Proveedor y Entorno**. Cada integración escribe exclusivamente en su propia subcarpeta y archivo físico (ej. `db/protrack/prod.db`, `db/schmitz/test.db`). Esto garantiza que picos de tráfico en un proveedor no afecten el rendimiento ni la latencia de otros proveedores, a la vez que mantiene el directorio raíz limpio.

### 3. Modelo Canónico y Resiliencia Extrema
- **Validación Estricta:** Todo dato entrante se filtra mediante `Pydantic` hacia el **Modelo Canónico** de Assistcargo.
- **Circuit Breaker y Timeouts:** Si un envío a RC falla (ej. timeout de red), el sistema absorbe el impacto. Zeep cuenta con un **timeout granular (5s conexión / 25s lectura)**. Si ocurren 5 fallos consecutivos, el "Circuit Breaker" corta el tráfico hacia RC (estado OPEN) para evitar congestión, hasta que la red se recupere.
- **Reintentos Inteligentes (Backoff Lineal/Exponencial):** Los eventos fallidos quedan retenidos y se reintentan progresivamente (Ej. +10s, +45s, +120s...). El Worker aísla los eventos fallidos para que el tráfico nuevo fluya inmediatamente.
- **Respaldo JSONL y Auto-Purga:** Todo payload se guarda en logs rotativos `.jsonl` y los eventos procesados se respaldan en disco agrupados mensualmente. Retención configurable + toggle procesados + purga manual. La gestión de logs se realiza desde el Dashboard y la auto-purga dinámica protege el espacio del servidor, operando la BD SQLite estrictamente como una RAM volátil. Los logs crudos son forenses y no pueden desactivarse.

### 4. Monitoreo Táctico y Dashboard en Tiempo Real
- **Frontend SSE:** Un Dashboard moderno, estéticamente enriquecido, impulsado por *Server-Sent Events*. Provee telemetría en vivo y trazabilidad sin saturar el servidor mediante técnicas de "Long Polling".
- **Buscador de Vehículos Únicos:** Un monitor forense interno que permite buscar patentes específicas con filtros de fecha y proveedor, y extraer o descargar en formato JSON crudo todo el historial de eventos de un chasis particular en tiempo real.
- **Filtros contra Outliers:** Matemática defensiva integrada. Si un evento se desconecta de la red y entra como "zombie" días después, su latencia queda aislada del cálculo promedio global mediante un umbral seguro de **300 segundos**, garantizando que los KPIs operativos no se contaminen.

### 5. Seguridad End-to-End
- Todo el entorno de monitoreo web y APIs visualizadoras están protegidas por **HTTP Basic Authentication**.
- Incorpora un **Inspector de APIs** interno para pruebas técnicas (Postman-like) con un riguroso escudo **Anti-SSRF**, el cual:
  - Bloquea categóricamente consultas a redes locales, loopbacks, link-local (incluye metadata de cloud `169.254.169.254`) y rangos reservados.
  - **Mitigación de DNS rebinding:** resuelve el hostname una sola vez, valida la IP y "pinnea" la conexión a esa IP específica (con Host header preservado), evitando que una segunda resolución DNS bypasse el escudo.
  - Verificación TLS configurable vía `INSPECTOR_ALLOW_INSECURE_TLS` (default False).
- **Data at Rest Segura:** Los tokens de sesión y credenciales cacheadas en disco duro (ej. Recurso Confiable) se persisten cifrados mediante **algoritmo simétrico AES-128 (Fernet)**. Esto mitiga vulnerabilidades críticas de escalamiento de privilegios por Local File Inclusion (LFI).

> ⚠️ **Caveat conocido — RC sobre HTTP:** Recurso Confiable (RC) actualmente solo expone su endpoint SOAP sobre HTTP (no HTTPS). Esto significa que las credenciales SOAP viajan en claro por la red hacia RC. Esta es una limitación del proveedor que no se puede resolver del lado del cliente. Mitigación recomendada: asegurar que el tráfico hacia RC viaje por un canal cifrado a nivel de red (VPN, túnel IPsec, o proxy que termine TLS hacia RC). Rotar credenciales periódicamente.

### 6. Autoconfiguración y Observabilidad Avanzada
- **Migraciones Idempotentes:** Despliegue sin scripts. En el arranque, el motor DDL intenta crear estructuras en crudo; los errores de duplicidad se absorben intencionalmente y certifican el éxito, asegurando portabilidad inmediata.
- **Hot-Reload Logging:** Nivel de consola ajustable en caliente (DEBUG a INFO) editando el archivo `.env`. El sistema recarga los loggers silenciosamente en fondo sin reiniciar la API, vital para depurar en servidores productivos sin dropear conexiones HTTP.

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

---

## 7. Gestión de Logs desde el Dashboard
El sistema incorpora controles completos de gestión de logs accesibles directamente desde la interfaz de usuario (Dashboard):
- **Retención Configurable:** Posibilidad de ajustar la vida útil de los logs crudos (7 a 90 días) y logs procesados (7 a 30 días).
- **Toggle de Procesados:** Permite desactivar los respaldos en disco de eventos ya procesados. *(Nota: Los logs crudos de ingesta son obligatorios por motivos forenses y no pueden apagarse).*
- **Purga Manual de Emergencia:** Incluye una herramienta protegida por estrictos guardrails (verificación de contraseña, confirmación escrita "PURGAR" y mínimo 7 días de retención obligatoria) para liberar espacio en disco de forma segura e inmediata.
