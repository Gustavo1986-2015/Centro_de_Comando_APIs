"""
Seguridad del panel (v1.9.12).

- /api/seguridad/dos-pasos/…: alta, baja y ajustes de la verificación en dos
  pasos (sección Configuración → Seguridad del panel).
- /auth/verificar y /auth/salir: abrir y cerrar la sesión verificada. Piden
  solo la contraseña (son la puerta de entrada a la verificación).
- /docs, /redoc y /openapi.json: la documentación automática de la API, que
  antes era pública. Ahora pide el mismo acceso que el panel.
"""
import json
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.core import dos_pasos
from app.core.auth import (contrasena_correcta, get_dashboard_user, verify_dashboard_auth,
                           verify_dashboard_password)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Seguridad"])

_SIN_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
              "Pragma": "no-cache", "Expires": "0"}


def _error(e: dos_pasos.ErrorDosPasos):
    return HTTPException(status_code=e.status, detail=e.mensaje)


class ConContrasena(BaseModel):
    password: str = ""


class ConCodigo(BaseModel):
    codigo: str = ""


class Baja(BaseModel):
    password: str = ""
    codigo: str = ""


class Duracion(BaseModel):
    horas: int


# ── Estado y sesión ──────────────────────────────────────────────────────────

@router.get("/api/seguridad/dos-pasos/estado")
def estado_dos_pasos(_=Depends(verify_dashboard_auth)):
    return dos_pasos.resumen()


@router.get("/api/seguridad/sesion")
def sesion(_=Depends(verify_dashboard_auth)):
    """Consulta liviana: el panel la usa para distinguir un corte de red de
    una sesión vencida cuando se corta un stream."""
    return {"ok": True}


# ── Alta ─────────────────────────────────────────────────────────────────────

@router.post("/api/seguridad/dos-pasos/iniciar")
def iniciar(body: ConContrasena, request: Request, credenciales=Depends(verify_dashboard_auth)):
    if not contrasena_correcta(request, body.password):
        raise HTTPException(status_code=403, detail="La contraseña no es correcta.")
    try:
        datos = dos_pasos.iniciar(credenciales.username)
    except dos_pasos.ErrorDosPasos as e:
        raise _error(e)
    return JSONResponse(datos, headers=_SIN_CACHE)


@router.post("/api/seguridad/dos-pasos/confirmar")
def confirmar(body: ConCodigo, request: Request, _=Depends(verify_dashboard_auth)):
    try:
        codigos = dos_pasos.confirmar(body.codigo)
        valor, max_age = dos_pasos.emitir_cookie(dos_pasos.estado())
    except dos_pasos.ErrorDosPasos as e:
        raise _error(e)
    # El navegador que activa queda con su sesión: no tiene que verificarse
    # de nuevo enseguida.
    respuesta = JSONResponse({"activo": True, "codigos_recuperacion": codigos}, headers=_SIN_CACHE)
    dos_pasos.poner_cookie(respuesta, request, valor, max_age)
    return respuesta


# ── Baja y ajustes ───────────────────────────────────────────────────────────

@router.post("/api/seguridad/dos-pasos/desactivar")
def desactivar(body: Baja, request: Request, _=Depends(verify_dashboard_auth)):
    if not contrasena_correcta(request, body.password):
        raise HTTPException(status_code=403, detail="La contraseña no es correcta.")
    try:
        dos_pasos.verificar_codigo(body.codigo, admitir_recuperacion=True)
        dos_pasos.desactivar("desde el panel")
    except dos_pasos.ErrorDosPasos as e:
        raise _error(e)
    respuesta = JSONResponse({"activo": False})
    dos_pasos.borrar_cookie(respuesta, request)
    return respuesta


@router.put("/api/seguridad/dos-pasos/duracion")
def duracion(body: Duracion, _=Depends(verify_dashboard_auth)):
    try:
        return {"duracion_horas": dos_pasos.fijar_duracion(body.horas)}
    except dos_pasos.ErrorDosPasos as e:
        raise _error(e)


@router.post("/api/seguridad/dos-pasos/cerrar-todas")
def cerrar_todas(request: Request, _=Depends(verify_dashboard_auth)):
    """Sube la generación: toda cookie emitida antes deja de valer, también
    la de este navegador."""
    dos_pasos.cerrar_todas()
    respuesta = JSONResponse({"ok": True})
    dos_pasos.borrar_cookie(respuesta, request)
    return respuesta


@router.post("/api/seguridad/dos-pasos/recuperacion")
def nuevos_codigos(body: ConCodigo, _=Depends(verify_dashboard_auth)):
    try:
        codigos = dos_pasos.regenerar_codigos(body.codigo)
    except dos_pasos.ErrorDosPasos as e:
        raise _error(e)
    return JSONResponse({"codigos_recuperacion": codigos}, headers=_SIN_CACHE)


# ── Verificación ─────────────────────────────────────────────────────────────

def pagina_verificacion(request: Request, error: str | None = None, status_code: int = 200):
    from app.api.routers.dashboard import templates
    from app.version import __version__, static_version
    respuesta = templates.TemplateResponse(
        request=request, name="verificacion.html", status_code=status_code,
        context={"css_v": static_version("dashboard.css"), "app_version": __version__,
                 "error": error, "usuario": get_dashboard_user()},
    )
    respuesta.headers.update(_SIN_CACHE)
    return respuesta


async def manejar_verificacion_requerida(request: Request, exc: dos_pasos.VerificacionRequerida):
    """GET /dashboard sin sesión verificada: la página que pide el código. El
    resto de las rutas: 403 {"detail": "verificacion_requerida"}, sin
    WWW-Authenticate (la contraseña ya es correcta)."""
    if request.method == "GET" and request.url.path == "/dashboard":
        return pagina_verificacion(request)
    return JSONResponse({"detail": "verificacion_requerida"}, status_code=403)


async def _codigo_del_cuerpo(request: Request) -> tuple[str, bool]:
    """(código, vino como JSON). El formulario de la página llega como
    application/x-www-form-urlencoded; se lee a mano (sin python-multipart)."""
    crudo = (await request.body())[:4096]
    tipo = (request.headers.get("content-type") or "").lower()
    if "application/json" in tipo:
        try:
            datos = json.loads(crudo or b"{}")
        except ValueError:
            datos = {}
        return str(datos.get("codigo") or "") if isinstance(datos, dict) else "", True
    valores = parse_qs(crudo.decode("utf-8", errors="replace"))
    return (valores.get("codigo") or [""])[0], False


@router.post("/auth/verificar", include_in_schema=True)
async def verificar(request: Request, _=Depends(verify_dashboard_password)):
    codigo, es_json = await _codigo_del_cuerpo(request)
    if not dos_pasos.estado().activo:
        return (JSONResponse({"ok": True, "redirigir": "/dashboard"}) if es_json
                else RedirectResponse("/dashboard", status_code=303))
    try:
        # PBKDF2 de los códigos de recuperación: fuera del bucle de eventos.
        await run_in_threadpool(dos_pasos.verificar_codigo, codigo, True)
        valor, max_age = dos_pasos.emitir_cookie(dos_pasos.estado())
    except dos_pasos.ErrorDosPasos as e:
        if es_json:
            return JSONResponse({"detail": e.mensaje}, status_code=e.status)
        return pagina_verificacion(request, error=e.mensaje, status_code=e.status)
    respuesta = (JSONResponse({"ok": True, "redirigir": "/dashboard"}) if es_json
                 else RedirectResponse("/dashboard", status_code=303))
    dos_pasos.poner_cookie(respuesta, request, valor, max_age)
    return respuesta


@router.post("/auth/salir")
def salir(request: Request, _=Depends(verify_dashboard_password)):
    """«Cerrar esta sesión»: borra la cookie de este navegador."""
    respuesta = JSONResponse({"ok": True})
    dos_pasos.borrar_cookie(respuesta, request)
    return respuesta


# ── Documentación de la API (C) ──────────────────────────────────────────────

@router.get("/openapi.json", include_in_schema=False)
def openapi(request: Request, _=Depends(verify_dashboard_auth)):
    return JSONResponse(request.app.openapi())


@router.get("/docs", include_in_schema=False)
def docs(request: Request, _=Depends(verify_dashboard_auth)) -> HTMLResponse:
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{request.app.title} - Docs")


@router.get("/redoc", include_in_schema=False)
def redoc(request: Request, _=Depends(verify_dashboard_auth)) -> HTMLResponse:
    return get_redoc_html(openapi_url="/openapi.json", title=f"{request.app.title} - ReDoc")
