"""
Protección HTTP del panel (v1.9.12): CSRF y encabezados de seguridad.

Los dos son middlewares ASGI puros (no BaseHTTPMiddleware): no leen ni
reescriben el cuerpo, así que no interfieren con los streams SSE ni con la
ingesta. Solo miran los encabezados de la petición y agregan encabezados a la
respuesta.

CSRF
----
El panel usa HTTP Basic. El navegador reenvía esas credenciales solo, también
cuando la petición la dispara OTRA página: un formulario en un sitio ajeno
puede hacer que el navegador del operador mande un POST de purga o de
reenvío al hub, autenticado. Los navegadores actuales marcan esas peticiones
con `Sec-Fetch-Site: cross-site`, que una página no puede falsificar.

Se rechaza solo ese valor. Sin el encabezado (curl, scripts, navegadores
viejos, los proveedores que mandan al webhook) la petición pasa como antes:
el encabezado lo pone el navegador, y lo que no es un navegador no lo manda.
No se usa Origin ni Host: detrás del proxy del servidor el Host que ve el hub
no es el que ve el navegador, y comparar contra él rechazaría al propio panel.
"""
import json
import logging

logger = logging.getLogger(__name__)

METODOS_QUE_ESCRIBEN = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Ingesta y salud: nunca pasan por el control. Los proveedores no son
# navegadores (no mandan Sec-Fetch-Site), pero aun si un intermediario lo
# agregara, la recepción no puede depender de esto. /schmitz/webhook y
# /schmitz/Json/Data son las rutas de ingesta de Schmitz.
_RUTAS_EXCLUIDAS = frozenset({"/Json/Data", "/health", "/schmitz/webhook", "/schmitz/Json/Data"})
_PREFIJOS_EXCLUIDOS = ("/webhook/", "/inspector/catch/")

ENCABEZADOS_FIJOS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)
HSTS = (b"strict-transport-security", b"max-age=31536000")


def _encabezado(scope, nombre: bytes) -> str | None:
    for clave, valor in scope.get("headers") or ():
        if clave == nombre:
            return valor.decode("latin-1")
    return None


def es_https_scope(scope) -> bool:
    """
    True si la petición llegó por HTTPS: el esquema propio o, detrás del
    proxy que termina TLS, `X-Forwarded-Proto: https`. Mismo criterio para
    HSTS y para la marca Secure de la cookie de sesión.
    """
    if scope.get("scheme") == "https":
        return True
    proto = _encabezado(scope, b"x-forwarded-proto")
    return bool(proto) and proto.split(",")[0].strip().lower() == "https"


def es_https(request) -> bool:
    """es_https_scope para una Request de Starlette."""
    return es_https_scope(request.scope)


def ruta_excluida_de_csrf(ruta: str) -> bool:
    return ruta in _RUTAS_EXCLUIDAS or ruta.startswith(_PREFIJOS_EXCLUIDOS)


class ProteccionCSRF:
    """403 a toda escritura (POST/PUT/PATCH/DELETE) con Sec-Fetch-Site: cross-site."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in METODOS_QUE_ESCRIBEN:
            return await self.app(scope, receive, send)
        ruta = scope.get("path") or ""
        if ruta_excluida_de_csrf(ruta):
            return await self.app(scope, receive, send)
        sitio = _encabezado(scope, b"sec-fetch-site")
        if sitio is None or sitio.strip().lower() != "cross-site":
            return await self.app(scope, receive, send)

        logger.warning(
            "Petición entre sitios rechazada (CSRF): %s %s, Sec-Fetch-Site=%r",
            scope.get("method"), ruta, sitio[:64],
        )
        cuerpo = json.dumps({"detail": "Petición rechazada: llegó desde otro sitio."}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(cuerpo)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": cuerpo})


class EncabezadosDeSeguridad:
    """
    Agrega a TODA respuesta nosniff, DENY y no-referrer; y HSTS solo por
    HTTPS (por HTTP plano el navegador lo ignora, y anunciarlo detrás de un
    acceso sin TLS confundiría). Sin includeSubDomains ni preload: el hub no
    decide por los otros subdominios del dominio.

    No se agrega Content-Security-Policy: el panel tiene scripts y estilos
    en línea; queda pendiente (ver ENTREGA_v1.9.12.md).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        https = es_https_scope(scope)

        async def enviar(mensaje):
            if mensaje["type"] == "http.response.start":
                encabezados = list(mensaje.get("headers") or [])
                presentes = {clave.lower() for clave, _ in encabezados}
                for clave, valor in ENCABEZADOS_FIJOS:
                    if clave not in presentes:
                        encabezados.append((clave, valor))
                if https and HSTS[0] not in presentes:
                    encabezados.append(HSTS)
                mensaje = {**mensaje, "headers": encabezados}
            await send(mensaje)

        await self.app(scope, receive, enviar)
