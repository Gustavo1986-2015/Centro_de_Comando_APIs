# Resumen del Proyecto: Centro de Comando (Hub Telemático)

Hola equipo, les preparé un resumen de la arquitectura y capacidades técnicas del nuevo Centro de Comando que armamos y, sobre todo, cómo nos aseguramos de que sea seguro, escalable y resiliente ante miles de peticiones.

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
