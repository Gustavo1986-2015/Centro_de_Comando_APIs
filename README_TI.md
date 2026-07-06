# Resumen del Proyecto: Centro de Comando (Hub Telemático)

Preparé un resumen de la arquitectura y capacidades técnicas del nuevo Centro de Comando que armamos y, sobre todo, cómo nos aseguramos de que sea seguro, escalable y resiliente ante miles de peticiones.

## ¿Qué hace exactamente este sistema?

Básicamente, funciona como un **"traductor y semáforo"** entre todos los proveedores de GPS que tenemos (como Protrack, Schmitz, etc.) y nuestro servidor central (Recurso Confiable). 

Como cada proveedor manda la información en su propio idioma y a su propio ritmo, este sistema se encarga de:
1. **Recibir todo el caos:** Atrapa la información de los camiones, ya sea porque el proveedor nos la "empuja" (Webhooks) o porque nosotros vamos a buscarla de forma programada (PULL).
2. **Traducirlo a un formato único:** Convierte todo a un estándar que nosotros entendemos (Modelo Canónico) mediante *Mappers* aislados por proveedor.
3. **Enviarlo de forma ordenada y segura:** En lugar de bombardear a Recurso Confiable con miles de datos por segundo y correr el riesgo de tirarlo, el sistema forma una "fila" (cola de procesamiento) y va entregando los datos de a poco y de forma constante.

---

## 1. Recepción de Datos y Prevención de Cuellos de Botella (SQLite Sharding)

Sabiendo que el sistema debe procesar miles de eventos por segundo, el cuello de botella tradicional siempre es la base de datos (el famoso `database is locked` de SQLite). Para evitarlo, implementamos:

- **Sharding (Bases de datos fragmentadas):** En lugar de que todos los proveedores escriban en un archivo central gigante, el sistema le asigna un archivo de base de datos SQLite exclusivo a cada proveedor y entorno (ej. `db/protrack/prod.db`). 
- **Beneficio:** Esto permite escrituras paralelas reales a nivel de disco. Si un proveedor inyecta un pico masivo de 50.000 eventos, solo estresará su propio archivo de base de datos, mientras que el resto de los proveedores seguirán traficando información a velocidad de luz y sin latencia.
- **Agujero Negro (Drop and Forget):** Las rutas de recepción pasiva (Webhooks) absorben el JSON, lo escriben en la base de datos fragmentada en microsegundos, y responden un `HTTP 202 Accepted` de inmediato. Cualquier validación compleja ocurre en segundo plano, para jamás bloquear ni hacer esperar al servidor del proveedor.

---

## 2. Hilos, Workers y Semáforos (Procesamiento Asíncrono)

Para el despacho de datos hacia Recurso Confiable (RC) sin saturar su infraestructura externa, la aplicación emplea una arquitectura de subprocesos y control de concurrencia avanzado:

- **El Worker Asíncrono:** Detrás de escena corre un proceso paralelo (*background worker*) que está leyendo constantemente las bases de datos SQLite. Al despertar, arma lotes (micro-batching) y despacha los datos a RC.
- **Circuit Breaker y Backoff:** Si RC se cae o hay un corte de internet temporal, el sistema no colapsa ni descarta los eventos. Los retiene en memoria (pending) y aplica un castigo progresivo (se reintentan en 10s, 45s, 2min, 5min). Esto aísla el tráfico "enfermo" para que el tráfico nuevo fluya sin interrupciones.
- **Semáforos de Concurrencia:** Para evitar un ataque DDoS accidental hacia RC (por ejemplo, enviar de golpe los 50.000 eventos retenidos al volver internet), el Worker utiliza un Semáforo (`asyncio.Semaphore`). Esto asegura que, por más saturada que esté la cola, solo saldrán un máximo de conexiones simultáneas permitidas (ej. 4 a la vez), protegiendo la salud de Recurso Confiable.

---

## 3. Seguridad de Extremo a Extremo (Hardening)

Manejamos datos sensibles, contraseñas de terceros y estamos expuestos a la red. El sistema fue diseñado con estrictas defensas:

- **Rechazo por Defecto (Webhooks):** Si un proveedor intenta enviar información y no posee la llave (API Key) exacta configurada por nosotros, el sistema rechaza la conexión con `401 Unauthorized` inmediatamente (Fail-closed).
- **Cifrado de Sobre (Envelope Encryption):** Las contraseñas de RC y llaves de proveedores no se guardan como texto plano en las bases de datos. Se almacenan cifradas (`AES-128`). La "llave maestra" para leerlas se aloja exclusivamente en las variables de entorno del servidor. Si alguien roba la base de datos, solo obtendrá texto inútil y cifrado.
- **Escudo Anti-SSRF:** El sistema incluye un inspector interno (similar a Postman). Para que nadie lo utilice como túnel para atacar otras partes de nuestra red interna (Server-Side Request Forgery), cuenta con un escudo que bloquea por defecto toda petición a IPs locales, redes privadas y previene ataques de envenenamiento DNS (*DNS Rebinding Mitigation*).

---

## 4. Auditoría y Trazabilidad (Caja Negra Forense)

A nivel de archivos, implementamos una regla de oro: **todo lo que entra, se anota**. 
Antes de que el sistema analice, traduzca o filtre cualquier coordenada de GPS, una copia exacta del dato crudo original (el JSON intacto) se guarda en un archivo `audit/*.jsonl` en el disco duro. 

Si el día de mañana hay un problema legal, un error rarísimo, o un proveedor insiste en que sí nos envió un dato, podemos ir a esta "Caja Negra" inalterable y extraer la verdad absoluta de lo que pisó nuestros servidores. Para que el disco duro no se llene, el propio sistema posee políticas de auto-purga y retención configurables que borran los archivos muy antiguos.

---

Cualquier duda técnica más profunda de la arquitectura me avisan y lo revisamos en código, pero el flujo está contenido, paralelizado y totalmente blindado.

📋 Listado completo de endpoints (31 endpoints)
A continuación se detallan todos los endpoints HTTP del sistema, agrupados por función.

🔐 Autenticación
HTTP Basic Auth protege: Dashboard, Admin Config, DB Viewer, Vehicles, Audit Logs e Inspector.
Credenciales: variables de entorno DASHBOARD_USER y DASHBOARD_PASSWORD.
Machine-to-Machine (API Key): Webhooks de ingesta (Schmitz, dynamic).
Sin auth (público): Health check.

## 1. Ingesta — Webhooks PUSH (Machine-to-Machine, requiere API Key)
Reciben telemetría de proveedores externos. Responden HTTP 202 inmediato (Drop and Forget).

### 🚨 IMPORTANTE: Endpoint Productivo SCHMITZ
Por requerimiento estricto y no negociable del proveedor, Schmitz debe apuntar a la siguiente URL exacta para producción:
👉 **`https://api.telemetria.assistcargo.com/Json/Data`** *(Método: POST)*

*(Nota técnica: internamente esto asume `env=prod` por defecto si no se especifica).*

### Endpoints Generales del iPaaS

| Método | Path | Para qué sirve |
|--------|------|----------------|
| POST | `/Json/Data?env={test\|prod}` | Webhook oficial para Schmitz (Legacy/Hardcodeado). |
| POST | `/schmitz/webhook?env={test\|prod}` | Alias genérico alternativo para Schmitz. |
| POST | `/webhook/dynamic/{provider_name}?env={test\|prod}` | Webhook Universal para TODAS las nuevas integraciones (ej. `/webhook/dynamic/geotab`). |

**Reglas para estos endpoints:**
* Validan API key mediante la cabecera `x-api-key` (fail-closed: sin key configurada → HTTP 401).
* Persisten directo a la base de datos SQLite correspondiente (fragmentada por provider/env).
* Responden HTTP 202 inmediato para jamás frenar la cola del proveedor.

2. Dashboard — UI y métricas (Basic Auth)

| Método | Path | Para qué sirve | Archivo |
|--------|------|----------------|---------|
| GET | `/dashboard` | Render HTML del dashboard (Jinja2) — la UI que ve el operador | `app/api/routers/dashboard.py` |
| GET | `/api/stats?status=&provider=` | KPIs consolidados de todas las BDs (pending/sent/failed/retries, latencias, throughput, top 200 eventos) | `app/api/routers/dashboard.py` |
| GET | `/api/stats/stream` | SSE — telemetría en tiempo real (push cada 2s a todas las pantallas conectadas) | `app/api/routers/dashboard.py` |

3. Admin Config — Gestión de proveedores (Basic Auth)

| Método | Path | Para qué sirve | Archivo |
|--------|------|----------------|---------|
| GET | `/api/config/providers` | Lista proveedores configurados con sus credenciales (enmascaradas) | `app/api/routers/admin_config.py` |
| POST | `/api/config/providers` | Crear nuevo proveedor (PUSH o PULL) | `app/api/routers/admin_config.py` |
| GET | `/api/config` | Lista entornos disponibles (test/prod) | `app/api/routers/admin_config.py` |
| POST | `/api/config` | Crear entorno nuevo | `app/api/routers/admin_config.py` |
| GET | `/api/config/{provider_name}/{env}/mapping` | Obtener mapping JSONPath del proveedor (cómo traducir su JSON al Modelo Canónico) | `app/api/routers/admin_config.py` |
| POST | `/api/config/{provider_name}/{env}/mapping` | Guardar mapping JSONPath | `app/api/routers/admin_config.py` |
| GET | `/api/config/{provider_name}/{env}/enrichment` | Obtener config de enriquecimiento (URL diccionario IMEI→Placa) | `app/api/routers/admin_config.py` |
| POST | `/api/config/{provider_name}/{env}/enrichment` | Guardar config de enriquecimiento | `app/api/routers/admin_config.py` |
| GET | `/api/config/retention` | Obtener retención de logs configurada (crudos y procesados) | `app/api/routers/admin_config.py` |
| PUT | `/api/config/retention` | Actualizar retención (crudos 7-90 días, procesados 7-30 días) | `app/api/routers/admin_config.py` |
| PUT | `/api/config/processed-logs-toggle` | Activar/desactivar backups de eventos procesados a disco | `app/api/routers/admin_config.py` |
| POST | `/api/config/purge-logs` | Purga manual con guardrails (min 7 días, escribir "PURGAR", revalidar password) | `app/api/routers/admin_config.py` |

4. DB Viewer — Visor SQLite (Basic Auth + revalidación password)

| Método | Path | Para qué sirve | Archivo |
|--------|------|----------------|---------|
| GET | `/api/db-viewer/databases` | Lista archivos .db disponibles en el servidor | `app/api/routers/db_viewer.py` |
| GET | `/api/db-viewer/tables?db_name=` | Lista tablas de una BD (con protección anti path-traversal) | `app/api/routers/db_viewer.py` |
| GET | `/api/db-viewer/query?db_name=&table=&limit=&offset=` | SELECT rowid, * — explora datos (solo tablas whitelist editables) | `app/api/routers/db_viewer.py` |
| POST | `/api/db-viewer/update_cell` | Editar celda de tablas whitelist (revalida DASHBOARD_PASSWORD, SQL parametrizado) | `app/api/routers/db_viewer.py` |

5. Vehicles — Buscador de vehículos (Basic Auth)

| Método | Path | Para qué sirve | Archivo |
|--------|------|----------------|---------|
| GET | `/api/vehicles/unique?date=&provider=&search=` | Buscar patentes (DISTINCT chassis_number, LIKE %search%) — monitor forense | `app/api/routers/vehicles.py` |
| GET | `/api/vehicles/data?provider=&env=&chassis=&date=` | Historial JSON crudo de un chasis particular (hasta 500 eventos) | `app/api/routers/vehicles.py` |

6. Audit Logs — Auditoría e historial (Basic Auth)

| Método | Path | Para qué sirve | Archivo |
|--------|------|----------------|---------|
| GET | `/api/logs` | Últimos 50 registros de auditoría JSONL crudos | `app/api/routers/audit_logs.py` |
| DELETE | `/api/logs` | Borrar todos los archivos .jsonl (operación destructiva) | `app/api/routers/audit_logs.py` |
| GET | `/api/history` | Estadísticas diarias consolidadas (últimos 200 DailyStat) | `app/api/routers/audit_logs.py` |

7. Inspector — Mini-Postman con Anti-SSRF (Basic Auth)

| Método | Path | Para qué sirve | Archivo |
|--------|------|----------------|---------|
| POST | `/inspector/catch/{session_id}` | Capturar payload de un webhook temporal (modo PUSH) | `app/api/routers/inspector.py` |
| GET | `/inspector/catch/{session_id}/latest` | Sondear el payload capturado | `app/api/routers/inspector.py` |
| POST | `/inspector/fetch` | HTTP request a URL arbitraria (Anti-SSRF + DNS rebinding mitigation + TLS verification) | `app/api/routers/inspector.py` |
| POST | `/inspector/fetch-token` | OAuth flow — extrae access_token automáticamente de formatos comunes | `app/api/routers/inspector.py` |

8. Health — Liveness (PÚBLICO, sin auth)

| Método | Path | Para qué sirve | Archivo |
|--------|------|----------------|---------|
| GET | `/health` | Liveness check (SELECT 1 SQLite + ping Redis si está configurado) — para load balancers | `app/api/routers/health.py` |

📊 Resumen por categorías

| Categoría | Endpoints | Auth | Función principal |
|-----------|-----------|------|-------------------|
| Ingesta PUSH | 3 | API key (fail-closed) | Recibir telemetría de proveedores |
| Dashboard | 3 | Basic Auth | UI + KPIs + SSE |
| Admin Config | 12 | Basic Auth | CRUD proveedores + retención + purga |
| DB Viewer | 4 | Basic Auth + revalidación | Visor SQLite con edición limitada |
| Vehicles | 2 | Basic Auth | Búsqueda por patente |
| Audit Logs | 3 | Basic Auth | Logs + historial diario |
| Inspector | 4 | Basic Auth | Mini-Postman con Anti-SSRF |
| Health | 1 | Público | Liveness para load balancers |
| Total | 31 | | |

🛡️ Seguridad por endpoint

| Endpoint | Protección |
|----------|------------|
| Webhooks PUSH | API key (fail-closed, secrets.compare_digest) — sin key configurada → 401 |
| Dashboard / Admin / DB Viewer / Vehicles / Audit Logs / Inspector | HTTP Basic Auth (secrets.compare_digest) |
| DB Viewer update_cell | Revalidación de DASHBOARD_PASSWORD en el body + whitelist de tablas editables |
| Inspector /fetch y /fetch-token | Anti-SSRF (bloquea loopback/privadas/metadata cloud) + DNS rebinding mitigation (pin IP + Host header) + TLS verification configurable |
| Health | Sin auth (público, para load balancers) |

---

🧪 Tests Automatizados (pytest)

Suite de 25 tests que protege contra regresiones al integrar nuevas APIs.

```bash
# Instalar dependencias de test (si no están instaladas)
pip install pytest pytest-asyncio

# Ejecutar suite completa
pytest tests/
# Output esperado: 25 passed in ~3s
```

Ver `docs/TESTING.md` para detalles de aislamiento y cómo agregar tests para APIs nuevas.
