# Centro de Comando APIs - Preguntas Frecuentes (FAQ)

Bienvenido a las Preguntas Frecuentes del **Centro de Comando APIs de Assistcargo**. Este documento está diseñado para ayudar a los operadores y usuarios del dashboard a entender cómo funciona el monitoreo de telemetría de punta a punta.

## 1. Conceptos Generales

### ¿Qué es el Centro de Comando APIs?
Es una plataforma de monitoreo en tiempo real que consolida y visualiza todo el tráfico de datos (telemetría GPS, sensores, alarmas) que viaja desde las integraciones de los proveedores (ej. Protrack, Schmitz) hacia nuestro sistema central de procesamiento (Radio Comando).

### ¿Qué información se muestra en el Dashboard?
El dashboard te permite ver el estado global de la salud de las integraciones mediante:
- Indicadores de estado de conexión de cada proveedor.
- Contadores diarios de eventos procesados.
- Promedios de latencia (tiempos de demora) en vivo.
- Una grilla detallada con la trazabilidad exacta de los últimos eventos recibidos.

## 2. Indicadores y Estados

### ¿Qué significan las tarjetas superiores (En Cola, Enviados, Fallidos, En Reintento)?
- **En Cola (Pendientes):** Eventos que acabamos de recibir del proveedor y que actualmente están siendo procesados por nuestro Hub antes de ser despachados.
- **Enviados (Hoy):** Total de eventos que fueron procesados y entregados con éxito a Radio Comando durante el día en curso.
- **Fallidos (Hoy):** Eventos que, tras agotar todos los intentos posibles, no pudieron ser entregados al destino.
- **En Reintento:** Eventos que fallaron en su primer intento (por ejemplo, por una micro-caída de red) y el sistema está intentando retransmitirlos automáticamente.

### ¿Qué diferencia hay entre "Latencia Hub AC" y "Latencia RC"?
- **Latencia Hub AC (Assistcargo):** Es el tiempo que el evento pasa "adentro" de nuestra plataforma. Desde que entra por el webhook hasta que sale hacia su destino. Lo normal es que se mantenga cerca de 1.0 segundo, ya que el sistema agrupa eventos (Micro-Batching) para mayor eficiencia.
- **Latencia RC (Radio Comando):** Es el tiempo neto que tarda el sistema destino en respondernos "Recibido Ok". Si este número sube, indica lentitud en los servidores externos.

## 3. Uso de la Grilla de Trazabilidad

### ¿Cómo leo la información de un evento en la grilla?
Cada fila representa un reporte de GPS y muestra:
1. **Vehículo:** Patente/Activo, IMEI y el botón para ver el "JSON Origen" (el dato crudo que mandó el proveedor).
2. **Ubicación:** Fecha y hora exacta de la coordenada GPS.
3. **Sensores Clave:** Resumen de velocidad, estado del motor (Ignición), batería, etc.
4. **Trazabilidad:** Una línea de tiempo exacta (con milisegundos) que muestra cuándo fue enviado por el proveedor, cuándo lo recibimos, y en qué milisegundo Radio Comando nos dio el OK.

### ¿Para qué sirve el botón "Descargar Excel"?
Permite exportar una instantánea de los eventos que estás visualizando actualmente en la grilla para auditoría, análisis externo o armado de reportes operativos.

### ¿Qué pasa si aplico filtros de Proveedor o Estado?
La grilla se actualizará para mostrarte exclusivamente el tráfico de ese proveedor (ej. solo Protrack) o solo los eventos en un estado particular (ej. solo los "Fallidos" para investigar por qué no están llegando).

## 4. Herramientas de Diagnóstico

### ¿Para qué sirve el menú "Inspector de APIs"?
Es una herramienta avanzada incorporada en el sistema que permite a los operadores técnicos y al equipo de soporte simular envíos de datos o probar conectividad hacia endpoints externos directamente desde nuestros servidores, garantizando que los bloqueos de red locales no afecten las pruebas.

## 5. Historial y Retención

### ¿Por cuánto tiempo puedo ver los eventos en la grilla?
El dashboard en tiempo real está diseñado para monitoreo táctico y muestra los flujos más recientes de información. Para garantizar el máximo rendimiento y velocidad (mostrando latencias sub-segundo), los eventos individuales crudos se depuran automáticamente del monitor en vivo luego de unas horas.

### ¿Dónde consulto la información de días anteriores?
En el menú superior puedes acceder a la sección de **Historial (Gráficos)**. Allí, el sistema guarda de forma permanente el recuento total de envíos, fallos y los promedios de latencia de días anteriores, lo cual es ideal para analizar tendencias a largo plazo.
