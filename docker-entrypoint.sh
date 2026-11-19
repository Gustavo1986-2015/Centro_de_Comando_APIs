#!/bin/sh
# v1.9.12 (I): el hub corre como el usuario `app` (uid y gid 1000), no como root.
#
# Este script arranca como root SOLO para dos cosas:
#  1. Dejar db/, logs/ y audit/ (los volúmenes) a nombre de `app`, si algo
#     adentro no lo está: una instalación que venía corriendo como root
#     (v1.9.11 o anterior) los tiene a nombre de root. Si ya son de `app`, no
#     toca nada.
#  2. Bajar a `app` con setpriv (util-linux, viene en la imagen base) y
#     reemplazarse por el comando (exec): uvicorn queda como proceso principal
#     del contenedor, sin root, y recibe las señales de `docker stop`.
#
# Volver a una imagen que corre como root (v1.9.11) sigue funcionando: root
# lee y escribe archivos de `app` sin problema.
set -eu

USUARIO=app

if [ "$(id -u)" = "0" ]; then
    for carpeta in /app/db /app/logs /app/audit; do
        mkdir -p "$carpeta"
        # ¿Hay algo (la carpeta o adentro) que no sea de app? find corta en el primero.
        if [ -n "$(find "$carpeta" ! -user "$USUARIO" -print -quit 2>/dev/null)" ]; then
            echo "docker-entrypoint: $carpeta tenía archivos de otro usuario; se pasan a $USUARIO."
            if ! chown -R "$USUARIO:$USUARIO" "$carpeta"; then
                # El volumen no deja cambiar el dueño (solo lectura, NFS con
                # root_squash, etc.). En lugar de no arrancar, el hub sigue
                # como root, igual que en la v1.9.11.
                echo "docker-entrypoint: AVISO: no se pudo cambiar el dueño de $carpeta; el hub arranca como root, igual que en la v1.9.11."
                exec "$@"
            fi
        fi
    done
    exec setpriv --reuid="$USUARIO" --regid="$USUARIO" --init-groups -- "$@"
fi

# Ya sin root (por ejemplo, con `user:` en docker-compose): se corre tal cual.
exec "$@"
