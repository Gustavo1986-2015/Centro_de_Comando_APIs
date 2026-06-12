# Centro de Comando APIs - Documentación Integral (FAQ)

Este documento es la referencia principal para comprender el contexto operativo, la arquitectura de procesamiento y la lógica funcional del **Centro de Comando APIs de Assistcargo**. Está diseñado para responder en detalle cómo opera el sistema de extremo a extremo, siendo de utilidad tanto para usuarios operativos como para ingenieros de sistemas.

---

## 1. Propósito y Arquitectura Central

### ¿Cuál es la verdadera misión del Centro de Comando?
En el ecosistema logístico, existen múltiples proveedores de GPS (ej. Protrack, Schmitz) que emiten datos en formatos completamente distintos, a diferentes velocidades, y con distintas mecánicas (algunos empujan datos, otros requieren ser consultados).
El Centro de Comando actúa como un **Hub Telemático Inteligente**. Su misión es atrapar este caos, transformarlo a un "Modelo Canónico" unificado (el formato estándar que requiere Assistcargo), encolarlo de manera segura, y despacharlo de forma asíncrona y ordenada hacia el servidor central (Recurso Confiable).

### ¿Cómo evita el sistema colapsar si llegan miles de eventos por segundo?
El sistema evita el tradicional error `database is locked` implementando un patrón de **Sharding Dinámico**. En lugar de guardar todos los eventos en un solo archivo físico gigante, el Hub asigna una base de datos local independiente (`.db` SQLite) para cada proveedor y entorno (ej. `protrack_prod.db`, `schmitz_test.db`). Esto permite transacciones de escritura paralelas reales a nivel de sistema operativo, escalando el throughput horizontalmente.

---

## 2. Ingesta de Datos (Push vs Pull)

### ¿Cómo atrapa el Hub los datos de los proveedores?
El Centro de Comando cuenta con dos motores de ingesta híbridos:
1. **Motores PUSH (Webhooks):** Son endpoints de recepción pasiva (ej. la ruta de Schmitz). Están constantemente abiertos y escuchando. Cuando el proveedor tiene un dato nuevo, lo "empuja" al Hub e ingresa en milisegundos a la cola.
2. **Motores PULL (Cron-Driven):** Hay proveedores que no envían datos, sino que exigen que el Hub vaya a buscarlos (ej. Protrack). Para ellos, el sistema tiene un "Puller" dinámico. Mediante un temporizador asíncrono, el Hub genera firmas de seguridad dinámicas (hashes MD5 basados en la fecha actual), solicita el estado de cientos de patentes a la vez, y empuja las respuestas a la cola interna, emulando un comportamiento en tiempo real.

---

## 3. Procesamiento, Despacho y Resiliencia

### ¿Cómo se despachan los eventos a Recurso Confiable (RC)?
No se despachan inmediatamente en el hilo web (lo cual bloquearía la recepción de nuevos datos). Todo lo que ingresa se guarda como estado `pending`.
En el fondo del servidor corre un **Worker Asíncrono**. No utiliza un bucle ineficiente que consulta la base cada "N" segundos; está conectado a un *Despertador Thread-Safe*. En el mismo milisegundo en que ingresa un payload, el Webhook dispara una alerta que "despierta" al Worker, toma un lote de la base de datos, y despacha a RC utilizando un pool de hilos (`ThreadPoolExecutor`) para no bloquear el sistema principal.

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
