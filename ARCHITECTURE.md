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
        B -->|2. Adaptación| E[Mapper: app/providers/...]
        E -->|Mapea JSON a Canonical Model| F[Validador Pydantic: app/schemas/canonical.py]
        F -->|3. Persistencia Local| G[(db/schmitz_test.db)]
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

- La función `get_session(provider, env)` crea o devuelve una conexión SQLAlchemy enrutada físicamente a `./db/{provider}_{env}.db`.
- **Beneficio:** Las transacciones ocurren en paralelo a nivel sistema operativo. La saturación de un proveedor jamás degrada el throughput de escritura de otro.
- **Configuración Global:** La base de datos `system_config.db` (aislada) se utiliza exclusivamente para almacenar tokens persistentes, diccionarios dinámicos y el historial de estadísticas diarias consolidadas (`daily_stats`).

---

## 3. Concurrencia y el Worker Asíncrono

El cerebro de despacho (`app/worker/processor.py`) corre en un hilo de fondo atado al ciclo de vida (`lifespan`) de FastAPI.

### Despertador Thread-Safe (`asyncio.Event`)
En lugar de un clásico bucle `while True: sleep(5)` ciego, el Worker duerme hasta que ocurra uno de dos eventos:
1. Pasa el `WORKER_INTERVAL_SEC` (Fallback natural).
2. El endpoint de inyección (Webhook) ejecuta `trigger_worker()`, que activa la bandera del evento de asyncio. El worker despierta **en milisegundos** tras el ingreso de un payload.

### Hilos de Red
Al despertar, lee lotes de la DB. El envío hacia el WSDL de Recurso Confiable (operación de red bloqueante) se orquesta envolviendo el cliente SOAP (`Zeep`) dentro de un `ThreadPoolExecutor` nativo de Python (`run_in_executor`). Esto impide que un timeout externo bloquee el hilo principal de ASGI/FastAPI.

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

### Intercambio Activo (Filtros)
Al aplicar un filtro en la UI (ej. "Mostrar solo PROTRACK"), la limitación de SSE (broadcast global masivo) se vuelve un impedimento. El Frontend interrumpe la escucha SSE del grid y dispara solicitudes `GET /api/stats?provider_filter=PROTRACK` directo al backend para garantizar la extracción exacta.

---

## 6. Seguridad y Anti-SSRF

Toda interfaz orientada al operador (Dashboard y visualizador DB) está blindada bajo **HTTP Basic Auth** (`verify_dashboard_auth`). 
Adicionalmente, el proyecto incorpora la herramienta `/inspector`, un proxy inverso (Mini-Postman) para facilitar el debug desde el propio servidor. Para prevenir vulnerabilidades de Server-Side Request Forgery (SSRF) introducidas por esta herramienta de proxy, toda URL objetivo pasa por el helper `_is_safe_url()`, el cual resuelve DNS y bloquea cualquier resolución a los rangos IP:
- Loopback (`127.0.0.0/8`)
- Redes Privadas (`10.x.x.x`, `192.168.x.x`, `172.16.x.x/12`)
- AWS/GCP Meta-data (`169.254.169.254`)
