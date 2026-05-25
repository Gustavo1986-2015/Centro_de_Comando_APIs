# Arquitectura Interna del Centro de Comando APIs

Este documento sirve como guía técnica para desarrolladores e ingenieros que necesiten mantener o escalar el Hub Telemático.

## 1. El Paradigma "Cero Creación Manual de Bases de Datos"

**Pregunta Frecuente:** *"¿Cómo genero la Base de Datos para una futura API?"*
**Respuesta:** ¡No haces nada! El sistema utiliza generación dinámica. 
Cuando en cualquier parte del código llamas a la función `get_session("nombre_proveedor", "entorno")` (ubicada en `app/database.py`), SQLAlchemy revisa la carpeta `/db`. Si el archivo `nombre_proveedor_entorno.db` no existe, **lo crea automáticamente en ese milisegundo**, con todas las tablas perfectamente formateadas, y te devuelve la conexión abierta. El motor se auto-construye.

---

## 2. Estructura de Módulos (El Core)

### Directorio Raíz
- **`main.py`**: Es el punto de entrada de la aplicación. Aquí se registran los routers (URLs) de cada proveedor y arranca el servidor web.
- **`requirements.txt`**: Listado estricto de dependencias para clonar el entorno en AWS usando `pip install -r requirements.txt`.

### Carpeta `app/core/` (Lógica de Negocio Central)
- **`auditor.py`**: Su función `audit_event()` toma el JSON puro recibido y lo escribe en la carpeta `/audit`. Implementa rotación diaria de archivos (JSONL) para que el disco duro no se llene.
- **`sender.py`**: Intermediario entre la Base de Datos local y Recurso Confiable.

### Carpeta `app/database.py` (Magia Multi-DB)
- Mantiene en memoria diccionarios (`_engines`, `_sessions`) para reciclar conexiones abiertas.
- Instancia las bases de datos de SQLite al vuelo dependiendo del proveedor que se le pida.

### Carpeta `app/models/` y `app/schemas/`
- **`db_models.py`**: Define la estructura de SQLite (Tabla `NormalizedRCEvent`).
- **`config_models.py`**: Define la tabla de la base de datos maestra de configuración (`system_config_global.db`).
- **`canonical.py`**: El guardaespaldas del sistema. Usa **Pydantic** para forzar tipos de datos. Aquí se encuentra el interceptor global (`@field_validator`) que asegura que toda patente sea siempre mayúscula y alfanumérica pura.

### Carpeta `app/services/`
- **`rc_soap.py`**: Implementa la clase `RCSOAPClient`. Se encarga de construir el XML feo que requiere el protocolo SOAP y dispararlo a la URL real de Recurso Confiable. Mantiene el Token en memoria.

### Carpeta `app/worker/`
- **`processor.py`**: Es un motor infinito. Extrae los proveedores activos de la configuración maestra y usa `asyncio.gather` para abrir todas sus bases de datos en simultáneo. Busca eventos "pending", invoca a `rc_soap.py`, y marca los eventos como "sent" o "failed". También limpia datos viejos.

---

## 3. El Traductor (Mapper) y el Paradigma Push vs Pull

La Base de Datos Dinámica almacena **únicamente el Modelo Canónico** (las 18 columnas universales como `latitude`, `temperature`, `code`, etc.). Jamás guarda los nombres de campos extraños que envían los proveedores.

Por lo tanto, no importa si un dato llega porque el proveedor nos lo envió (Webhooks / PUSH) o porque nosotros corrimos un CronJob para ir a buscarlo (PULL). El flujo es siempre el mismo:

1. **Ingesta Cruda:** Llega el JSON inentendible (ej. `{"pos_x": -34, "temp_door": 12}`). Se guarda intacto en los **Logs de Auditoría** para respaldo.
2. **El Mapper (La única tarea humana):** Alguien del equipo programa un archivo `mapper.py` exclusivo para este proveedor. Este script hace la traducción: `canonical.latitude = json["pos_x"]`.
3. **Guardado Transparente:** Se pasa el objeto canónico ya traducido a la base de datos `proveedor_entorno.db`, la cual lo guarda sin hacer preguntas, porque ya viene en el idioma universal que el sistema entiende.

---

## 4. Configuración en Producción (Cloudflare Tunnels)

Para evitar exponer puertos de la máquina virtual (VM) en AWS y maximizar la seguridad (Zero Trust), se recomienda el uso de **Cloudflare Tunnels** (`cloudflared`).

Es **mandatorio** crear dos (2) túneles separados (o dos subdominios enrutados por Cloudflare) para mantener una separación física de los entornos antes de enviar a Recurso Confiable:

- **Túnel PROD:** Ej. `https://prod-hub.assistcargo.com` -> Apuntando al puerto 8000 local. (La URL para el proveedor será: `.../webhook?env=prod`)
- **Túnel TEST:** Ej. `https://test-hub.assistcargo.com` -> Apuntando al mismo puerto 8000 local. (La URL para el proveedor será: `.../webhook?env=test`)

Ambas URLs convergen en la misma aplicación interna, pero obligan a los proveedores externos (y a las integraciones) a definir claramente a qué subdominio disparan, blindando así los datos productivos.

---

## 5. ¿Cómo agregar una Nueva API en el futuro? (Guía Paso a Paso)

Supongamos que Assistcargo firma con un proveedor llamado **"Samsara"**.

1. **Crear Carpeta:** Crea la carpeta `app/providers/samsara/`.
2. **Crear Traductor:** Crea `app/providers/samsara/mapper.py`. Aquí escribes una función que tome el JSON raro de Samsara y devuelva un objeto `RCCanonicalModel` limpio.
3. **Crear Router:** Crea `app/providers/samsara/router.py`. Haces un `@router.post("/samsara/webhook")` que escuche los eventos.
   - Adentro de ese endpoint, llamas a `audit_event()`.
   - Llamas a tu mapper.
   - Pides la base de datos `get_session("samsara", "prod")` y haces el `db.add()`.
4. **Registrar Router:** Vas a `app/main.py` y agregas `app.include_router(samsara_router.router)`.
5. **Activar Proveedor:** Modificas el endpoint `/api/config` en `dashboard.py` o directamente inyectas el proveedor en `system_config_global.db` para que el Worker (procesador asíncrono) sepa que debe empezar a escuchar la base de datos "samsara". (En el futuro se puede agregar un botón "Nuevo Proveedor" en la interfaz).

¡Eso es todo! Con esos simples pasos, Samsara tendrá su propia base de datos auto-creada, concurrencia total, interfaz visual y conexión a RC asegurada.
