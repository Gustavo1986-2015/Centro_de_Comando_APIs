# Arquitectura Interna y Mapa de Datos del Centro de Comando APIs

Este documento sirve como guía de ingeniería y mapa técnico del Hub Telemático corporativo de Assistcargo. Detalla el flujo de ejecución extremo a extremo, la interacción entre scripts, la arquitectura de bases de datos aisladas y el comportamiento de la concurrencia y los reintentos.

---

## 1. Mapa de Flujo de Datos Extremo a Extremo (E2E)

El siguiente diagrama detalla cómo viaja la información desde que el camión reporta su telemetría hasta que es procesada por Assistcargo y despachada a Recurso Confiable (RC):

```mermaid
graph TD
    %% Bloque Ingesta Webhook (PUSH)
    subgraph 1. Ingesta y Normalización
        A[Proveedor: Schmitz/Otros] -->|POST HTTP /provider/webhook?env=test| B[FastAPI: app/api/routers/schmitz.py]
        B -->|1. Resguardo Crudo| C[Auditor: app/core/auditor.py]
        C -->|Escribe logs diarios| D[(audit/schmitz_test/schmitz_test.jsonl)]
        B -->|2. Adaptación| E[Mapper: app/providers/schmitz/mapper.py]
        E -->|Mapea JSON a Canonical Model| F[Validador Pydantic: app/schemas/canonical.py]
        F -->|3. Persistencia Local| G[(db/schmitz_test.db)]
        B -->|Retorna HTTP 202 Accepted| A
    end

    %% Bloque Worker de Despacho (Asíncrono)
    subgraph 2. Procesamiento y Despacho Asíncrono (Worker)
        H[Worker Core: app/worker/processor.py] -->|1. Consulta APIs Activas| I[(db/system_config_global.db)]
        H -->|2. Lee eventos 'pending'| G
        H -->|3. Filtra con Backoff in-memory| J[RETRIES_CACHE]
        H -->|4. Agrupa en sub-lotes de 50| K{soap_tasks}
        K -->|5. Gather paralelo| L[Client SOAP: app/services/rc_soap.py]
        L -->|Caché Token| M[(db/rc_token_cache.json)]
        L -->|Llamada SOAP: send_events_batch| N[Web Service de Recurso Confiable]
        N -->|Retorna JobID / CGI:UNKNOWN_TOKEN| L
        L -->|Retorna Éxito / Error| H
        H -->|6. Actualiza status y job_id| G
        H -->|7. Consolida totales del día| O[update_daily_stats]
        O -->|Escribe DailyStat| I
    end

    %% Bloque Dashboard (Visualización)
    subgraph 3. Visualización y Control (Dashboard)
        P[API Dashboard: app/api/routers/dashboard.py] -->|Lee Config y DailyStats| I
        P -->|Lee últimos 200 eventos globales| G
        P -->|Calcula latencias de red y transmisión| P
        P -->|Inyecta RETRIES_CACHE| P
        P -->|Retorna JSON de estadísticas| Q[Frontend UI: app/templates/index.html]
    end
```

---

## 2. Mapa detallado de Scripts y Dependencias (Qué impacta en qué)

A continuación se detalla la matriz de impacto y el rol de cada script en el sistema:

| Script / Componente | Frecuencia / Gatillo | Entrada | Salida / Impacto | Rol Principal |
| :--- | :--- | :--- | :--- | :--- |
| **`main.py`** | Al arrancar la aplicación | Ninguna | Inicializa FastAPI y crea la tarea del Worker en background | Punto de entrada del Hub. Registra todos los routers del sistema. |
| **`app/api/routers/schmitz.py`** | Evento PUSH del proveedor | Payload JSON de Schmitz | Escribe en Logs de Auditoría y guarda el evento normalizado en `schmitz_{env}.db` | Webhook receptor de Schmitz. Realiza la autenticación, auditoría y encolamiento inicial. |
| **`app/providers/schmitz/mapper.py`** | Llamado por `schmitz.py` | JSON crudo de Schmitz | Modelo de datos `RCCanonicalModel` (Pydantic) | Adapta, parsea a UTC 0 y normaliza la telemetría (ej. limpia coordenadas y fuerza velocidad nula a `0.0`). |
| **`app/core/auditor.py`** | Llamado por routers de webhooks | Payload JSON original | Archivos diarios `.jsonl` bajo `audit/{provider}_{env}/` | Caja negra. Asegura el resguardo permanente de la información cruda antes de cualquier transformación. |
| **`app/worker/processor.py`** | En ejecución 24/7 (Loop asíncrono) | Parámetros de `system_config_global.db` | Consume eventos de las DBs de proveedores, los envía a RC y escribe estadísticas de éxito/falla | Core del despacho. Orquesta sub-workers independientes, concurrencia por sub-lotes, reintentos con backoff y purga. |
| **`app/services/rc_soap.py`** | Llamado por el Worker | Objetos de datos `RCCanonicalModel` | Construye el XML SOAP, interactúa con el WSDL de RC y gestiona la caché de tokens | Integrador SOAP. Controla la autenticación persistente y re-autenticación automática si expira el token. |
| **`app/api/routers/dashboard.py`** | Consulta del Frontend (cada 2 seg) | Datos de bases de datos globales e individuales | Payload JSON formateado con métricas y lista de eventos | API de control. consolida estadísticas, calcula desfase satelital y tiempo de cola en el Hub. |
| **`app/templates/index.html`** | Cargado en navegador por operador | Respuestas JSON de `/api/stats` e `/api/config` | Renderiza grillas en caliente, temporizadores de backoff e histórico consolidado | Consola de visualización. Provee filtros interactivos y el simulador de webhooks. |

---

## 3. Arquitectura de Base de Datos y Aislamiento (`.db`)

El sistema implementa el **Paradigma de Bases de Datos Aisladas** para prevenir cuellos de botella en SQLite, optimizar bloqueos de escritura y garantizar aislamiento físico total entre entornos (`TEST` y `PROD`).

### Estructura de Archivos en la carpeta `db/`
```text
db/
├── system_config_global.db   <-- Base de datos Maestra del Sistema
├── schmitz_prod.db           <-- Eventos productivos de Schmitz
├── schmitz_test.db           <-- Eventos de prueba del simulador de Schmitz
└── rc_token_cache.json       <-- Caché del token SOAP (archivo JSON persistente)
```

### 1. La Base de Datos Maestra (`system_config_global.db`)
Contiene los esquemas globales y la parametrización de comportamiento de las APIs:
* **Tabla `provider_configs` (Modelo `ProviderConfig`):**
  * `provider_name` (Ej. 'schmitz'): Identifica la API.
  * `env` (test/prod): Entorno de ejecución.
  * `is_active` (boolean): Toggle switch para detener/iniciar el sub-worker en caliente desde el UI.
  * `rc_user` / `rc_password`: Credenciales SOAP específicas de este canal.
  * `run_interval_sec`: Intervalo del ciclo del worker (ej. cada 5 segundos).
  * `purge_interval_min`: Intervalo de purga automática (ej. borrar procesados de más de 3 horas).
* **Tabla `daily_stats` (Modelo `DailyStat`):**
  * `date` (date): Día calendario.
  * `provider` (string): Nombre de la API.
  * `env` (string): Entorno.
  * `sent_count` / `failed_count` (integers): Histórico permanente diario.
  * `avg_transmission_latency_sec` (float): Promedio de latencia de transmisión (satelital/red del AVL a nuestro Hub) del día.
  * `avg_hub_latency_sec` (float): Promedio de latencia de procesamiento interno y cola del Hub de Assistcargo del día.
  * `avg_rc_latency_sec` (float): Promedio de latencia de red SOAP (tiempo de respuesta de Recurso Confiable) del día.

### 2. Bases de Datos de Proveedores (Ej. `schmitz_prod.db`, `schmitz_test.db`)
Contienen una única tabla central optimizada para indexación y consumo rápido:
* **Tabla `normalized_rc_events` (Modelo `NormalizedRCEvent`):**
  * `id` (Clave primaria indexada).
  * `status` (indexada: `pending`, `sent`, `failed`).
  * `raw_data` (Text): Payload crudo JSON original (para trazabilidad/auditoría rápida).
  * `rc_response` (Text): Respuesta XML o mensaje de excepción de red retornado por RC.
  * `job_id` (indexada): Identificador único o acuse de recibo de RC.
  * `rc_latency_sec` (Float): Tiempo exacto de red (en segundos) que demoró la llamada SOAP a Recurso Confiable para este evento.
  * **18 Columnas Normalizadas:** Campos del modelo canónico (`chassis_number`, `latitude`, `speed`, `date`, `ignition`, etc.) validados por Pydantic.
  * `created_at` / `updated_at` (DateTime): Auditoría de tiempos del Hub.

---

## 4. Lógica de Concurrencia de Red y Transaccionalidad de SQLite

Dado que SQLite no soporta múltiples transacciones de escritura simultáneas (bloqueo por `database is locked`), la arquitectura separa de forma limpia la **ejecución de red** de la **ejecución de base de datos**:

1. **Lectura e Ignorado:** El worker obtiene los eventos `pending` y descarta los que están esperando backoff en memoria (`RETRIES_CACHE`).
2. **Particionado Asíncrono:** Agrupa los pendientes en bloques de máximo 50 eventos.
3. **Paralelización de Red SOAP:** Invoca `asyncio.gather(*tasks)` disparando las peticiones SOAP en paralelo contra RC. Esto reduce la latencia de red al máximo, permitiendo procesar cientos de transmisiones simultáneas.
4. **Escritura Atómica:** Una vez que todas las respuestas de red regresan al script, se actualizan los estados y se ejecuta una **única transacción estructurada** (`db.commit()`) por base de datos, garantizando consistencia, eliminando bloqueos de base de datos y completando la operación en milisegundos.

---

## 5. El Motor de Reintentos Asíncronos con Backoff

Para evitar la saturación de los servidores de RC y evitar bucles infinitos por credenciales desactualizadas o caídas prolongadas de red, el Hub implementa un motor inteligente en memoria:

```text
               [ Evento falla en despacho SOAP ]
                               │
               Verifica contador de reintentos
              (almacenado en RETRIES_CACHE)
                               │
                     ┌─────────┴─────────┐
                     ▼                   ▼
                 Intentos < 4        Intentos >= 4
                     │                   │
      Calcula Backoff Lineal:            │
      1°: +10s | 2°: +45s                │
      3°: +120s | 4°: +300s              ▼
                     │            Marca status = 'failed'
                     ▼            Elimina de RETRIES_CACHE
       Actualiza 'next_retry_at'  (Fallo Definitivo en UI)
       Estado queda 'pending'
       (Badge Amarillo en UI)
```
* **Comportamiento en Cola:** El sub-worker de la API continúa ejecutándose normalmente cada $N$ segundos procesando paquetes de telemetría nuevos, omitiendo de forma inteligente cualquier evento en cola cuya marca de tiempo actual sea inferior a `next_retry_at`. Esto asegura que el canal de datos permanezca siempre operativo.
