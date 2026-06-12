# Centro de Comando APIs - Preguntas Frecuentes (FAQ)

Este documento centraliza las dudas más comunes sobre la arquitectura, funcionamiento, métricas y seguridad del Centro de Comando.

## 1. Arquitectura y Tiempo Real

### ¿Cómo funciona la actualización en tiempo real del Dashboard?
El Dashboard utiliza **Server-Sent Events (SSE)**. El backend mantiene un *broadcast loop* global que envía un payload con el top 200 de los últimos eventos y métricas pre-calculadas a todos los clientes web conectados cada 2 segundos.
*Nota: SSE envía información desde el servidor al cliente en una sola dirección, lo que lo hace mucho más ligero que WebSockets para este caso de uso.*

### Si hago clic en el filtro "EN COLA" o filtro por Proveedor, ¿qué pasa?
Al aplicar un filtro específico (por estado o proveedor), la grilla web deja de consumir el stream global SSE (ya que este solo manda el top 200 general) y hace un **fetch REST tradicional** (`GET /api/stats?status_filter=...`) directo a las bases de datos. Esto garantiza que veas todos los eventos, incluso si quedaron empujados fuera del "Top 200 global" por una ráfaga masiva reciente.

## 2. Métricas y Latencias

### ¿Qué significan las distintas latencias en el sistema?
Existen 3 latencias clave monitoreadas:
- **Latencia de Transmisión (`avg_transmission_latency`):** Tiempo desde que el GPS del dispositivo tomó el dato físico hasta que llegó a nuestro webhook.
- **Latencia Hub AC (`avg_hub_latency`):** Tiempo que el evento pasó "esperando o procesándose" dentro del Hub de Assistcargo antes de ser enviado a su destino final. Lo ideal es mantenerlo cerca de 1.0s (por el micro-batching).
- **Latencia SOAP RC (`avg_rc_latency`):** Tiempo de respuesta neto (ida y vuelta) del servidor externo (ej. Radio Comando) al confirmar la recepción.

### ¿Por qué a veces la latencia de transmisión daba negativo en el historial?
El campo `date` del dispositivo (hora del GPS) puede venir en zona horaria local o desfasada. Si el evento se marca como recibido en UTC, la diferencia podía dar negativa. Esto se solucionó aplicando un filtro `MAX(0.0, ...)` a nivel base de datos para sanear los cálculos.

### Si un evento queda trabado 3 días, ¿arruina el promedio de latencia de ese día?
No. El sistema implementa un esquema de **Protección contra Outliers**. Cualquier evento que demore más de **300 segundos (5 minutos)** en el Hub es excluido matemáticamente del cálculo del promedio para no distorsionar el 99% de las métricas que fluyen en milisegundos.

## 3. Seguridad y Accesos

### ¿Cómo modifico los usuarios y contraseñas del Dashboard?
Toda la seguridad base (Basic Auth) se lee desde las variables de entorno (`.env`):
```env
DASHBOARD_USER=admin
DASHBOARD_PASS=changeme
```

### ¿Por qué el Endpoint `/api/stats` da un error "Not authenticated" si intento leerlo manual?
El endpoint `/api/stats` y todo el visualizador de base de datos (`/api/db-viewer/*`) requieren estrictamente autenticación básica con las credenciales del dashboard para evitar exposición de métricas sensibles o credenciales en tránsito.

### ¿Qué es el Inspector de APIs (`/inspector/*`)?
Es una mini-herramienta embebida (estilo Postman) diseñada para que la interfaz pueda probar llamadas HTTP sin sufrir bloqueos CORS del navegador. Permite capturar webhooks, ejecutar requests cURL o traer Tokens.
**Seguridad:** Está protegido con la autenticación del Dashboard y cuenta con un guard **anti-SSRF**, el cual bloquea categóricamente cualquier intento de consultar direcciones IP privadas, loopbacks o IPs de metadatos cloud (como `169.254.169.254`).

## 4. Bases de Datos (SQLite)

### ¿Por qué hay un archivo SQLite distinto por cada proveedor y entorno?
Para maximizar el throughput y evitar cuellos de botella por locks (bloqueos) de SQLite, el sistema implementa **sharding**.
- `protrack_prod.db`
- `schmitz_test.db`
- `system_config.db` (Ajustes y métricas globales)
Esto permite que, si un proveedor bombardea la API con 1.000 eventos por segundo, solo bloquee su propio archivo, sin afectar la recepción del resto.

### ¿Se borrarán los datos viejos y saturarán el disco?
El sistema tiene un **Proceso de Purga** automático que limpia eventos antiguos (configurable desde el menú de Configuración Global) manteniendo la base de datos veloz. Además, las métricas globales diarias (`sent_count`, promedios de latencia) se archivan permanentemente en la tabla `daily_stats` dentro de `system_config.db`.
