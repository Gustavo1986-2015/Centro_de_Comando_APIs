# Arquitectura Interna y Mapa de Datos del Centro de Comando APIs

Este documento sirve como guía de ingeniería y mapa técnico del Hub Telemático corporativo de Assistcargo. Detalla el flujo de ejecución extremo a extremo, la arquitectura de bases de datos, y los mecanismos de seguridad y concurrencia.

---

## 1. Mapa de Flujo de Datos Extremo a Extremo (E2E)

El siguiente diagrama detalla cómo viaja la información desde que el camión reporta su telemetría hasta que es procesada por Assistcargo y despachada a Recurso Confiable (RC):

```mermaid
graph TD
    %% Bloque Ingesta Webhook (PUSH)
    subgraph 1a. Ingesta y Normalización (PUSH)
        A[Proveedor: Schmitz/Otros] -->|POST HTTP /provider/webhook| B[FastAPI Router]
        B -->|1. Resguardo Crudo| C[Auditor: app/core/auditor.py]
        C -->|Escribe logs diarios| D[(audit/schmitz_test.jsonl)]
        B -->|2. Desacoplamiento| E[Mapper: app/providers/...]
        E -->|Mapea JSON a Canonical Model| F[Validador Pydantic: app/schemas/canonical.py]
        F -->|3. Persistencia Local (Drop and Forget)| G[(db/schmitz/test.db)]
    end

    %% Bloque Ingesta (PULL)
    subgraph 1b. Ingesta Activa (PULL)
        H[Timer Asíncrono] -->|Cron| I[Puller: app/providers/protrack/puller.py]
        I -->|HTTP GET dinámico| J[API Externa Protrack]
        J -->|JSON Response| E
    end

    %% Bloque Despacho (WORKER)
    subgraph 2. Procesamiento y Despacho Asíncrono
        G -->|Lee pendientes (limit 200)| K[Worker: app/worker/processor.py]
        K -->|Lote de N eventos| L[ThreadPoolExecutor]
        L -->|Zeep SOAP Client| M[Recurso Confiable WSDL]
        M -->|OK (200)| N[Actualiza estado a 'sent']
        M -->|Fail/Timeout| O[Actualiza a 'failed' e incrementa retry_count]
        N --> G
        O --> G
    end

    %% Bloque Consumo Front (UI)
    subgraph 3. Dashboard Tiempo Real
        G -->|Consulta SQL periódica| P[Dashboard Router / SSE]
        P -->|Empuja JSON Top 200| Q[Browser UI]
    end
```

---

## 2. Bases de Datos: Sharding por Integración

El sistema requiere ingestar miles de eventos simultáneos. Si todas las peticiones escribieran sobre un único archivo SQLite (`main.db`), el sistema sufriría el error `database is locked`. Para resolver esto, el Hub implementa un esquema de **Sharding Dinámico**.

- La función `get_session(provider, env)` crea o devuelve una conexión SQLAlchemy enrutada físicamente a `./db/{provider}/{env}.db`.
- **Beneficio:** Las transacciones ocurren en paralelo a nivel sistema operativo. La saturación de un proveedor jamás degrada el throughput de escritura de otro.
- **Configuración Global:** La base de datos `system_config.db` (aislada) se utiliza exclusivamente para almacenar tokens persistentes, diccionarios dinámicos y el historial de estadísticas diarias consolidadas (`daily_stats`).

---

## 3. Concurrencia y el Worker Asíncrono

El cerebro de despacho (`app/worker/processor.py`) corre en un hilo de fondo atado al ciclo de vida (`lifespan`) de FastAPI.

### Despertador Thread-Safe (`asyncio.Event`)
En lugar de un clásico bucle `while True: sleep(5)` ciego, el Worker duerme hasta que ocurra uno de dos eventos:
1. Pasa el `WORKER_INTERVAL_SEC` (Fallback natural).
2. El endpoint de inyección (Webhook) ejecuta `trigger_worker()`, que activa la bandera del evento de asyncio. El worker despierta **en milisegundos** tras el ingreso de un payload.

### Hilos de Red y Semáforos (Protección de Ráfagas)
Al despertar, lee lotes de la DB. El envío hacia el WSDL de Recurso Confiable (operación de red bloqueante) se orquesta envolviendo el cliente SOAP (`Zeep`) dentro de un `ThreadPoolExecutor` nativo de Python (`run_in_executor`). 
Para evitar tumbar a Recurso Confiable durante una ráfaga masiva de eventos encolados (Queue Burst), se implementan Semáforos Asíncronos (`asyncio.Semaphore`) que estrangulan la salida, permitiendo únicamente un máximo predefinido de conexiones paralelas a RC por proveedor, manteniendo la salud del sistema intacta.

---

## 4. Política de Reintentos (Backoff Lineal)

Para garantizar la entrega en infraestructuras inestables, los eventos fallidos quedan en estado `pending` pero su `retry_count` se incrementa.

La query del Worker excluye eventos cuyo tiempo de "castigo" no se haya cumplido, bajo esta fórmula heurística:
- Intento 1: + 10 segundos
- Intento 2: + 45 segundos
- Intento 3: + 120 segundos
- Intento 4: + 300 segundos
- Intento 5: Abandono (queda en `failed` terminal).

Esto permite que la congestión de eventos "enfermos" no impida el rápido flujo de eventos "sanos" entrantes.

---

## 5. Diseño del Dashboard de Monitoreo

### Server-Sent Events (SSE)
Para la telemetría en tiempo real, el backend cuenta con un hilo asíncrono (`broadcast_loop`) que consolida las lecturas de todos los archivos `.db` en memoria y emite un flujo continuo `text/event-stream`.
El navegador del cliente recibe pasivamente este objeto JSON y repinta el DOM.

### Protección Matemática (Outliers)
Debido a la naturaleza asíncrona de los reportes GPS (ej. camión sale de zona de cobertura y envía lotes atrasados), las latencias podrían desvirtuarse. 
El backend incorpora mitigaciones a nivel query (`func.max(0.0, diff)`) y bloquea de las medias matemáticas cualquier latencia de Hub superior a **300 segundos**, garantizando que un evento "Zombie" no arruine el KPI diario del tablero.

### Intercambio Activo (Filtros y Búsquedas)
Al aplicar un filtro en la UI (ej. "Mostrar solo PROTRACK"), la limitación de SSE (broadcast global masivo) se vuelve un impedimento. El Frontend interrumpe la escucha SSE del grid y dispara solicitudes `GET /api/stats?provider_filter=PROTRACK` directo al backend para garantizar la extracción exacta.
Para búsquedas profundas e históricas (ej. Buscador de Vehículos Únicos), la UI invoca endpoints dedicados (`GET /api/vehicles/unique`) que escanean las subcarpetas de SQLite de manera bajo demanda, salvaguardando así el hilo principal de SSE.

---

## 6. Seguridad y Anti-SSRF

Toda interfaz orientada al operador (Dashboard y visualizador DB) está blindada bajo **HTTP Basic Auth** (`verify_dashboard_auth`). 
Adicionalmente, el proyecto incorpora la herramienta `/inspector`, un proxy inverso (Mini-Postman) para facilitar el debug desde el propio servidor. Para prevenir vulnerabilidades de Server-Side Request Forgery (SSRF) introducidas por esta herramienta de proxy, toda URL objetivo pasa por el helper `_is_safe_url()`, el cual resuelve DNS y bloquea cualquier resolución a los rangos IP:
- AWS/GCP Meta-data (`169.254.169.254`)

### Cifrado Data at Rest (Tokens RC)
Los tokens de sesión generados por la API de Recurso Confiable (RC) se almacenan en caché local (`db/rc_token_cache_{username}.json`). Para prevenir exfiltración en escenarios de escalamiento de privilegios o acceso no autorizado al filesystem (ej. LFI), los tokens se **cifran simétricamente en disco usando AES-128 (Fernet)**.
- **Fail-Safe Cryptográfico:** La clave de cifrado se inyecta por entorno (`RC_TOKEN_ENC_KEY`). Si no existe, se deriva criptográficamente del `RC_PASSWORD` usando SHA256. Si un caché antiguo (texto plano) o corrupto es detectado, la excepción `InvalidToken` purga el archivo obsoleto forzando una re-autenticación limpia sin crashear el worker.

---

## 7. Retención, Respaldos y Purga de Datos (Ciclo de Vida)

Para prevenir el colapso del almacenamiento y mantener la RAM optimizada, el sistema implementa una estricta política de retención:

1. **Ingesta Cruda (JSONL):** Todo payload entrante se guarda inmediatamente en texto plano, agregando líneas a `audit/{proveedor}/YYYY-MM/crudos_YYYY-MM-DD.jsonl`.
2. **Backups de Procesados:** Cuando los eventos finalizan su ciclo (enviados exitosamente o fallidos definitivamente), son extraídos de SQLite y resguardados en `db/backups_diarios/{proveedor}_{env}/YYYY-MM/procesados_YYYY-MM-DD.jsonl` usando cursores `.yield_per()` para evitar desbordar la memoria RAM durante purgas masivas.
3. **Purga Automática:** Retención configurable desde el Dashboard (7-90 días crudos, 7-30 días procesados). Existe la opción de una purga manual de emergencia protegida por guardrails (mínimo 7 días, requiere escribir "PURGAR" y revalidación de contraseña). Los logs crudos no pueden apagarse por propósitos forenses.
4. **SQLite Volátil:** Las bases `.db` se mantienen extremadamente livianas alojando únicamente el tráfico "en vuelo" (pendientes, reintentos y las últimas horas de enviados).

---

## 8. Circuit Breaker y Timeouts Granulares (Protección de Red)

El envío SOAP a Recurso Confiable (RC) es el eslabón más frágil por las fluctuaciones de red. Para aislar fallos externos:

- **Timeout Granular en Zeep:** El cliente `requests.Session` inyectado en Zeep tiene configurado un timeout dividido `(5, 25)`. Esto asegura que si RC rechaza la conexión TCP, fallamos rápido en 5 segundos. Si la acepta pero tarda en responder, esperamos un máximo de 25 segundos, evitando que el *Worker* de FastAPI se congele.
- **Patrón Circuit Breaker:** Se interceptan las fallas de conexión o timeouts de lectura. Si el sistema sufre **5 fallos de transporte consecutivos**, el Circuit Breaker pasa a estado **OPEN (Rojo)**, cortando la salida temporalmente (10 minutos) para permitir que el receptor respire y se recupere. Los eventos se marcan para reintento hasta que el circuito pase a **HALF_OPEN (Amarillo)** y finalmente se restablezca a **CLOSED (Verde)**. 

---

## 9. Filosofía de Deduplicación en PULL

Para las APIs de extracción (PULL, ej. Protrack), el sistema adopta una postura "Passthrough Directo". No se realiza deduplicación local basada en coordenadas idénticas. Si el vehículo está detenido y la API del proveedor emite repetidamente la misma posición, el motor insertará todos y cada uno de esos registros. La responsabilidad de filtrado estático se delega integralmente a la capa del cliente final (RC).

---

## 10. Implementación de Colas y Backends

Actualmente, el sistema utiliza **SQLite** de forma nativa para el manejo de las colas asíncronas (`app/core/sqlite_queue.py`), aprovechando el esquema de Sharding Dinámico detallado en la sección 2.
**Redis backend:** planeado, no implementado. La clase `RedisQueue` existe como un *stub* (esqueleto) para futura implementación. El factory de colas fallará explícitamente (`NotImplementedError`) en el arranque si se intenta activar el backend `redis`.

---

## 11. Autoconfiguración: Migraciones Idempotentes y Logging

Para sostener la ligereza del sistema y evitar pesadas dependencias de migración (Alembic), la base de datos se rige por **Migraciones Idempotentes por Fuerza Bruta**. 
En la inicialización del motor, el Hub inyecta en crudo comandos DDL (ej. `ALTER TABLE ADD COLUMN`). Si la estructura ya está actualizada, el motor de SQLite levanta un `OperationalError` que el Hub intercepta proactivamente y cataloga como "éxito esperado", asegurando que el despliegue a nuevos servidores sea inmediato y sin comandos de migración previos.

El sistema también implementa un **Hot-Reload a nivel observabilidad** (`app/core/logging_config.py`). Monitorea pasivamente el archivo `.env` en un hilo de fondo. Al modificarse la variable `LOG_LEVEL` (ej. pasar de `DEBUG` a `INFO`), el sistema purga los niveles de los handlers de red (`uvicorn.error`, `zeep`, `watchfiles`) e inyecta la nueva severidad dinámicamente en menos de 5 segundos. Esto permite depurar problemas críticos en caliente directamente en los servidores de producción sin jamás interrumpir el socket de recepción de payloads ni perder microsegundos de procesamiento Webhook.

---

## 12. Gestión de Logs y Retención Dinámica

El Centro de Comando permite administrar la retención y generación de logs directamente desde el Dashboard:
- **Retención configurable:** Control en caliente sobre logs crudos (auditoría PUSH) y procesados (backups_diarios).
- **Toggle de Procesados:** Es posible apagar por completo el volcado a disco de los eventos procesados para maximizar IOPS y espacio en disco. Los logs crudos se mantienen siempre encendidos para fines de auditoría forense.
- **Purga de Emergencia:** Dispone de un mecanismo de purga manual protegido mediante validaciones severas (revalidación de password, `window.confirm`, antigüedad mínima obligatoria de 7 días, etc.) para liberar espacio crítico sin riesgos de eliminación accidental.
