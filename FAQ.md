# Centro de Comando APIs - Documentación Integral (FAQ)

Este documento es la referencia principal para comprender el contexto operativo, la arquitectura de procesamiento y la lógica funcional del **Centro de Comando APIs de Assistcargo**. Está diseñado para responder en detalle cómo opera el sistema de extremo a extremo, siendo de utilidad tanto para usuarios operativos como para ingenieros de sistemas.

---

## 1. Propósito y Arquitectura Central

### ¿Cuál es la verdadera misión del Centro de Comando?
En el ecosistema logístico, existen múltiples proveedores de GPS (ej. Protrack, Schmitz) que emiten datos en formatos completamente distintos, a diferentes velocidades, y con distintas mecánicas (algunos empujan datos, otros requieren ser consultados).
El Centro de Comando actúa como un **Hub Telemático Inteligente**. Su misión es atrapar este caos, transformarlo a un "Modelo Canónico" unificado (el formato estándar que requiere Assistcargo), encolarlo de manera segura, y despacharlo de forma asíncrona y ordenada hacia el servidor central (Recurso Confiable).

### ¿Cómo evita el sistema colapsar si llegan miles de eventos por segundo?
El sistema evita el tradicional error `database is locked` implementando un patrón de **Sharding Dinámico**. En lugar de guardar todos los eventos en un solo archivo físico gigante, el Hub asigna una base de datos local independiente (`.db` SQLite) para cada proveedor y entorno (ej. `db/protrack/prod.db`, `db/schmitz/test.db`). Esto permite transacciones de escritura paralelas reales a nivel de sistema operativo, escalando el throughput horizontalmente.

---

## 2. Ingesta de Datos (Push vs Pull)

### ¿Cómo atrapa el Hub los datos de los proveedores?
El Centro de Comando cuenta con dos motores de ingesta híbridos:
1. **Motores PUSH (Webhooks):** Son endpoints de recepción pasiva (ej. la ruta de Schmitz). Están constantemente abiertos y escuchando. Cuando el proveedor tiene un dato nuevo, lo "empuja" al Hub e ingresa en milisegundos a la cola.
2. **Motores PULL (Cron-Driven):** Hay proveedores que no envían datos, sino que exigen que el Hub vaya a buscarlos (ej. Protrack). Para ellos, el sistema tiene un "Puller" dinámico. Mediante un temporizador asíncrono, el Hub genera firmas de seguridad dinámicas (hashes MD5 basados en la fecha actual), solicita el estado de cientos de patentes a la vez, y empuja las respuestas a la cola interna, emulando un comportamiento en tiempo real.

### ¿Cómo sabe el sistema leer los JSON distintos de cada proveedor?
El sistema utiliza el **Patrón Traductor (Mappers)**. Existe 1 solo archivo `mapper.py` por cada proveedor. Su única función es tomar el JSON "en idioma crudo" del proveedor (ej. Schmitz, Protrack) y traducirlo al **Modelo Canónico** (el "idioma universal" de nuestro sistema). Una vez traducido a este idioma estándar, el resto del Hub funciona exactamente igual para todos los proveedores, completamente a ciegas del origen del dato. A esto se le llama **Desacoplamiento Absoluto**.

### ¿Qué es el "Agujero Negro 202" y el "Drop and Forget"?
Los proveedores Push más estrictos castigan a los servidores que responden con errores. Para evitarlo, nuestras rutas Webhook operan como un agujero negro (*Fail-safe Ingress*): 
Aceptan el JSON, lo meten en la cola de memoria en milisegundos y automáticamente le devuelven un `HTTP 202 Accepted` al proveedor. Si el JSON traía basura o le faltaban campos, el sistema lo descarta silenciosamente de fondo ("Drop and Forget") sin generar errores hacia afuera. Así nos aseguramos de no romper nunca la transmisión del proveedor.

---

## 3. Procesamiento, Despacho y Resiliencia

### ¿Cómo se despachan los eventos a Recurso Confiable (RC)?
No se despachan inmediatamente en el hilo web (lo cual bloquearía la recepción de nuevos datos). Todo lo que ingresa se guarda en SQLite.
En el fondo del servidor corre un **Worker Asíncrono**. El mismo milisegundo en que ingresa un payload, se "despierta" al Worker. Para evitar que el envío de datos colapse a Recurso Confiable tras una desconexión masiva de la red (Queue Burst), el Worker utiliza un **Semáforo Asíncrono** limitando la cantidad máxima de conexiones en paralelo (ej. 4 simultáneas). Si hay 70.000 datos en la cola, el Hub los absorberá dosificando la entrega para jamás saturar la API externa de destino.

### ¿Qué ocurre si Recurso Confiable o la red se caen?
El Centro de Comando está diseñado para nunca perder un dato. Si un envío falla (por un error 500 o un Timeout temporal de RC), el evento no se descarta. Su estado se mantiene como `pending` y se incrementa un contador de reintentos.
Se aplica un **Backoff Lineal de Castigo**:
- 1° fallo: Se espera 10 segundos para reintentar.
- 2° fallo: Se espera 45 segundos.
- 3° fallo: Se espera 120 segundos.
- 4° fallo: Se espera 300 segundos.
- 5° fallo: Se declara como fallo definitivo (`failed`).
Este mecanismo aísla los eventos "problemáticos" (el tráfico enfermo), permitiendo que la cola siga procesando a toda velocidad los eventos entrantes frescos (el tráfico sano).

---

## 4. El Dashboard de Monitoreo

### ¿Cómo actualiza la pantalla sin que se caiga el servidor?
Si 10 pantallas abrieran el dashboard y consultaran a la vez la base de datos, saturarían el servidor. Para evitar esto, se utiliza **Server-Sent Events (SSE)**. El backend consolida los datos y métricas una vez cada 2 segundos, y los *empuja* a todas las pantallas conectadas simultáneamente. El navegador solo pinta el resultado, con un coste de red bajísimo.

### ¿Qué miden exactamente las latencias?
- **Latencia de Transmisión:** El tiempo exacto entre la hora que marcó el reloj interno del GPS (en el camión) y la hora en que el proveedor nos lo entregó.
- **Latencia Hub AC (Assistcargo):** El tiempo que demoramos nosotros. Desde que lo guardamos en la cola local hasta que el *Worker* lo toma para despacharlo. Se mantiene alrededor de 1.0s gracias a la lógica de procesamiento en pequeños lotes (Micro-Batching).
- **Latencia RC:** El tiempo que demoró el servidor de Recurso Confiable en confirmar y devolver el `HTTP 200 Ok` tras nuestra petición SOAP.

### ¿Por qué a veces un evento demoraba días y arruinaba el promedio?
Existen dos tipos de distorsiones matemáticas que el Hub mitiga proactivamente:
1. **Desfasaje de Timestamps:** A veces los satélites envían horas locales y el servidor compara en UTC, arrojando valores negativos. El sistema aplica un clamp a nivel base de datos (`MAX(0.0)`) para asegurar tiempos reales.
2. **Eventos Zombie (Outliers):** Si un dispositivo pierde señal celular durante un viaje y envía 500 datos de golpe 3 días después, su latencia de transmisión será inmensa. Para evitar que la métrica táctica del día se contamine y muestre "Promedio Hub: 8.000 segundos", el Hub aplica un **Filtro de Outliers** y excluye de los promedios aritméticos cualquier evento que demore más de 300 segundos en nuestro servidor, garantizando que el Dashboard solo grafique la salud operativa actual.

---

## 5. Herramientas de Auditoría y Seguridad

### Si falla el paso de un dato a Pydantic (Modelo Canónico), ¿Se pierde?
No. Antes siquiera de intentar mapear, validar o guardar en SQLite, la capa HTTP más superficial del sistema invoca al `auditor.py`. Este componente crea logs físicos diarios (ej. `audit/schmitz_test.jsonl`) volcando el payload JSON original crudo e intacto en el disco duro. Si hubiese una caída masiva de base de datos, se podrían inyectar los logs de auditoría nuevamente al sistema.

### ¿Qué es el Inspector de APIs del Dashboard?
# Centro de Comando APIs - Documentación Integral (FAQ)

Este documento es la referencia principal para comprender el contexto operativo, la arquitectura de procesamiento y la lógica funcional del **Centro de Comando APIs de Assistcargo**. Está diseñado para responder en detalle cómo opera el sistema de extremo a extremo, siendo de utilidad tanto para usuarios operativos como para ingenieros de sistemas.

---

## 1. Propósito y Arquitectura Central

### ¿Cuál es la verdadera misión del Centro de Comando?
En el ecosistema logístico, existen múltiples proveedores de GPS (ej. Protrack, Schmitz) que emiten datos en formatos completamente distintos, a diferentes velocidades, y con distintas mecánicas (algunos empujan datos, otros requieren ser consultados).
El Centro de Comando actúa como un **Hub Telemático Inteligente**. Su misión es atrapar este caos, transformarlo a un "Modelo Canónico" unificado (el formato estándar que requiere Assistcargo), encolarlo de manera segura, y despacharlo de forma asíncrona y ordenada hacia el servidor central (Recurso Confiable).

### ¿Cómo evita el sistema colapsar si llegan miles de eventos por segundo?
El sistema evita el tradicional error `database is locked` implementando un patrón de **Sharding Dinámico**. En lugar de guardar todos los eventos en un solo archivo físico gigante, el Hub asigna una base de datos local independiente (`.db` SQLite) para cada proveedor y entorno (ej. `db/protrack/prod.db`, `db/schmitz/test.db`). Esto permite transacciones de escritura paralelas reales a nivel de sistema operativo, escalando el throughput horizontalmente.

---

## 2. Ingesta de Datos (Push vs Pull)

### ¿Cómo atrapa el Hub los datos de los proveedores?
El Centro de Comando cuenta con dos motores de ingesta híbridos:
1. **Motores PUSH (Webhooks):** Son endpoints de recepción pasiva (ej. la ruta de Schmitz). Están constantemente abiertos y escuchando. Cuando el proveedor tiene un dato nuevo, lo "empuja" al Hub e ingresa en milisegundos a la cola.
2. **Motores PULL (Cron-Driven):** Hay proveedores que no envían datos, sino que exigen que el Hub vaya a buscarlos (ej. Protrack). Para ellos, el sistema tiene un "Puller" dinámico. Mediante un temporizador asíncrono, el Hub genera firmas de seguridad dinámicas (hashes MD5 basados en la fecha actual), solicita el estado de cientos de patentes a la vez, y empuja las respuestas a la cola interna, emulando un comportamiento en tiempo real.

### ¿Cómo sabe el sistema leer los JSON distintos de cada proveedor?
El sistema utiliza el **Patrón Traductor (Mappers)**. Existe 1 solo archivo `mapper.py` por cada proveedor. Su única función es tomar el JSON "en idioma crudo" del proveedor (ej. Schmitz, Protrack) y traducirlo al **Modelo Canónico** (el "idioma universal" de nuestro sistema). Una vez traducido a este idioma estándar, el resto del Hub funciona exactamente igual para todos los proveedores, completamente a ciegas del origen del dato. A esto se le llama **Desacoplamiento Absoluto**.

### ¿Qué es el "Agujero Negro 202" y el "Drop and Forget"?
Los proveedores Push más estrictos castigan a los servidores que responden con errores. Para evitarlo, nuestras rutas Webhook operan como un agujero negro (*Fail-safe Ingress*): 
Aceptan el JSON, lo meten en la cola de memoria en milisegundos y automáticamente le devuelven un `HTTP 202 Accepted` al proveedor. Si el JSON traía basura o le faltaban campos, el sistema lo descarta silenciosamente de fondo ("Drop and Forget") sin generar errores hacia afuera. Así nos aseguramos de no romper nunca la transmisión del proveedor.

---

## 3. Procesamiento, Despacho y Resiliencia

### ¿Cómo se despachan los eventos a Recurso Confiable (RC)?
No se despachan inmediatamente en el hilo web (lo cual bloquearía la recepción de nuevos datos). Todo lo que ingresa se guarda en SQLite.
En el fondo del servidor corre un **Worker Asíncrono**. El mismo milisegundo en que ingresa un payload, se "despierta" al Worker. Para evitar que el envío de datos colapse a Recurso Confiable tras una desconexión masiva de la red (Queue Burst), el Worker utiliza un **Semáforo Asíncrono** limitando la cantidad máxima de conexiones en paralelo (ej. 4 simultáneas). Si hay 70.000 datos en la cola, el Hub los absorberá dosificando la entrega para jamás saturar la API externa de destino.

### ¿Qué ocurre si Recurso Confiable o la red se caen?
El Centro de Comando está diseñado para nunca perder un dato. Si un envío falla (por un error 500 o un Timeout temporal de RC), el evento no se descarta. Su estado se mantiene como `pending` y se incrementa un contador de reintentos.
Se aplica un **Backoff Lineal de Castigo**:
- 1° fallo: Se espera 10 segundos para reintentar.
- 2° fallo: Se espera 45 segundos.
- 3° fallo: Se espera 120 segundos.
- 4° fallo: Se espera 300 segundos.
- 5° fallo: Se declara como fallo definitivo (`failed`).
Este mecanismo aísla los eventos "problemáticos" (el tráfico enfermo), permitiendo que la cola siga procesando a toda velocidad los eventos entrantes frescos (el tráfico sano).

---

## 4. El Dashboard de Monitoreo

### ¿Cómo actualiza la pantalla sin que se caiga el servidor?
Si 10 pantallas abrieran el dashboard y consultaran a la vez la base de datos, saturarían el servidor. Para evitar esto, se utiliza **Server-Sent Events (SSE)**. El backend consolida los datos y métricas una vez cada 2 segundos, y los *empuja* a todas las pantallas conectadas simultáneamente. El navegador solo pinta el resultado, con un coste de red bajísimo.

### ¿Qué miden exactamente las latencias?
- **Latencia de Transmisión:** El tiempo exacto entre la hora que marcó el reloj interno del GPS (en el camión) y la hora en que el proveedor nos lo entregó.
- **Latencia Hub AC (Assistcargo):** El tiempo que demoramos nosotros. Desde que lo guardamos en la cola local hasta que el *Worker* lo toma para despacharlo. Se mantiene alrededor de 1.0s gracias a la lógica de procesamiento en pequeños lotes (Micro-Batching).
- **Latencia RC:** El tiempo que demoró el servidor de Recurso Confiable en confirmar y devolver el `HTTP 200 Ok` tras nuestra petición SOAP.

### ¿Por qué a veces un evento demoraba días y arruinaba el promedio?
Existen dos tipos de distorsiones matemáticas que el Hub mitiga proactivamente:
1. **Desfasaje de Timestamps:** A veces los satélites envían horas locales y el servidor compara en UTC, arrojando valores negativos. El sistema aplica un clamp a nivel base de datos (`MAX(0.0)`) para asegurar tiempos reales.
2. **Eventos Zombie (Outliers):** Si un dispositivo pierde señal celular durante un viaje y envía 500 datos de golpe 3 días después, su latencia de transmisión será inmensa. Para evitar que la métrica táctica del día se contamine y muestre "Promedio Hub: 8.000 segundos", el Hub aplica un **Filtro de Outliers** y excluye de los promedios aritméticos cualquier evento que demore más de 300 segundos en nuestro servidor, garantizando que el Dashboard solo grafique la salud operativa actual.

---

## 5. Herramientas de Auditoría y Seguridad

### Si falla el paso de un dato a Pydantic (Modelo Canónico), ¿Se pierde?
No. Antes siquiera de intentar mapear, validar o guardar en SQLite, la capa HTTP más superficial del sistema invoca al `auditor.py`. Este componente crea logs físicos diarios (ej. `audit/schmitz_test.jsonl`) volcando el payload JSON original crudo e intacto en el disco duro. Si hubiese una caída masiva de base de datos, se podrían inyectar los logs de auditoría nuevamente al sistema.

### ¿Qué es el Inspector de APIs del Dashboard?
Es un módulo interno que emula a Postman. Permite a los analistas probar conexiones salientes directamente desde los servidores de Assistcargo en la nube, saltándose problemas locales de VPN corporativas o bloqueos del navegador (CORS).
Por diseño de seguridad, este inspector cuenta con un **Escudo Anti-SSRF**, que intercepta y bloquea llamadas maliciosas (ej. un usuario intentando que el servidor se consulte a sí mismo en `127.0.0.1` o escanee redes privadas).

### ¿Por qué se eliminó la deduplicación de coordenadas en el motor PULL?
En integraciones pasivas (PUSH), el Hub acepta todo lo que le envíen. Para integraciones activas (PULL como Protrack), originalmente el Hub ignoraba los datos si la fecha y coordenadas eran idénticas a la lectura anterior (ej. camión estacionado). Por requerimiento operativo estricto de Assistcargo: *"Siempre debemos mostrar lo que consumimos"*, se removió ese bloqueo local. Ahora el Hub enviará el dato 20 veces por hora a Recurso Confiable si Protrack lo informa 20 veces, dejando la responsabilidad de deduplicar a la plataforma destino.

### ¿Qué hace el Circuit Breaker y los Micro-cortes?
Cuando el Hub experimenta Timeouts o caídas de red intentando comunicarse con RC, se registra un "Micro-corte" (o fallo). Zeep utiliza un **Timeout Granular** (5 segundos para conectar, 25 para leer). Si ocurren **5 fallos consecutivos**, el "Circuit Breaker" corta la corriente y suspende todos los envíos a RC durante 10 minutos (estado OPEN/Rojo). En la UI, si ves `(Micro-cortes: 2/5)` significa que la red está intermitente, pero el sistema está absorbiendo el impacto sin detener el flujo general, protegiendo a RC de saturarse y al Worker de colapsar.

### ¿Por qué los logs de consola dicen "Migración idempotente omitida o error esperado"? ¿Es un error real?
No, es una característica de diseño. El sistema no utiliza pesados frameworks de migración de base de datos (como Alembic) para mantener su ligereza. En su lugar, utiliza un patrón de **"Migración Idempotente por Fuerza Bruta"**. Cada vez que arranca, el código intenta ciegamente inyectar columnas nuevas en las bases de datos locales (ej: `ALTER TABLE ADD COLUMN`). 
- Si la base de datos es nueva: SQLite ejecuta el comando con éxito y crea la columna.
- Si la base de datos ya está actualizada: SQLite arroja un error (`OperationalError: duplicate column name`). El sistema atrapa intencionalmente este error, lo ignora silenciosamente, y asume que la base de datos ya está lista para operar. Es un mecanismo *Fail-Safe* para asegurar retrocompatibilidad.

### ¿Por qué se llena la consola de datos XML enormes y cómo se limpia?
El nivel de detalle de la consola y los archivos físicos está dictado por la variable de entorno `LOG_LEVEL`. Si está en `DEBUG`, el sistema escupirá hasta el último byte de tráfico HTTP saliente (XML de SOAP). Para una operativa normal y limpia, se recomienda `LOG_LEVEL=INFO`.
El sistema incorpora un motor de **Hot-Reload en Logs**, lo que significa que puedes abrir el archivo `.env`, cambiar el nivel de log, y en menos de 5 segundos la consola se silenciará (o se volverá más ruidosa) sin tener que reiniciar jamás el proceso web.

### ¿Dónde se alojan exactamente los logs y cómo se configuran?
El sistema divide los logs en tres categorías distintas para no mezclar diagnósticos con telemetría. La configuración base de estos archivos se controla desde el `.env`:

| Tipo de Log | Ubicación Física | Propósito | Retención Base |
|-------------|------------------|-----------|----------------|
| **1. Transaccionales / Sistema** | `logs/app.jsonl.YYYY-MM-DD` | Errores de código, caídas de red, reinicios de Uvicorn y advertencias. (Los colores se ven en consola, en el archivo se guarda en formato JSONL estructurado). | Controlado por `LOG_RETENTION_DAYS` en el `.env` (Default: 7 días). |
| **2. Crudos (Auditoría PUSH)** | `audit/{proveedor}/YYYY-MM/crudos_YYYY-MM-DD.jsonl` | Es el JSON original, intacto, tal cual lo mandó el camión/proveedor antes de que nuestro código lo toque. Sirve como prueba legal de qué nos enviaron. Los logs crudos no se pueden apagar desde la UI por motivos forenses. | Configurable desde UI (7-90 días). |
| **3. Procesados (Historial RC)** | `db/backups_diarios/{prov}_{env}/YYYY-MM/procesados_YYYY-MM-DD.jsonl` | Es el Modelo Canónico que se logró enviar con éxito a Recurso Confiable (o falló definitivamente). Incluye el `jobId` y los timestamps de latencia. | Configurable desde UI (7-30 días, y apagable por completo). |

*(Nota: Los archivos .jsonl son de texto plano separados por salto de línea. Se pueden abrir con el Bloc de notas o importar directamente a Excel/PowerBI).*

### ¿Se llenará el disco duro del servidor con todos estos logs?
No. El Hub posee un recolector de basura automatizado. Transforma los eventos de base de datos a un formato comprimido de texto por líneas (`.jsonl`) agrupado mensualmente. Cada ciclo de procesamiento purga y borra de forma definitiva cualquier archivo de respaldo en crudo, procesado y logs transaccionales que superen sus umbrales de retención configurados en el Dashboard, garantizando que el almacenamiento del servidor permanezca estable sin importar el volumen de vehículos traficados.

### ¿Puedo controlar la retención o apagar los logs desde la UI?
Sí, la retención de logs se gestiona desde el Dashboard y es totalmente dinámica (sin editar `.env`). Existen controles separados para logs crudos (auditoría forense) y logs procesados (backups diarios).
Por medidas de seguridad y auditoría estricta, los logs crudos no pueden apagarse. Además, el Dashboard ofrece una herramienta de "Purga Manual" de emergencia con guardrails estrictos (requiere al menos 7 días de antigüedad mínima, escribir explícitamente "PURGAR" y revalidación de la contraseña de admin) para evitar borrados accidentales de los historiales de telemetría.
