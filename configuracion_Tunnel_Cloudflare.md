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
* **Alternativa a largo plazo:** Si requieres que la API esté disponible 24/7 en internet con un nombre fijo, se recomienda desplegar el código en un servicio en la nube (PaaS) como **Render.com** o **Koyeb**.
