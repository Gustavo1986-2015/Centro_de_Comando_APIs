# Centro de Comando APIs - Preguntas Frecuentes (FAQ)

Bienvenido a la documentación del **Centro de Comando APIs de Assistcargo**. Este documento abarca tanto la perspectiva operativa (para usuarios de monitoreo) como la técnica (para ingenieros y analistas de sistemas).

---

## 1. Visión General y Operativa (Nivel Usuario)

### ¿Qué es el Centro de Comando APIs?
Es una plataforma de monitoreo en tiempo real que consolida y visualiza todo el tráfico de datos (telemetría GPS, sensores, alarmas) que ingresa desde las integraciones de los proveedores de hardware (ej. Protrack, Schmitz) hacia nuestro sistema central de procesamiento (Radio Comando).

### ¿Qué significan los estados de los eventos?
- **En Cola (Pendientes):** Eventos recién recibidos, actualmente procesándose en la memoria de nuestro Hub.
- **Enviados (Hoy):** Eventos despachados exitosamente al destino final (HTTP 200 Ok).
- **Fallidos (Hoy):** Eventos descartados luego de agotar la política de reintentos.
- **En Reintento:** Eventos que fallaron inicialmente por un timeout temporal y están en cola de retransmisión.

### ¿Cómo se calculan y qué miden las latencias?
- **Latencia Hub AC (Assistcargo):** El tiempo "interno" de procesamiento. Desde el milisegundo en que recibimos el webhook del proveedor hasta el milisegundo en que iniciamos el envío a destino. Un valor óptimo es ~1.0s (por lógica de Micro-Batching).
- **Latencia RC (Radio Comando):** El tiempo externo de respuesta. Cuánto tarda el servidor destino en devolver un código 200 tras recibir nuestro payload.

---

## 2. Arquitectura y Stack Tecnológico (Nivel Ingeniería)

### ¿Cuál es el Stack Tecnológico Principal?
- **Backend:** Python 3.10+, FastAPI, Uvicorn (ASGI).
- **Base de Datos:** SQLite3 con SQLAlchemy ORM.
- **Frontend:** Vanilla JavaScript, HTML5, CSS3 puro, Server-Sent Events (SSE).
- **Entorno:** Despliegue en Windows Server / Linux vía entornos virtuales aislados.

### ¿Cómo funciona la actualización en Tiempo Real sin saturar el servidor?
El sistema utiliza un patrón de **Server-Sent Events (SSE)**. 
Un único hilo global (`broadcast_loop`) consulta la base de datos cada 2 segundos, empaqueta las métricas calculadas y el top 200 de los últimos eventos, y empuja (PUSH) este payload JSON unificado a todos los clientes web conectados. Esto evita que cada cliente golpee la base de datos de manera independiente (lo que ocurriría con *long-polling*).

### ¿Por qué se usa SQLite y por qué hay múltiples archivos `.db`?
Para evitar cuellos de botella por concurrencia y bloqueos (*locks*) propios de las bases de datos monolíticas o del SQLite de un solo archivo, se aplica una arquitectura de **Sharding por Proveedor**. 
Cada integración escribe exclusivamente en su propio archivo físico (ej. `protrack_prod.db`, `schmitz_test.db`). Esto permite escalar la recepción a miles de eventos por segundo (EPS) en paralelo sin que un proveedor sature o frene al resto.

---

## 3. Lógica de Métricas y Seguridad (Nivel Analista de Sistemas)

### ¿Cómo se tratan las anomalías de tiempo (Outliers)?
Al integrar proveedores de todo el mundo, a menudo las estampas de tiempo (`timestamp` original del GPS) llegan con desfases de zona horaria o de sincronización de satélite.
- **Valores Negativos:** El sistema aplica filtros `MAX(0.0, timestamp_dif)` a nivel de query SQL para impedir que latencias negativas rompan el promedio.
- **Eventos Zombie (Outliers):** Si un evento quedó encolado por una desconexión y se envía horas más tarde, un filtro algorítmico excluye cualquier evento cuya latencia de Hub sea mayor a **300 segundos** de los promedios matemáticos, evitando la contaminación del indicador de performance real.

### ¿Cómo está estructurada la Seguridad Perimetral e Interna?
1. **Autenticación (Dashboard):** Protegido por HTTP Basic Auth inyectado vía dependencias de FastAPI (leyendo de `.env` para evitar credenciales hardcodeadas).
2. **Endpoints Internos (`/api/*`):** Funciones analíticas y visualizadores de bases de datos (`db-viewer`) heredan el guard de Basic Auth, evitando fugas de información.
3. **Inspector Anti-SSRF:** La herramienta embebida tipo *Postman* (`/inspector/*`) incluye un control riguroso de IP Address. Antes de despachar un request en nombre del servidor, resuelve el DNS y bloquea cualquier intento de alcance a IPs privadas (`10.0.0.0/8`, `192.168.0.0/16`), Loopbacks o Metadata Cloud (`169.254.169.254`).

### ¿Qué sucede con el almacenamiento a largo plazo?
El diseño del sistema prioriza la latencia táctica sobre el almacenamiento masivo ("Data Lake").
Los datos granulares (webhooks crudos) se depuran continuamente mediante una tarea de purga automática basada en antigüedad y volumen. Sin embargo, antes de purgarse, los KPIs agregados (volumetría total y promedios) son consolidados diariamente en una base de datos central (`system_config.db` -> tabla `daily_stats`) para alimentar la pestaña de *Historial y Gráficos*.
