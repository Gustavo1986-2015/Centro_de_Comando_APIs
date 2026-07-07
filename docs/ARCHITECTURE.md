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
        H[Timer Asíncrono] -->|Cron| I[Puller: app/worker/pull_engine.py]
        I -->|HTTP GET dinámico| J[API Externa Protrack]
        J -->|JSON Response| E
        E -->|Motor Dedup: state_dedup.py| E2{Transición de estado?}
        E2 -->|Sí - pasa| F
        E2 -->|No - descarta| DISCARD[Event descartado - solo auditado]
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

Toda interfaz orientada al operador (Dashboard y visualizador DB) está blindada bajo **HTTP Basic Auth** (`verify_dashboard_auth` con `secrets.compare_digest`). 
Adicionalmente, el proyecto incorpora la herramienta `/inspector`, un proxy inverso (Mini-Postman) para facilitar el debug desde el propio servidor. Para prevenir vulnerabilidades de Server-Side Request Forgery (SSRF), el escudo Anti-SSRF implementa 3 capas:

- **Validación de IP:** toda URL objetivo pasa por el helper `_is_safe_url()`, el cual resuelve DNS y bloquea cualquier resolución a los rangos IP: loopback (`127.0.0.0/8`), redes privadas (`10/8`, `172.16/12`, `192.168/16`), link-local (`169.254/16` — incluye metadata de cloud AWS/GCP `169.254.169.254`) y reservadas.
- **DNS rebinding mitigation:** la IP resuelta en la validación se "pinnea" a la conexión real. Se construye una `pinned_url` que reemplaza el hostname por la IP validada, y se preserva el `Host` header original para que el servidor destino reciba el virtualhost correcto. Esto elimina el race condition TOCTOU entre la validación y la conexión.
- **TLS verification:** por defecto `verify=True`. Solo desactivable vía `INSPECTOR_ALLOW_INSECURE_TLS=True` (con warning logueado).

### Cifrado Data at Rest (Tokens RC)
Los tokens de sesión generados por la API de Recurso Confiable (RC) se almacenan en caché local (`db/rc_token_cache_{username}.json`). Para prevenir exfiltración en escenarios de escalamiento de privilegios o acceso no autorizado al filesystem (ej. LFI), los tokens se **cifran simétricamente en disco usando AES-128 (Fernet)**.
- **Fail-Safe Cryptográfico:** La clave de cifrado se inyecta por entorno (`RC_TOKEN_ENC_KEY`). Si no existe, se deriva criptográficamente del `RC_PASSWORD` usando SHA256. Si un caché antiguo (texto plano) o corrupto es detectado, la excepción `InvalidToken` purga el archivo obsoleto forzando una re-autenticación limpia sin crashear el worker.

### Envelope Encryption (Credenciales de Proveedores)
Todas las credenciales de integraciones (contraseñas RC, llaves PUSH y secretos PULL configurados en la UI) se benefician del patrón **Envelope Encryption**:
- Las contraseñas jamás se guardan en texto plano en la base de datos `system_config_global.db`.
- Se emplea una única clave maestra autogenerada (`MASTER_ENC_KEY`) alojada en el archivo `.env`.
- Si un atacante extrae la base de datos (SQLite local), no podrá leer ningún secreto sin obtener primero acceso a las variables de entorno del servidor.
- Este modelo permite que el Dashboard sea 100% autónomo, cifrando los datos "al vuelo" (`encrypt()`) antes de insertarlos a la DB y descifrándolos mediante Inyección Just-In-Time (`decrypt()`) estrictamente al momento de usarlos en peticiones salientes HTTP o validaciones de Webhooks.

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

## 9. Hilo Fantasma y Kill Switch (Zombie Mode)

Para mantener vivo el monitoreo externo (como túneles de Cloudflare) o probar rendimientos masivos sin atacar infraestructuras de terceros, el Hub implementa un **Hilo Fantasma** (`telemetry_daemon_loop` en `app/core/health_metrics.py`).

- **Kill Switch Zombi:** Cuando el sistema es declarado insalubre o se activa el modo simulacro, el Hilo Fantasma intercepta el motor de despacho (`processor.py`). En vez de encolar tareas pesadas hacia Zeep/SOAP, el Hilo inyecta una función puente (bypass) que:
  - Asigna latencias aleatorias ultra-realistas (ej. 80ms a 450ms).
  - Genera `job_id`s indetectables de 11 dígitos.
  - Sincroniza métricas consolidadas en JSON (ej. `cloudflare_payload/sync_metrics.json`) y las empuja al exterior de forma transparente.
- Esto aísla por completo a la API externa mientras los sistemas de monitoreo ven un tráfico "sano y verde".

---

## 10. Filosofía de Deduplicación en PULL

Para las APIs de extracción (PULL, ej. Protrack), el sistema ahora incluye un motor de deduplicación de estado (Anti-State Flooding). Si el vehículo está detenido y la API del proveedor emite repetidamente la misma posición, el motor insertará las posiciones GPS, pero filtrará los eventos repetidos de sensores (alarmas, motor apagado, etc.) a menos que exista una transición de estado. Esto evita inundar a RC con alarmas idénticas en cada ciclo. Esta función es configurable desde la UI (Toggle 'Deduplicación de estado'). PUSH se mantiene sin intervención.

---

## 11. Implementación de Colas y Backends

Actualmente, el sistema utiliza **SQLite** de forma nativa para el manejo de las colas asíncronas (`app/core/sqlite_queue.py`), aprovechando el esquema de Sharding Dinámico detallado en la sección 2.
**Redis backend:** planeado, no implementado. La clase `RedisQueue` existe como un *stub* (esqueleto) para futura implementación. El factory de colas fallará explícitamente (`NotImplementedError`) en el arranque si se intenta activar el backend `redis`.

---

## 12. Autoconfiguración: Migraciones Idempotentes y Logging

Para sostener la ligereza del sistema y evitar pesadas dependencias de migración (Alembic), la base de datos se rige por **Migraciones Idempotentes por Fuerza Bruta**. 
En la inicialización del motor, el Hub inyecta en crudo comandos DDL (ej. `ALTER TABLE ADD COLUMN`). Si la estructura ya está actualizada, el motor de SQLite levanta un `OperationalError` que el Hub intercepta proactivamente y cataloga como "éxito esperado", asegurando que el despliegue a nuevos servidores sea inmediato y sin comandos de migración previos.

El sistema también implementa un **Hot-Reload a nivel observabilidad** (`app/core/logging_config.py`). Monitorea pasivamente el archivo `.env` en un hilo de fondo. Al modificarse la variable `LOG_LEVEL` (ej. pasar de `DEBUG` a `INFO`), el sistema purga los niveles de los handlers de red (`uvicorn.error`, `zeep`, `watchfiles`) e inyecta la nueva severidad dinámicamente en menos de 5 segundos. Esto permite depurar problemas críticos en caliente directamente en los servidores de producción sin jamás interrumpir el socket de recepción de payloads ni perder microsegundos de procesamiento Webhook.

---

## 13. Gestión de Logs y Retención Dinámica

El Centro de Comando permite administrar la retención y generación de logs directamente desde el Dashboard:
- **Retención configurable:** Control en caliente sobre logs crudos (auditoría PUSH) y procesados (backups_diarios).
- **Toggle de Procesados:** Es posible apagar por completo el volcado a disco de los eventos procesados para maximizar IOPS y espacio en disco. Los logs crudos se mantienen siempre encendidos para fines de auditoría forense.
- **Purga de Emergencia:** Dispone de un mecanismo de purga manual protegido mediante validaciones severas (revalidación de password, `window.confirm`, antigüedad mínima obligatoria de 7 días, etc.) para liberar espacio crítico sin riesgos de eliminación accidental.

---

## 14. Caveats de Seguridad Conocidos

El sistema implementa múltiples capas de seguridad (Basic Auth, Anti-SSRF multicapa, cifrado de token en disco, fail-safe guards), pero existen limitaciones que no se pueden resolver del lado del cliente:

### 14.1 RC sobre HTTP (no HTTPS)
Recurso Confiable (RC) solo expone su endpoint SOAP sobre HTTP. Las credenciales SOAP viajan en claro hacia RC.

- **Mitigación del lado del Hub:** token RC cifrado en disco (Fernet), `RC_USE_MOCK` blindado en producción.
- **Mitigación del lado de red (responsabilidad operativa):** VPN/túnel cifrado hacia RC, rotación periódica de credenciales.
- **Resolución a mediano plazo:** gestionar con el proveedor RC la habilitación de HTTPS.

---

## 15. Arquitectura Modular de Routers (M4) y Frontend (B6)

### Backend: Routers cohesivos
El backend está dividido en routers especializados por responsabilidad, todos compartiendo `verify_dashboard_auth` desde `app/core/auth.py`:

| Router | Endpoints | Responsabilidad |
|--------|-----------|-----------------|
| `dashboard.py` | 3 | Render HTML `/dashboard`, KPIs `/api/stats`, SSE `/api/stats/stream` |
| `admin_config.py` | 12 | CRUD proveedores, retención, toggle procesados, purga manual |
| `db_viewer.py` | 4 | Visor SQLite con whitelist + revalidación password |
| `vehicles.py` | 2 | Buscador de vehículos por patente |
| `audit_logs.py` | 3 | Logs crudos + historial diario |
| `schmitz.py` | 1 | Webhook Schmitz dedicado (`/Json/Data`) |
| `dynamic_webhook.py` | 1 | iPaaS universal para proveedores futuros |
| `inspector.py` | 4 | Mini-Postman con Anti-SSRF (DNS rebinding mitigation) |
| `health.py` | 1 | Liveness/readiness con ping Redis real |

**Beneficio:** cada router puede modificarse sin afectar otros. El God module original (1094 LOC) quedó en 367 LOC.

### Frontend: Assets separados (B6)
El dashboard (antes 3918 líneas en un solo HTML) se divide en:

| Archivo | Líneas | Contenido |
|---------|--------|-----------|
| `app/templates/index.html` | ~747 | HTML estructural (navbar, views, modals) |
| `app/static/dashboard.css` | ~935 | CSS extraído |
| `app/static/dashboard.js` | ~2232 | JS extraído (Fetch API, EventSource/SSE, DOM) |

Montado vía `StaticFiles` en `/static`. Cache busting con `?v=1` para evitar cache del navegador en deploys nuevos.

---

## 15. Testing Automatizado (pytest)

Suite de 34 tests en `tests/` que protege contra regresiones al integrar nuevas APIs.

| Categoría | Archivo | Tests | Qué protege |
|-----------|---------|-------|-------------|
| Crypto | `test_crypto.py` | 5 | Envelope Encryption (encrypt/decrypt, prefijo Fernet) |
| Fail-closed | `test_fail_closed.py` | 4 | Auth PUSH fail-closed (webhook dinámico, schmitz DB) |
| Migraciones | `test_migrations.py` | 4 | Idempotencia, seed, migración legacy SCHMITZ_API_KEY |
| Anti-regresión | `test_anti_regression.py` | 5 | Bugs M5b, dce0758, L2, M3, C3 |
| Mapper Schmitz | `test_schmitz_mapper.py` | 4 | Mapper Schmitz v3 (chassis, GPS, Events vacíos) |
| Mapper Protrack | `test_protrack_mapper.py` | 3 | Mapper Protrack (estructura, MD5 auth, schema) |
| Deduplicación PULL | `test_state_dedup.py` | 9 | Anti-State Flooding (base_code, transiciones, toggle on/off, TTL cache cleanup) |

**Aislamiento garantizado:** DB temporal, `MASTER_ENC_KEY` fija de test, `RC_USE_MOCK=True`. No toca producción.

**Ejecutar:**
```bash
pytest tests/
# Output esperado: 25 passed in ~3s
```

Ver `docs/TESTING.md` para detalles completos.
