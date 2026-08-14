"""
Descargas masivas desde el panel: crudos del AVL y enviado-a-RC.

Ambas son SEPARADAS de la descarga de "Última Actividad Global", que ya existía
y se arma en el navegador desde la tabla ya cargada (dashboard.js/downloadExcel).
Ese patrón no sirve acá: estos datos viven en archivos de disco y en la base, no
en el DOM, así que van por endpoint con verify_dashboard_auth y respuesta en
streaming.

Dos formatos, a propósito distintos:

  CRUDOS → JSONL tal cual está en disco, sin transformar. La gracia es poder
      mostrarle al proveedor exactamente lo que mandó. Aplanarlo a CSV exigiría
      lógica por proveedor (los payloads son anidados y cada AVL anida distinto),
      o sea que violaría transversalidad, y encima perdería información.

  ENVIADOS → CSV. Ya es plano y canónico: son las columnas del modelo, iguales
      para todo proveedor presente o futuro.

Los enviados unifican DOS fuentes, porque partirlo haría inútil la función:
  · la base    → lo despachado dentro de la ventana de retención (horas)
  · los JSONL  → lo despachado antes, ya purgado de la base
El endpoint resuelve de dónde sacar cada cosa según el rango pedido; quien
descarga no se entera de que hay dos orígenes.

Rango obligatorio y acotado. Un día de crudos a caudal de certificación son
~2 GB: sin tope, el primer clic se lleva puesto el navegador y la RAM del
contenedor. El tope sale de SystemSettings.export_max_days y se responde con un
400 explícito, no con un archivo gigante.
"""
import csv
import glob
import io
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core import config_cache
from app.core.auditor import log_admin_action
from app.core.auth import verify_dashboard_auth
from app.database import get_session
from app.models.db_models import NormalizedRCEvent
from app.worker.processor import evento_a_registro_respaldo

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Exportaciones"])

AUDIT_DIR = "audit"
BACKUP_DIR = os.path.join("db", "backups_diarios")

# Un evento se purga a la base cuando supera la retención, y el archivo JSONL se
# nombra por el día de la PURGA, no por el día del evento. Con la retención en su
# máximo (72 h) un evento del día D puede terminar escrito en el archivo de D+4.
# Por eso se leen archivos más allá del rango pedido y después se filtra por el
# created_at real de cada registro. Filtrar solo por nombre de archivo perdería
# eventos, que es exactamente lo que no puede pasar en una auditoría.
MARGEN_DIAS_PURGA = 4

# provider y env viajan a rutas del filesystem: se validan contra una lista
# blanca de caracteres antes de tocar disco. Nada de "../".
_SEGMENTO_VALIDO = re.compile(r"^[a-z0-9_-]+$")

TODOS = "todos"


def _validar_segmento(valor: str, campo: str) -> str:
    limpio = (valor or "").strip().lower()
    if limpio == TODOS:
        return TODOS
    if not _SEGMENTO_VALIDO.match(limpio):
        raise HTTPException(
            status_code=400,
            detail=f"El campo '{campo}' solo admite letras, números, guion y guion bajo.",
        )
    return limpio


def _parsear_rango(desde: str, hasta: str) -> tuple[date, date, int]:
    """Valida el rango y devuelve (desde, hasta, tope_de_dias)."""
    try:
        d_desde = datetime.strptime(desde, "%Y-%m-%d").date()
        d_hasta = datetime.strptime(hasta, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Las fechas deben tener formato AAAA-MM-DD.",
        )

    if d_hasta < d_desde:
        raise HTTPException(
            status_code=400,
            detail="La fecha final no puede ser anterior a la inicial.",
        )

    tope = getattr(config_cache.get_settings(), "export_max_days", 7) or 7
    dias = (d_hasta - d_desde).days + 1
    if dias > tope:
        raise HTTPException(
            status_code=400,
            detail=(
                f"El rango pedido son {dias} días y el máximo permitido es {tope}. "
                f"Descargá el período en tramos más cortos, o subí el tope en "
                f"Configuración → Retención de datos."
            ),
        )
    return d_desde, d_hasta, tope


def _dias_del_rango(desde: date, hasta: date):
    actual = desde
    while actual <= hasta:
        yield actual
        actual += timedelta(days=1)


def _carpetas_proveedor(raiz: str, provider: str, env: str) -> list[tuple[str, str, str]]:
    """
    Devuelve [(ruta, proveedor, entorno)] resolviendo 'todos'.

    Las carpetas se llaman {proveedor}_{entorno}. El entorno se separa por el
    ÚLTIMO guion bajo, porque el nombre del proveedor puede tener guiones bajos
    y el entorno no.
    """
    if not os.path.isdir(raiz):
        return []

    encontradas = []
    for nombre in sorted(os.listdir(raiz)):
        if not os.path.isdir(os.path.join(raiz, nombre)) or "_" not in nombre:
            continue
        prov, _, ent = nombre.rpartition("_")
        if provider != TODOS and prov != provider:
            continue
        if env != TODOS and ent != env:
            continue
        encontradas.append((os.path.join(raiz, nombre), prov, ent))
    return encontradas


def _archivos_por_dia(carpeta: str, prefijo: str, dias) -> list[str]:
    """Rutas existentes de {carpeta}/{AAAA-MM}/{prefijo}_{AAAA-MM-DD}.jsonl."""
    rutas = []
    for dia in dias:
        ruta = os.path.join(
            carpeta, dia.strftime("%Y-%m"), f"{prefijo}_{dia.strftime('%Y-%m-%d')}.jsonl"
        )
        if os.path.isfile(ruta):
            rutas.append(ruta)
    return rutas


def _nombre_archivo(base: str, provider: str, env: str, desde: date, hasta: date, ext: str) -> str:
    tramo = desde.isoformat() if desde == hasta else f"{desde.isoformat()}_a_{hasta.isoformat()}"
    return f"{base}_{provider}_{env}_{tramo}.{ext}"


# ─── Crudos del AVL ──────────────────────────────────────────────────────────

@router.get("/api/export/crudos")
def exportar_crudos(
    request: Request,
    provider: str = Query(..., description="Nombre del proveedor, o 'todos'"),
    env: str = Query(..., description="Entorno (prod/test), o 'todos'"),
    desde: str = Query(..., description="Fecha inicial AAAA-MM-DD"),
    hasta: str = Query(..., description="Fecha final AAAA-MM-DD"),
    _auth=Depends(verify_dashboard_auth),
):
    """
    Payloads tal como los mandó el proveedor, sin transformar, en JSONL.

    Se sirve en streaming línea por línea: los archivos de un día a caudal de
    certificación rondan los 2 GB y no entran en memoria.
    """
    provider = _validar_segmento(provider, "provider")
    env = _validar_segmento(env, "env")
    d_desde, d_hasta, _ = _parsear_rango(desde, hasta)

    carpetas = _carpetas_proveedor(AUDIT_DIR, provider, env)
    dias = list(_dias_del_rango(d_desde, d_hasta))

    log_admin_action(
        "export_crudos",
        {"provider": provider, "env": env, "desde": desde, "hasta": hasta},
        request,
        getattr(_auth, "username", "desconocido"),
    )

    def generar():
        for carpeta, prov, ent in carpetas:
            for ruta in _archivos_por_dia(carpeta, "crudos", dias):
                try:
                    with open(ruta, "r", encoding="utf-8") as f:
                        for linea in f:
                            linea = linea.strip()
                            if not linea:
                                continue
                            # El crudo NO se toca. Solo se anotan proveedor y
                            # entorno, que en una descarga de 'todos' son lo
                            # único que permite separar el origen de cada línea.
                            try:
                                registro = json.loads(linea)
                                registro.setdefault("provider", prov)
                                registro.setdefault("env", ent)
                                yield json.dumps(registro, ensure_ascii=False) + "\n"
                            except json.JSONDecodeError:
                                # Línea corrupta (escritura cortada). Se emite
                                # igual: una auditoría no puede ocultar que el
                                # archivo tiene un problema.
                                yield linea + "\n"
                except OSError as e:
                    logger.warning(f"No se pudo leer {ruta} en la exportación de crudos: {e}")

    nombre = _nombre_archivo("crudos", provider, env, d_desde, d_hasta, "jsonl")
    return StreamingResponse(
        generar(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# ─── Enviado a RC ────────────────────────────────────────────────────────────

COLUMNAS_ENVIADOS = [
    "id", "provider", "env", "chassis", "status", "code",
    "date", "created_at", "updated_at",
    "job_id", "response",
    "latitude", "longitude", "speed", "altitude", "course",
    "battery", "humidity", "ignition", "odometer", "temperature",
    "serial_number", "shipment", "vehicle_type", "vehicle_brand", "vehicle_model",
    "rc_latency_sec", "retry_count",
    "origen",
]


def _fila_csv(registro: dict, origen: str) -> list:
    fila = []
    for col in COLUMNAS_ENVIADOS:
        if col == "origen":
            fila.append(origen)
            continue
        valor = registro.get(col)
        if valor is None:
            fila.append("")
        elif isinstance(valor, bool):
            fila.append("true" if valor else "false")
        else:
            fila.append(str(valor))
    return fila


def _en_rango(iso_texto: str | None, desde: date, hasta: date) -> bool:
    if not iso_texto:
        return False
    try:
        return desde <= datetime.fromisoformat(iso_texto).date() <= hasta
    except ValueError:
        return False


@router.get("/api/export/enviados")
def exportar_enviados(
    request: Request,
    provider: str = Query(..., description="Nombre del proveedor, o 'todos'"),
    env: str = Query(..., description="Entorno (prod/test), o 'todos'"),
    desde: str = Query(..., description="Fecha inicial AAAA-MM-DD"),
    hasta: str = Query(..., description="Fecha final AAAA-MM-DD"),
    _auth=Depends(verify_dashboard_auth),
):
    """
    Lo despachado a RC en el rango, en CSV, unificando base y respaldos JSONL.

    La columna `origen` dice de dónde salió cada fila ('base' o 'respaldo'), que
    sirve para entender un resultado raro sin tener que adivinar.
    """
    provider = _validar_segmento(provider, "provider")
    env = _validar_segmento(env, "env")
    d_desde, d_hasta, _ = _parsear_rango(desde, hasta)

    carpetas = _carpetas_proveedor(BACKUP_DIR, provider, env)
    # La base ya no tiene carpeta si todo se purgó, así que los pares
    # (proveedor, entorno) se toman de la unión de ambas fuentes.
    pares = {(prov, ent) for _, prov, ent in carpetas}
    pares |= {
        (prov, ent)
        for _, prov, ent in _carpetas_proveedor(AUDIT_DIR, provider, env)
    }
    for ruta in glob.glob(os.path.join("db", "*", "*.db")):
        prov = os.path.basename(os.path.dirname(ruta))
        ent = os.path.splitext(os.path.basename(ruta))[0]
        if prov in ("system_config", "backups_diarios"):
            continue
        if (provider in (TODOS, prov)) and (env in (TODOS, ent)):
            pares.add((prov, ent))

    dias_archivo = list(
        _dias_del_rango(d_desde, d_hasta + timedelta(days=MARGEN_DIAS_PURGA))
    )

    log_admin_action(
        "export_enviados",
        {"provider": provider, "env": env, "desde": desde, "hasta": hasta},
        request,
        getattr(_auth, "username", "desconocido"),
    )

    def generar():
        buffer = io.StringIO()
        escritor = csv.writer(buffer, delimiter=";", lineterminator="\n")

        def volcar():
            datos = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return datos

        # BOM para que Excel abra el CSV en UTF-8 sin romper los acentos.
        yield "\ufeff"
        escritor.writerow(COLUMNAS_ENVIADOS)
        yield volcar()

        for prov, ent in sorted(pares):
            # 1) La base: lo despachado que todavía no se purgó.
            #
            # Se recorre PRIMERO y se anotan los ids emitidos, para que el paso
            # siguiente no repita una fila si la purga alcanzó a escribir el
            # respaldo pero falló al borrar. El set queda acotado por la
            # retención (horas), no por el rango pedido.
            vistos = set()
            try:
                db = get_session(prov, ent)
                try:
                    consulta = (
                        db.query(NormalizedRCEvent)
                        .filter(
                            NormalizedRCEvent.status.in_(["sent", "failed"]),
                            NormalizedRCEvent.created_at >= datetime.combine(
                                d_desde, datetime.min.time()
                            ),
                            NormalizedRCEvent.created_at < datetime.combine(
                                d_hasta + timedelta(days=1), datetime.min.time()
                            ),
                        )
                        .order_by(NormalizedRCEvent.id)
                    )
                    for fila in consulta.yield_per(500):
                        vistos.add(fila.id)
                        escritor.writerow(
                            _fila_csv(evento_a_registro_respaldo(fila, ent), "base")
                        )
                        yield volcar()
                finally:
                    db.close()
            except Exception as e:
                # Una base ilegible no puede abortar la descarga entera: se
                # sigue con los respaldos, que es donde está el grueso.
                logger.warning(f"No se pudo leer la base {prov}_{ent} al exportar: {e}")

            # 2) Los respaldos: lo purgado. Se filtra por created_at real, no
            #    por el nombre del archivo (ver MARGEN_DIAS_PURGA).
            carpeta = os.path.join(BACKUP_DIR, f"{prov}_{ent}")
            for ruta in _archivos_por_dia(carpeta, "procesados", dias_archivo):
                try:
                    with open(ruta, "r", encoding="utf-8") as f:
                        for linea in f:
                            linea = linea.strip()
                            if not linea:
                                continue
                            try:
                                registro = json.loads(linea)
                            except json.JSONDecodeError:
                                continue
                            if not _en_rango(registro.get("created_at"), d_desde, d_hasta):
                                continue
                            if registro.get("id") in vistos:
                                continue
                            registro.setdefault("provider", prov)
                            registro.setdefault("env", ent)
                            escritor.writerow(_fila_csv(registro, "respaldo"))
                            yield volcar()
                except OSError as e:
                    logger.warning(f"No se pudo leer {ruta} al exportar enviados: {e}")

    nombre = _nombre_archivo("enviado_a_rc", provider, env, d_desde, d_hasta, "csv")
    return StreamingResponse(
        generar(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


# ─── Metadatos para el panel ─────────────────────────────────────────────────

@router.get("/api/export/validar")
def validar_rango(
    provider: str = Query(...),
    env: str = Query(...),
    desde: str = Query(...),
    hasta: str = Query(...),
    _auth=Depends(verify_dashboard_auth),
):
    """
    Valida los parámetros sin generar nada.

    Existe porque el panel necesita poder mostrar el motivo del rechazo ANTES de
    navegar a la descarga: si navegara directo, un 400 abriría una pestaña con un
    JSON críptico. Sondear con HEAD no sirve — Starlette ejecuta igual el
    endpoint y descarta el cuerpo, o sea que armaría el archivo entero al pedo.
    """
    _validar_segmento(provider, "provider")
    _validar_segmento(env, "env")
    d_desde, d_hasta, tope = _parsear_rango(desde, hasta)
    return {"ok": True, "dias": (d_hasta - d_desde).days + 1, "max_dias": tope}


# El inventario recorre el árbol de archivos con os.stat. Es barato, pero el
# panel lo pide cada vez que se abre la vista y hoy —con la retención de crudos
# recién arreglada— esas carpetas pueden tener miles de archivos acumulados. Se
# cachea 60 s, igual que config_cache: el dato cambia de a días, no de a
# segundos, así que la ventana no oculta nada relevante.
_INVENTARIO_TTL_SEG = 60
_inventario_cache: dict = {"datos": None, "expira": 0.0}

_FECHA_EN_NOMBRE = re.compile(r"_(\d{4}-\d{2}-\d{2})\.jsonl$")


def _resumir_carpeta(carpeta: str, prefijo: str) -> dict | None:
    """
    Fechas extremas, días con datos, rango calendario y bytes de una carpeta.

    Las fechas salen del nombre del archivo, no de su contenido: abrir cada
    archivo movería gigabytes para responder una tabla de cuatro columnas.
    """
    fechas, total_bytes = [], 0

    for mes in os.scandir(carpeta) if os.path.isdir(carpeta) else []:
        if not mes.is_dir():
            continue
        for archivo in os.scandir(mes.path):
            if not archivo.is_file() or not archivo.name.startswith(f"{prefijo}_"):
                continue
            coincidencia = _FECHA_EN_NOMBRE.search(archivo.name)
            if not coincidencia:
                continue
            try:
                fechas.append(datetime.strptime(coincidencia.group(1), "%Y-%m-%d").date())
                total_bytes += archivo.stat().st_size
            except (ValueError, OSError):
                continue

    if not fechas:
        return None

    unicas = sorted(set(fechas))
    return {
        "desde": unicas[0].isoformat(),
        "hasta": unicas[-1].isoformat(),
        "dias_con_datos": len(unicas),
        "dias_rango": (unicas[-1] - unicas[0]).days + 1,
        "bytes": total_bytes,
    }


@router.get("/api/export/inventario")
def inventario_en_disco(_auth=Depends(verify_dashboard_auth)):
    """
    Qué hay guardado en disco ahora mismo, por proveedor y entorno.

    Se muestra ARRIBA de los controles de descarga a propósito: la pregunta útil
    al entrar a bajar algo es "¿qué tengo y desde cuándo?", no "¿qué está por
    vencer?". Lo segundo es aritmética sobre una constante conocida y depende de
    que alguien mire el panel en la ventana correcta; lo primero se contesta en
    cualquier momento y deja elegir el rango con el dato a la vista.

    Las fechas salen del NOMBRE de los archivos ({prefijo}_AAAA-MM-DD.jsonl), no
    de su contenido: leer los archivos para esto costaría gigabytes por consulta.
    El tamaño sale de os.stat.

    Se informan por separado los días CON DATOS y el rango calendario. Cuando no
    coinciden hay faltantes en el medio — un corte de ingesta, un proveedor que
    dejó de mandar — y eso es justamente lo que conviene ver antes de pedir un
    rango y creer que salió vacío por error.
    """
    ahora = time.time()
    if _inventario_cache["expira"] > ahora and _inventario_cache["datos"] is not None:
        return _inventario_cache["datos"]

    combos = {}
    for raiz, clave, prefijo in (
        (AUDIT_DIR, "crudos", "crudos"),
        (BACKUP_DIR, "enviados", "procesados"),
    ):
        for carpeta, prov, ent in _carpetas_proveedor(raiz, TODOS, TODOS):
            resumen = _resumir_carpeta(carpeta, prefijo)
            if resumen:
                combos.setdefault((prov, ent), {})[clave] = resumen

    settings = config_cache.get_settings()
    datos = {
        "filas": [
            {"provider": prov, "env": ent,
             "crudos": partes.get("crudos"), "enviados": partes.get("enviados")}
            for (prov, ent), partes in sorted(combos.items())
        ],
        "audit_retention_days": settings.audit_retention_days,
        "processed_retention_days": settings.processed_retention_days,
        "calculado": datetime.now().isoformat(timespec="seconds"),
    }

    _inventario_cache["datos"] = datos
    _inventario_cache["expira"] = ahora + _INVENTARIO_TTL_SEG
    return datos


@router.get("/api/export/opciones")
def opciones_de_exportacion(_auth=Depends(verify_dashboard_auth)):
    """
    Combinaciones proveedor/entorno con datos en disco, más el tope de días.

    El panel arma los selectores con esto en lugar de ofrecer combinaciones que
    no tienen ningún archivo detrás.
    """
    combos = set()
    for raiz in (AUDIT_DIR, BACKUP_DIR):
        for _, prov, ent in _carpetas_proveedor(raiz, TODOS, TODOS):
            combos.add((prov, ent))
    for ruta in glob.glob(os.path.join("db", "*", "*.db")):
        prov = os.path.basename(os.path.dirname(ruta))
        ent = os.path.splitext(os.path.basename(ruta))[0]
        if prov not in ("system_config", "backups_diarios"):
            combos.add((prov, ent))

    settings = config_cache.get_settings()
    return {
        "combinaciones": [
            {"provider": p, "env": e} for p, e in sorted(combos)
        ],
        "max_dias": getattr(settings, "export_max_days", 7) or 7,
        "audit_retention_days": settings.audit_retention_days,
        "processed_retention_days": settings.processed_retention_days,
    }
