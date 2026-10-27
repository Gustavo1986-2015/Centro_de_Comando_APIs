"""
Descartes: consulta, filtros, agrupado por equipo, exportación, crudo y
retención (v1.9.10, Bloque 4). Transversal a todas las integraciones.

Todos los endpoints exigen la autenticación del panel. Los parámetros se
validan acá contra el catálogo de categorías y el esquema real, y el SQL es
siempre parametrizado (app/core/descartes.py). Un parámetro inválido responde
400 con el motivo, nunca un error de base.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import dateutil.parser
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from app.api.routers.exports import csv_para_excel, xlsx_desde_filas
from app.core import config_cache, descartes
from app.core.auditor import AUDIT_DIR, log_admin_action
from app.core.auth import verify_dashboard_auth

router = APIRouter(prefix="/api/descartes", tags=["Descartes"])

_NOMBRE = re.compile(r"^[a-z0-9_]+$")
POR_PAGINA_MAXIMO = 500
GRUPOS_MAXIMO = 1000
TOPE_EXPORTACION = 100_000
LARGO_TEXTO_MAXIMO = 100
VENTANA_POR_DEFECTO_SEG = 24 * 3600

COLUMNAS_EXPORTACION = [
    "fecha_local", "fecha_utc", "integracion", "entorno", "categoria", "categoria_nombre", "tipo",
    "origen", "motivo", "detalle", "equipo", "envio", "alert_id", "latitud", "longitud", "ingest_id",
]


def _error(detalle: str):
    raise HTTPException(status_code=400, detail=detalle)


def _fecha(texto: str | None, nombre: str) -> float | None:
    """ISO 8601. Sin zona horaria se toma la hora local del servidor."""
    if texto is None or not str(texto).strip():
        return None
    try:
        dt = dateutil.parser.isoparse(str(texto).strip())
    except (ValueError, OverflowError):
        _error(f"'{nombre}' no es una fecha válida (formato ISO, ej. 2026-10-23T08:00).")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _entero(texto, nombre: str, minimo: int, maximo: int, defecto: int) -> int:
    if texto is None or texto == "":
        return defecto
    try:
        valor = int(texto)
    except (TypeError, ValueError):
        _error(f"'{nombre}' tiene que ser un número entero.")
    if not minimo <= valor <= maximo:
        _error(f"'{nombre}' tiene que estar entre {minimo} y {maximo}.")
    return valor


def _filtros(desde, hasta, provider, env, categorias, tipo, texto) -> descartes.Filtros:
    ahora = time.time()
    hasta_ts = _fecha(hasta, "hasta") or ahora + 1
    desde_ts = _fecha(desde, "desde") or hasta_ts - VENTANA_POR_DEFECTO_SEG
    if desde_ts >= hasta_ts:
        _error("'desde' tiene que ser anterior a 'hasta'.")
    valores = {}
    for nombre, valor in (("provider", provider), ("env", env)):
        valor = (valor or "").strip().lower()
        if valor in ("", "todos", "all"):
            valores[nombre] = None
        elif not _NOMBRE.match(valor):
            _error(f"'{nombre}' inválido: solo letras minúsculas, números y guion bajo.")
        else:
            valores[nombre] = valor
    lista = [c.strip() for c in (categorias or "").split(",") if c.strip()]
    desconocidas = [c for c in lista if c not in descartes.CATEGORIAS]
    if desconocidas:
        _error(f"Categorías que no están en el catálogo: {', '.join(desconocidas)}.")
    tipo = (tipo or "todos").strip().lower()
    if tipo not in ("todos", *descartes.TIPOS):
        _error("'tipo' tiene que ser esperado, a_revisar o todos.")
    texto = (texto or "").strip()
    if len(texto) > LARGO_TEXTO_MAXIMO:
        _error(f"'texto' admite hasta {LARGO_TEXTO_MAXIMO} caracteres.")
    return descartes.Filtros(desde_ts, hasta_ts, valores["provider"], valores["env"], lista,
                             None if tipo == "todos" else tipo, texto or None)


@router.get("/catalogo")
def catalogo(_: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    return {"categorias": descartes.catalogo(), "tipos": list(descartes.TIPOS)}


@router.get("")
async def consultar_descartes(
    desde: str | None = None, hasta: str | None = None, provider: str | None = None, env: str | None = None,
    categorias: str | None = None, tipo: str | None = None, texto: str | None = None,
    limit: str | None = None, offset: str | None = None,
    _: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """Filas paginadas del rango, el total y el conteo por categoría del rango."""
    f = _filtros(desde, hasta, provider, env, categorias, tipo, texto)
    limite = _entero(limit, "limit", 1, POR_PAGINA_MAXIMO, 50)
    desplazamiento = _entero(offset, "offset", 0, 10_000_000, 0)
    resultado = await asyncio.to_thread(descartes.buscar, f, limite, desplazamiento)
    return {**resultado, "limit": limite, "offset": desplazamiento, "desde": f.desde, "hasta": f.hasta}


@router.get("/por-equipo")
async def descartes_por_equipo(
    desde: str | None = None, hasta: str | None = None, provider: str | None = None, env: str | None = None,
    categorias: str | None = None, tipo: str | None = None, texto: str | None = None, limit: str | None = None,
    _: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """Por equipo: integración, categoría, cantidad, primero, último y último envío."""
    f = _filtros(desde, hasta, provider, env, categorias, tipo, texto)
    limite = _entero(limit, "limit", 1, GRUPOS_MAXIMO, 200)
    return await asyncio.to_thread(descartes.por_equipo, f, limite)


def _fila_exportacion(r: dict) -> list:
    momento = datetime.fromtimestamp(r["ts"], tz=timezone.utc)
    cat = descartes.CATEGORIAS.get(r.get("categoria") or "", descartes.CATEGORIAS[descartes.CATEGORIA_POR_DEFECTO])
    return [momento.astimezone().strftime("%Y-%m-%d %H:%M:%S"), momento.strftime("%Y-%m-%d %H:%M:%S"),
            r["proveedor"], r["env"], r.get("categoria"), cat.nombre, cat.tipo, r["origen"], r["motivo"],
            r.get("detalle"), r.get("equipo"), r.get("envio"), r.get("alert_id"), r.get("latitud"),
            r.get("longitud"), r.get("ingest_id")]


@router.get("/exportar")
async def exportar_descartes(
    request: Request,
    formato: str = "csv",
    desde: str | None = None, hasta: str | None = None, provider: str | None = None, env: str | None = None,
    categorias: str | None = None, tipo: str | None = None, texto: str | None = None,
    credenciales: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """
    Los descartes filtrados, en CSV o Excel (.xlsx), con los mismos filtros de
    la consulta. Fechas en hora local del servidor y en UTC. Como máximo
    TOPE_EXPORTACION filas: si se recorta, lo dicen el encabezado
    X-Descartes-Recortado y la primera línea del archivo.
    """
    formato = (formato or "").lower()
    if formato not in ("csv", "xlsx"):
        _error("'formato' tiene que ser csv o xlsx.")
    f = _filtros(desde, hasta, provider, env, categorias, tipo, texto)
    filas, recortado = await asyncio.to_thread(descartes.filas_para_exportar, f, TOPE_EXPORTACION)
    log_admin_action("export_descartes", {
        "formato": formato, "provider": f.proveedor, "env": f.env, "categorias": f.categorias, "tipo": f.tipo,
        "texto": f.texto, "desde": f.desde, "hasta": f.hasta, "filas": len(filas), "recortado": recortado,
    }, request, credenciales.username)
    datos = [_fila_exportacion(r) for r in filas]
    if recortado:
        aviso = [f"AVISO: se exportan las {TOPE_EXPORTACION:,} más recientes; el rango tiene más. "
                 f"Acotá los filtros para ver el resto."] + [""] * (len(COLUMNAS_EXPORTACION) - 1)
        datos = [aviso] + datos
    tramo = (datetime.fromtimestamp(f.desde).strftime("%Y-%m-%d") + "_a_"
             + datetime.fromtimestamp(f.hasta).strftime("%Y-%m-%d"))
    nombre = f"descartes_{f.proveedor or 'todas'}_{tramo}.{formato}"
    cabeceras = {"Content-Disposition": f'attachment; filename="{nombre}"',
                 "X-Descartes-Filas": str(len(filas)), "X-Descartes-Recortado": "true" if recortado else "false"}
    if formato == "csv":
        return StreamingResponse(csv_para_excel(COLUMNAS_EXPORTACION, datos),
                                 media_type="text/csv; charset=utf-8", headers=cabeceras)
    contenido = await asyncio.to_thread(xlsx_desde_filas, COLUMNAS_EXPORTACION, datos, "Descartes")
    return Response(contenido, headers=cabeceras,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _buscar_crudo(fila: dict) -> dict | None:
    """
    El crudo de la recepción que originó el descarte, en
    audit/{proveedor}_{entorno}/AAAA-MM/crudos_AAAA-MM-DD.jsonl (fecha local),
    buscando en el día del descarte y el anterior y el siguiente.
    """
    proveedor, env, iid = fila["proveedor"], fila["env"], fila["ingest_id"]
    if not (_NOMBRE.match(proveedor or "") and _NOMBRE.match(env or "")):
        return None
    dia = datetime.fromtimestamp(fila["ts"]).date()
    for d in (dia, dia - timedelta(days=1), dia + timedelta(days=1)):
        ruta = os.path.join(AUDIT_DIR, f"{proveedor}_{env}", d.strftime("%Y-%m"), f"crudos_{d.isoformat()}.jsonl")
        if not os.path.isfile(ruta):
            continue
        with open(ruta, encoding="utf-8", errors="replace") as archivo:
            for linea in archivo:
                if iid not in linea:
                    continue
                try:
                    registro = json.loads(linea)
                except ValueError:
                    continue
                if registro.get("ingest_id") == iid:
                    return {"archivo": os.path.relpath(ruta).replace("\\", "/"), "registro": registro}
    return None


@router.get("/{id_descarte}/crudo")
async def crudo_del_descarte(id_descarte: str, _: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    """El payload crudo que originó el descarte, si sigue disponible."""
    id_ = _entero(id_descarte, "id", 1, 2**62, 0)
    fila = await asyncio.to_thread(descartes.descarte, id_)
    if fila is None:
        raise HTTPException(status_code=404, detail="Ese descarte ya no está en el registro.")
    if not fila.get("ingest_id"):
        raise HTTPException(status_code=404, detail=(
            "Este descarte no tiene vínculo con su crudo (es de antes de la v1.9.10, o de una "
            "integración PULL o de Schmitz, que no lo registran)."))
    encontrado = await asyncio.to_thread(_buscar_crudo, fila)
    if encontrado is None:
        dias = getattr(config_cache.get_settings(), "audit_retention_days", None) or 30
        raise HTTPException(status_code=404, detail=(
            f"El crudo de este descarte ya no está disponible: se purgó con la retención de crudos "
            f"({dias} días) o todavía no se escribió."))
    return {"disponible": True, "ingest_id": fila["ingest_id"], "archivo": encontrado["archivo"],
            "timestamp": encontrado["registro"].get("timestamp"), "payload": encontrado["registro"].get("payload")}


class RetencionDescartes(BaseModel):
    dias: int
    max_filas: int


@router.get("/retencion")
async def retencion(_: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    dias, max_filas = descartes.retencion_vigente()
    return {"dias": dias, "max_filas": max_filas,
            "limites_dias": list(descartes.LIMITES_DIAS), "limites_filas": list(descartes.LIMITES_FILAS),
            "por_defecto": {"dias": descartes.DIAS_RETENCION, "max_filas": descartes.MAX_FILAS},
            "mas_viejo": await asyncio.to_thread(descartes.mas_viejo)}


@router.put("/retencion")
async def guardar_retencion(body: RetencionDescartes, request: Request,
                            credenciales: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    """Días y tope de filas del registro de descartes. Se aplican en el momento."""
    (dmin, dmax), (fmin, fmax) = descartes.LIMITES_DIAS, descartes.LIMITES_FILAS
    if not dmin <= body.dias <= dmax:
        _error(f"Los días de retención de descartes tienen que estar entre {dmin} y {dmax}.")
    if not fmin <= body.max_filas <= fmax:
        _error(f"El tope de filas de descartes tiene que estar entre {fmin:,} y {fmax:,}.".replace(",", "."))

    def _guardar():
        from app.database import get_session
        from app.models.config_models import SystemSettings
        db = get_session("system_config", "global")
        try:
            settings = db.query(SystemSettings).first()
            if settings is None:
                settings = SystemSettings()
                db.add(settings)
            settings.descartes_retencion_dias = body.dias
            settings.descartes_max_filas = body.max_filas
            db.commit()
        finally:
            db.close()
        config_cache.invalidate()
        descartes.purgar_ahora()

    await asyncio.to_thread(_guardar)
    log_admin_action("retencion_descartes", body.model_dump(), request, credenciales.username)
    return await retencion(credenciales)
