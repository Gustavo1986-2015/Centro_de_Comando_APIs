# Configuración de Cloudflare Tunnel (Quick Tunnel)

Este documento explica cómo exponer la API local (FastAPI) a internet de forma rápida y gratuita utilizando Cloudflare Tunnels sin necesidad de tener una cuenta de Cloudflare o un dominio comprado.

## 1. Requisitos Previos

Antes de abrir el túnel, la aplicación debe estar ejecutándose de manera local en el puerto `8000`.

Abre una consola (PowerShell/CMD) en la raíz del proyecto y ejecuta:
```powershell
python main.py
```
*(También puedes iniciarla abriendo el archivo `launcher.py`).*
**Importante:** Esta consola debe permanecer **abierta** en todo momento.

## 2. Descargar Cloudflare Tunnel

Si aún no tienes el programa `cloudflared.exe`, abre una **nueva pestaña de PowerShell** (sin cerrar la de la API) en la raíz del proyecto y ejecuta el siguiente comando para descargarlo:

```powershell
Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile "cloudflared.exe"
```

## 3. Iniciar el Túnel

En esa misma pestaña de PowerShell, ejecuta el túnel apuntando al puerto local donde corre la API:

```powershell
.\cloudflared.exe tunnel --url http://localhost:8000
```

## 4. Obtener la URL Pública

Una vez ejecutado el comando, Cloudflare comenzará a realizar unas pruebas de conectividad. Busca en la consola un cuadro que dice:

```text
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://palabras-aleatorias-generadas.trycloudflare.com                                   |
+--------------------------------------------------------------------------------------------+
```

## 5. Probar el Enlace

Copia el enlace `.trycloudflare.com` generado y agrégale la ruta de los endpoints de la API. 
Por ejemplo:
- **Dashboard:** `https://palabras-aleatorias-generadas.trycloudflare.com/dashboard`
- **Documentación Swagger:** `https://palabras-aleatorias-generadas.trycloudflare.com/docs`

Puedes compartir este enlace con otras personas (ej. Schmit) y podrán acceder a la API desde sus propios dispositivos (incluso usando datos móviles).

## Notas y Limitaciones (¡Importante!)

* **Consolas abiertas:** Para que la conexión funcione, debes mantener abiertas **AMBAS** ventanas de la consola: la que ejecuta `python main.py` y la que ejecuta `cloudflared.exe`.
* **Nombres aleatorios:** La URL de `trycloudflare.com` se genera de forma aleatoria cada vez que ejecutas el túnel. No se puede personalizar este nombre en la versión gratuita sin cuenta.
* **Sesión temporal:** Si cierras el túnel, apagas tu computadora o cancelas el proceso, la URL dejará de existir. Al volver a ejecutar el comando, Cloudflare te asignará un enlace diferente.
* **Alternativa a largo plazo:** Si requieres que la API esté disponible 24/7 en internet con un nombre fijo, se recomienda desplegar el código en un servicio en la nube (PaaS) como **Render.com**, como se detalla a continuación.

---

# Configuración de Render.com (Entorno de Pruebas y 24/7)

Para no depender de una computadora local encendida, el proyecto se despliega en Render.com. Esto provee una URL fija (ej. `schmit-test.onrender.com`) y mantiene el servidor "en escucha" permanentemente.

## 1. Conexión con GitHub
1. Asegúrate de hacer un `git push` de todo tu código hacia tu repositorio de GitHub.
2. Ingresa a [Render.com](https://render.com) e inicia sesión con tu cuenta de GitHub.
3. Haz clic en **New +** y selecciona **Web Service**.
4. Conecta tu repositorio de GitHub.

## 2. Configuración del Entorno
Completa el formulario de creación con los siguientes datos:
* **Name:** El nombre que deseas para tu URL (ej. `schmit-test`).
* **Environment:** Python 3
* **Build Command:** `pip install -r requirements.txt`
* **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
* **Health Check Path:** `/dashboard` *(Muy importante para que Render sepa que el servicio está vivo y no arroje errores 404)*.

## 3. Variables de Entorno (.env)
Render no lee el archivo `.env` local por seguridad. Debes ir a la pestaña **Environment** en Render y agregar manualmente las siguientes claves (basadas en tu `.env.example`):
* `RC_USERNAME` = `(tu usuario)`
* `RC_PASSWORD` = `(tu contraseña)`
* `RC_ENDPOINT` = `http://gps.rcontrol.com.mx/Tracking/wcf/RCService.svc`
* `RC_USE_MOCK` = `True` *(o `False` para conectarse a producción)*
* `REQUIRE_SCHMITZ_AUTH` = `False`
* `SCHMITZ_API_KEY` = `(tu clave si REQUIRE_SCHMITZ_AUTH es True)`

## 4. Despliegue y Actualizaciones
* Haz clic en **Create Web Service** y espera unos minutos a que el log indique `Your service is live`.
* **Para actualizar el código:** Cada vez que hagas un `git push` a la rama `main` desde tu PC, Render detectará los cambios automáticamente y reconstruirá el servidor sin que tengas que hacer nada.

## Notas sobre Render (Cold Start)
* En el **plan gratuito**, el servidor "se duerme" tras 15 minutos sin recibir peticiones.
* La primera petición tras un descanso tardará entre 30 y 50 segundos en despertar el servidor. 
* Si usas simuladores locales de Python (como `simulador_vivo.py`) o herramientas externas, asegúrate de que tengan un `timeout` (tiempo máximo de espera) alto (ej. 30 a 60 segundos) para que no corten la conexión antes de que Render logre despertar.
* En el futuro, puedes asignar el dominio personalizado oficial de la empresa desde la pestaña **Settings > Custom Domains**.
