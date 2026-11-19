# 1. Utilizamos la imagen oficial de Python 3.12 sobre Debian Bookworm (stable)
#    Cambio clave: antes era python:3.12 (Debian trixie/testing, 261 CVEs sin fix)
#    Ahora bookworm es stable y tiene fixes para la mayoría de vulnerabilidades.
FROM python:3.12-slim-bookworm

# 2. Configurar variables de entorno críticas para Python en Docker
# Previene que Python escriba archivos .pyc en el disco
ENV PYTHONDONTWRITEBYTECODE=1
# Previene que Python almacene la salida estándar y de errores en un buffer (vital para ver logs en tiempo real)
ENV PYTHONUNBUFFERED=1
# Forzamos la zona horaria (opcional, pero buena práctica para los logs)
ENV TZ=America/Argentina/Buenos_Aires

# 3. Establecer el directorio de trabajo dentro del contenedor
WORKDIR /app

# 4. Instalar herramientas del sistema operativo que puedan ser necesarias 
# (sqlite3 es útil para debugging interno, libpq-dev por si a futuro migramos a PostgreSQL)
# Además: aplicar fix de libssh2 (único CVE crítico con patch disponible en bookworm)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    tzdata \
    libssh2-1 \
    && apt-get upgrade -y libssh2-1 \
    && rm -rf /var/lib/apt/lists/*

# 4b. v1.9.12 (I): usuario sin privilegios para correr el hub. uid y gid 1000,
# el habitual del primer usuario del servidor: los archivos de db/, logs/ y
# audit/ quedan a nombre de ese usuario también desde afuera del contenedor.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --no-create-home --home-dir /app --shell /usr/sbin/nologin app

# 5. Copiar primero SOLO el archivo de requerimientos para aprovechar la caché de Docker
# Si el requirements.txt no cambia, Docker no volverá a descargar todos los paquetes
COPY requirements.txt .

# 6. Instalar las dependencias de Python (versiones pinnadas en requirements.txt)
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 7. Crear los directorios persistentes vacíos para SQLite y Logs
# (Aunque los declaremos en el docker-compose, es buena práctica que existan)
RUN mkdir -p /app/db /app/logs /app/audit

# 8. Copiar el resto del código fuente del proyecto al contenedor (BAKEADO en la imagen)
# El código vive DENTRO de la imagen. Para actualizarlo, hay que rebuildear.
COPY . .

# 8b. v1.9.12 (I): el código queda de root y de solo lectura para el hub; lo que
# escribe (db/, logs/, audit/) es de `app`. El entrypoint arranca como root, deja
# esas carpetas a nombre de `app` si hace falta (instalaciones que venían como
# root) y baja a `app` antes de lanzar uvicorn. El sed protege de un checkout
# con fin de línea CRLF (Windows), que rompería el script.
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod 0755 /app/docker-entrypoint.sh \
    && chown -R app:app /app/db /app/logs /app/audit
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# 9. Exponer el puerto donde correrá FastAPI (El firewall/nginx de TI ruteará el tráfico hacia acá)
EXPOSE 8000

# 10. Comando de arranque de la aplicación usando Uvicorn
# Se bindea a "0.0.0.0" para aceptar conexiones externas a la red del contenedor
# Sin --reload (ese flag es solo para desarrollo). Corre como `app` (ver el entrypoint).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
