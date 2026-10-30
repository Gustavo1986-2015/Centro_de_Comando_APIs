"""
Visor de base de datos del panel.

Dos orígenes (v1.9.8):
  · Base (tránsito)        → las bases SQLite de db/: colas, configuración y
                             las bases de estado de Tive (solo lectura).
  · Respaldo (procesados)  → los JSONL de db/backups_diarios/, que es donde
                             queda lo despachado cuando la base ya lo purgó.

Con retencion_base_horas=1 la base de una integración de poco tráfico guarda
pocas filas (Tive: de 6 a 338 crudos por hora, mediana 27, medido en audit/).
Lo que el operador busca casi siempre está en el respaldo, y por eso el visor
lee los dos.

Reglas comunes:
  · lo más reciente primero;
  · las columnas *_enc nunca salen del servidor: se muestran como "(cifrado)",
    también en la descarga (B-13);
  · SQL parametrizado; los nombres de tabla y de columna se validan contra el
    esquema real de la base, no solo contra una expresión regular;
  · las rutas de respaldo se validan para que queden dentro de
    db/backups_diarios/.

v1.9.11:
  · orden en el servidor por cualquier columna del esquema real (no por las
    cifradas: el orden diría algo de su contenido);
  · celdas JSON con resumen en la grilla y el JSON formateado al hacer clic;
  · total y conteo por estado de la consulta (tablas de cola);
  · exportación CSV y Excel de lo filtrado y ordenado, con tope de filas;
  · la cola (normalized_rc_events) es de solo lectura; se editan solo las
    tablas de configuración de system_config_global.db.
"""
import csv
import glob
import heapq
import io
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from itertools import islice
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from sqlalchemy.exc import IntegrityError as IntegrityErrorSQLA

from app.core.auth import verify_dashboard_auth, get_dashboard_password

logger = logging.getLogger(__name__)
router = APIRouter(tags=["DB Viewer"])

# Tablas que el administrador puede editar desde el Visor de BD.
# Las tablas operativas (normalized_rc_events, etc.) son de SOLO LECTURA siempre.
EDITABLE_TABLES = {
    "provider_config",
    "provider_dictionary",
    "daily_stats",
}

# Columnas cifradas (B-13): su contenido no se manda al panel ni a la descarga.
SUFIJO_CIFRADO = "_enc"
TEXTO_CIFRADO = "(cifrado)"

# Tamaño de página: el panel ofrece 50/100/250/500; el servidor pone el techo.
LIMITE_MAXIMO = 1000

ESTADOS_DE_EVENTO = ("pending", "processing", "sent", "failed", "simulado")

# v1.9.11 (Bloque 5). Bases de cola: solo lectura, con conteo por estado.
# Editar la cola a mano puede mandar a RC algo que nadie midió.
TABLAS_COLA = {"normalized_rc_events"}
# Las tablas editables viven en la base de configuración; en otra base son
# residuos de un esquema viejo (ver _tabla_es_huerfana) y no se editan.
BASE_EDITABLE = "system_config_global.db"
# Columnas con JSON: la grilla muestra un resumen y el clic, el JSON entero.
# También cuenta toda columna declarada JSON en la base.
COLUMNAS_JSON = {"raw_data", "rc_response", "rc_payload", "velocidad_detalle", "detalle", "response"}
SENTIDOS = ("asc", "desc")
# Tope de la exportación del visor, el mismo que el de descartes. Si la vista
# tiene más, se exportan las primeras en el orden elegido y el archivo lo dice.
TOPE_EXPORTACION = 100_000
FORMATOS_EXPORTACION = ("csv", "xlsx")

# Filtros del visor → columnas donde aplican, en orden de preferencia. Un
# filtro sobre una tabla que no tiene ninguna de sus columnas no se aplica, y
# la respuesta lo dice (filtros_no_aplicados) para que el panel lo muestre.
COLUMNAS_FILTRO = {
    "estado": ("status",),
    "patente": ("chassis_number", "device_name", "chassis"),
    "envio": ("shipment",),
    "fecha": ("created_at",),
}


class CellUpdateRequest(BaseModel):
    db_name: str
    table: str
    rowid: int
    column_name: str
    new_value: Optional[str]
    password: str  # Revalidación de DASHBOARD_PASSWORD — seguridad real, no cosmética

# Tablas que cada tipo de base debe contener según el esquema vigente
# (app/database.py: create_all separa los modelos por engine).
_TABLAS_CONFIG = {"provider_config", "provider_dictionary", "daily_stats", "system_settings"}
_TABLAS_PROVEEDOR = {"normalized_rc_events"}
# Base de estado de Tive: app/providers/tive/estado.py (_conexion).
_TABLAS_ESTADO_TIVE = {"pares", "vistos", "tramos_pendientes", "consultas",
                       "velocidad_historial"}   # v1.9.10: historial de la velocidad estimada
_TABLAS_INTERNAS = {"sqlite_sequence"}
# Registro de descartes: app/core/descartes.py (db/descartes.db).
ARCHIVO_DESCARTES = "descartes.db"
_TABLAS_DESCARTES = {"descartes"}


def _es_base_de_estado(db_rel: str) -> bool:
    """db/tive/{env}_estado.db: la memoria del módulo de Tive (v1.9.8)."""
    partes = db_rel.replace("\\", "/").split("/")
    return len(partes) == 2 and partes[0] == "tive" and partes[1].endswith("_estado.db")


def _tabla_es_huerfana(db_rel: str, tabla: str) -> bool:
    """
    Indica si una tabla no corresponde al tipo de base donde está.

    Una versión anterior creaba TODOS los modelos en TODOS los engines, así que
    las bases de proveedor quedaron con las tablas de configuración vacías y
    viceversa. El esquema actual ya no lo hace, pero esas tablas siguen en disco
    y aparecen en el selector como si fueran válidas.
    """
    if tabla in _TABLAS_INTERNAS:
        return False
    if db_rel == "system_config_global.db":
        return tabla not in _TABLAS_CONFIG
    if db_rel == ARCHIVO_DESCARTES:
        return tabla not in _TABLAS_DESCARTES
    # Base de estado de Tive. Una normalized_rc_events vacía ahí es residuo:
    # hasta v1.9.7 el listado de almacenamiento la creaba al abrirla.
    if _es_base_de_estado(db_rel):
        return tabla not in _TABLAS_ESTADO_TIVE
    # Base de proveedor: {provider}/{env}.db
    return tabla not in _TABLAS_PROVEEDOR


def _resolve_db_path(db_name: str) -> str | None:
    """
    Resuelve y valida la ruta de una base de datos dentro de ./db/.
    Soporta rutas con subcarpeta (ej: 'protrack/test.db') y raíz (ej: 'system_config_global.db').
    Previene path traversal rechazando cualquier ruta que contenga '..'.
    Retorna la ruta absoluta válida, o None si es sospechosa.
    """
    if not db_name or ".." in db_name:
        return None
    db_root = os.path.abspath("./db")
    candidate = os.path.abspath(os.path.join(db_root, db_name))
    # La ruta resuelta debe quedar dentro de db/
    if not candidate.startswith(db_root + os.sep) and candidate != db_root:
        return None
    return candidate


def _conectar_lectura(db_path: str) -> sqlite3.Connection:
    """Conexión de solo lectura: mirar una base nunca la crea ni la modifica."""
    # check_same_thread=False: la descarga itera el cursor desde el hilo del
    # streaming, que no es el que abrió la conexión. Es de un solo uso.
    return sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True, timeout=10,
                           check_same_thread=False)


def _q(nombre: str) -> str:
    """Identificador SQL citado. Los nombres ya salen del esquema real."""
    return '"' + nombre.replace('"', '""') + '"'


def _ruta_de_base(db_name: str) -> str:
    """Ruta validada de una base existente, o 400/404."""
    db_path = _resolve_db_path(db_name)
    if not db_path:
        raise HTTPException(status_code=400, detail="Ruta de base de datos inválida")
    if not os.path.isfile(db_path):
        raise HTTPException(status_code=404, detail="Base de datos no encontrada")
    return db_path


def _tablas_reales(conn: sqlite3.Connection) -> list[str]:
    return [fila[0] for fila in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _columnas_reales(conn: sqlite3.Connection, tabla: str) -> list[str]:
    # `tabla` ya está validada contra sqlite_master; se cita igual.
    return [c[1] for c in conn.execute(f"PRAGMA table_info({_q(tabla)})")]


def _validar_tabla(conn: sqlite3.Connection, tabla: str) -> str:
    """El nombre tiene que existir en la base: no alcanza con que 'parezca' válido."""
    if tabla not in _tablas_reales(conn):
        raise HTTPException(status_code=400, detail=f"La tabla '{tabla}' no existe en esta base.")
    return tabla


def _es_cifrada(columna: str) -> bool:
    return columna.endswith(SUFIJO_CIFRADO)


def _ocultar(columnas: list[str], fila) -> list:
    """Reemplaza el contenido de las columnas *_enc por "(cifrado)"."""
    return [
        (TEXTO_CIFRADO if valor is not None else None) if _es_cifrada(col) else valor
        for col, valor in zip(columnas, fila)
    ]


def _es_editable(db_name: str, tabla: str) -> bool:
    return tabla in EDITABLE_TABLES and db_name.replace("\\", "/") == BASE_EDITABLE


def _columnas_json(columnas: list[str], tipos: dict | None = None) -> list[str]:
    tipos = tipos or {}
    return [c for c in columnas
            if not _es_cifrada(c) and (c in COLUMNAS_JSON or "JSON" in (tipos.get(c) or "").upper())]


def _orden_pedido(orden: str | None, sentido: str | None, columnas: list[str]) -> tuple[str, str] | None:
    """
    (columna, sentido) validados contra las columnas reales, o None (el orden
    por defecto: lo más reciente primero).
    """
    if not orden:
        return None
    if orden not in columnas:
        raise HTTPException(status_code=400, detail=f"No se puede ordenar por '{orden}': no es una columna de esta vista.")
    if _es_cifrada(orden):
        raise HTTPException(status_code=400, detail=f"'{orden}' está cifrada: no se ordena por ella.")
    sentido = (sentido or "asc").lower()
    if sentido not in SENTIDOS:
        raise HTTPException(status_code=400, detail="El sentido del orden es 'asc' o 'desc'.")
    return orden, sentido


def _order_by(orden: tuple[str, str] | None) -> str:
    # A igualdad de valor, lo más reciente primero (rowid descendente).
    if orden is None:
        return " ORDER BY rowid DESC"
    return f" ORDER BY {_q(orden[0])} {'ASC' if orden[1] == 'asc' else 'DESC'}, rowid DESC"


def _clave_de_orden(valor):
    """
    Orden de los valores del respaldo, el mismo que usa SQLite en la base:
    NULL, después números y después texto.
    """
    if valor is None:
        return (0, 0)
    if isinstance(valor, bool):
        return (1, int(valor))
    if isinstance(valor, (int, float)):
        return (1, valor)
    if isinstance(valor, (dict, list)):
        return (2, json.dumps(valor, ensure_ascii=False))
    return (2, str(valor))


def _proveedor_de_base(db_name: str) -> str | None:
    """db/tive/prod.db → 'tive' (para el texto de "Sin posición")."""
    partes = db_name.replace("\\", "/").split("/")
    return partes[0] if len(partes) == 2 else None


def _sin_posicion(columnas: list[str], filas: list, proveedor: str | None) -> dict | None:
    """
    Filas de cola sin latitud ni longitud: el visor muestra el mismo texto que
    el panel (contrato.texto_sin_posicion), no NULL.
    """
    from app.core import contrato
    if "latitude" not in columnas or "longitude" not in columnas:
        return None
    i_lat, i_lon = columnas.index("latitude"), columnas.index("longitude")
    indices = [n for n, f in enumerate(filas)
               if contrato.es_sin_posicion(SimpleNamespace(latitude=f[i_lat], longitude=f[i_lon]))]
    return {"texto": contrato.texto_sin_posicion(proveedor), "filas": indices}


def _seleccionar_proyeccion(texto: str | None, columnas: list[str]) -> list[int]:
    """
    Índices de las columnas pedidas para exportar ("a,b,c"), validadas contra
    las de la vista. Vacío = todas.
    """
    if not texto:
        return list(range(len(columnas)))
    indices = []
    for nombre in (n.strip() for n in texto.split(",")):
        if not nombre:
            continue
        if nombre not in columnas:
            raise HTTPException(status_code=400, detail=f"La columna '{nombre}' no es de esta vista.")
        indices.append(columnas.index(nombre))
    if not indices:
        raise HTTPException(status_code=400, detail="Elegí al menos una columna para exportar.")
    return indices


@router.get("/api/db-viewer/databases")
def get_databases(_: None = Depends(verify_dashboard_auth)):
    """Lista todas las bases de datos SQLite: raíz + subcarpetas por AVL."""
    db_dir = "./db"
    if not os.path.exists(db_dir):
        return []
    # recursive=True es necesario: sin él, ** solo cubre UN nivel de subcarpeta
    # y una base en db/proveedor/sub/x.db no aparecería en el listado.
    # El set() deduplica cuando un archivo matchea más de un patrón.
    patrones = ("*.db", "*.sqlite", "*.sqlite3")
    archivos = set()
    for pat in patrones:
        archivos.update(glob.glob(f"{db_dir}/**/{pat}", recursive=True))

    result = []
    for f in sorted(archivos):
        rel = os.path.relpath(f, db_dir).replace("\\", "/")
        try:
            size_mb = round(os.path.getsize(f) / (1024 * 1024), 2)
        except OSError:
            size_mb = None
        result.append({
            "name": rel,
            # Agrupa por proveedor en el selector: db/protrack/prod.db -> "protrack"
            "group": rel.split("/")[0] if "/" in rel else "global",
            "size_mb": size_mb,
            # Marca las bases que el esquema actual no genera. Suelen ser
            # residuos de versiones anteriores o de corridas de tests, y
            # confunden al operador porque aparecen junto a las reales.
            "orphan": _es_huerfana(rel),
            # Base de estado de Tive (v1.9.8): se ve, en solo lectura.
            "estado": _es_base_de_estado(rel),
        })
    return result


def _es_huerfana(rel: str) -> bool:
    """
    Determina si un archivo .db corresponde al esquema vigente.

    Esquema actual (app/database.py):
      system_config_global.db      archivo maestro en la raíz
      {provider}/{env}.db          colas operativas por proveedor
      tive/{env}_estado.db         estado del módulo de Tive (v1.9.8)

    Cualquier otra forma es residual: bases de esquemas viejos o generadas por
    corridas de tests. No se borran automáticamente (podrían tener datos que el
    operador quiera rescatar), solo se marcan.
    """
    if rel == "system_config_global.db":
        return False
    # Registro de descartes (v1.9.4). Es del esquema vigente: el visor lo
    # marcaba "Residual" (v1.9.9, 3.7).
    if rel == ARCHIVO_DESCARTES:
        return False
    partes = rel.split("/")
    if len(partes) == 2 and partes[1] in ("prod.db", "test.db"):
        return False
    if _es_base_de_estado(rel):
        return False
    return True

@router.get("/api/db-viewer/tables")
def get_tables(db_name: str = Query(...), _: None = Depends(verify_dashboard_auth)):
    """Lista las tablas de una base de datos específica."""
    db_path = _resolve_db_path(db_name)
    if not db_path:
        raise HTTPException(status_code=400, detail="Ruta de base de datos inválida")
    if not os.path.exists(db_path):
        return []

    try:
        conn = _conectar_lectura(db_path)
        cursor = conn.cursor()
        nombres = _tablas_reales(conn)

        # Conteo por tabla: permite ver de un vistazo cuáles tienen datos sin
        # tener que consultarlas una por una.
        tables = []
        for n in nombres:
            filas = None
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {_q(n)}")
                filas = cursor.fetchone()[0]
            except sqlite3.Error:
                pass   # tabla interna o corrupta: se lista igual, sin conteo
            tables.append({
                "name": n,
                "rows": filas,
                "orphan": _tabla_es_huerfana(db_name.replace("\\", "/"), n),
            })

        return {"tables": tables}
    except Exception as e:
        logger.warning(f"Excepción capturada en db_viewer: {e}")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()


# ─── Filtros ─────────────────────────────────────────────────────────────────

def _fecha(valor: str | None, campo: str) -> date | None:
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"'{campo}' debe tener formato AAAA-MM-DD.")


def _filtros_pedidos(estado, patente, envio, desde, hasta) -> dict:
    """Los filtros que vinieron, ya validados. Vacío = sin ese filtro."""
    filtros = {}
    if estado:
        if estado not in ESTADOS_DE_EVENTO:
            raise HTTPException(status_code=400, detail=f"Estado desconocido: '{estado}'.")
        filtros["estado"] = estado
    if patente and patente.strip():
        filtros["patente"] = patente.strip()
    if envio and envio.strip():
        filtros["envio"] = envio.strip()
    d_desde, d_hasta = _fecha(desde, "desde"), _fecha(hasta, "hasta")
    if d_desde and d_hasta and d_hasta < d_desde:
        raise HTTPException(status_code=400, detail="La fecha final no puede ser anterior a la inicial.")
    if d_desde or d_hasta:
        filtros["fecha"] = (d_desde, d_hasta)
    return filtros


def _columna_de_filtro(filtro: str, columnas: list[str]) -> str | None:
    return next((c for c in COLUMNAS_FILTRO[filtro] if c in columnas), None)


def _where(columnas: list[str], filtros: dict, search: str | None) -> tuple[str, list, list]:
    """
    (cláusula WHERE, parámetros, filtros que esta tabla no tiene cómo aplicar).
    Las columnas salen del esquema real; los valores van como parámetros.
    """
    condiciones, parametros, no_aplicados = [], [], []
    for filtro, valor in filtros.items():
        col = _columna_de_filtro(filtro, columnas)
        if col is None:
            no_aplicados.append(filtro)
            continue
        if filtro == "estado":
            condiciones.append(f"{_q(col)} = ?")
            parametros.append(valor)
        elif filtro in ("patente", "envio"):
            condiciones.append(f"CAST({_q(col)} AS TEXT) LIKE ?")
            parametros.append(f"%{valor}%")
        elif filtro == "fecha":
            # Las fechas se guardan como texto ISO ('AAAA-MM-DD HH:MM:SS'):
            # la comparación de texto respeta el orden cronológico.
            d_desde, d_hasta = valor
            if d_desde:
                condiciones.append(f"{_q(col)} >= ?")
                parametros.append(d_desde.isoformat())
            if d_hasta:
                condiciones.append(f"{_q(col)} < ?")
                parametros.append((d_hasta + timedelta(days=1)).isoformat())

    # Búsqueda libre: todas las columnas menos las cifradas (buscar dentro de
    # un texto cifrado no tiene sentido y sería una forma de sondearlo).
    if search:
        buscables = [c for c in columnas if not _es_cifrada(c)]
        if buscables:
            condiciones.append("(" + " OR ".join(f"CAST({_q(c)} AS TEXT) LIKE ?" for c in buscables) + ")")
            parametros.extend([f"%{search}%"] * len(buscables))

    clausula = (" WHERE " + " AND ".join(condiciones)) if condiciones else ""
    return clausula, parametros, no_aplicados


def _validar_pagina(limit: int, offset: int) -> None:
    if not 1 <= limit <= LIMITE_MAXIMO:
        raise HTTPException(status_code=400, detail=f"El tamaño de página va de 1 a {LIMITE_MAXIMO}.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="El desplazamiento no puede ser negativo.")


# ─── Origen: Base (tránsito) ─────────────────────────────────────────────────

@router.get("/api/db-viewer/query")
def execute_query(
    db_name: str = Query(...),
    table: str = Query(...),
    limit: int = 50,
    offset: int = 0,
    search: str = Query(None),
    estado: str = Query(None),
    patente: str = Query(None),
    envio: str = Query(None),
    desde: str = Query(None),
    hasta: str = Query(None),
    orden: str = Query(None),
    sentido: str = Query(None),
    _: None = Depends(verify_dashboard_auth)
):
    """
    Filas de una tabla, lo más reciente primero (rowid descendente) o en el
    orden pedido. Incluye rowid para la edición. Las columnas *_enc salen
    como "(cifrado)".
    """
    db_path = _ruta_de_base(db_name)
    _validar_pagina(limit, offset)
    filtros = _filtros_pedidos(estado, patente, envio, desde, hasta)

    conn = _conectar_lectura(db_path)
    try:
        tabla = _validar_tabla(conn, table)
        columnas = _columnas_reales(conn, tabla)
        orden_ok = _orden_pedido(orden, sentido, columnas)
        clausula, parametros, no_aplicados = _where(columnas, filtros, search)

        # Antes no había ORDER BY: la primera página eran las filas MÁS VIEJAS
        # de la tabla, justo lo que el operador no busca (v1.9.8).
        cursor = conn.execute(
            f"SELECT rowid, * FROM {_q(tabla)}{clausula}{_order_by(orden_ok)} LIMIT ? OFFSET ?",
            parametros + [limit, offset],
        )
        filas = [[fila[0]] + _ocultar(columnas, fila[1:]) for fila in cursor.fetchall()]
        total = conn.execute(f"SELECT COUNT(*) FROM {_q(tabla)}{clausula}", parametros).fetchone()[0]
        tipos = {c[1]: (c[2] or "") for c in conn.execute(f"PRAGMA table_info({_q(tabla)})")}
        es_cola = tabla in TABLAS_COLA

        conteo = None
        if es_cola and "status" in columnas:
            conteo = {str(e): n for e, n in conn.execute(
                f"SELECT {_q('status')}, COUNT(*) FROM {_q(tabla)}{clausula} GROUP BY {_q('status')}", parametros)}

        return {
            "origen": "base",
            "columns": ["__rowid__"] + columnas,
            "rows": filas,
            "total": total,
            "limit": limit,
            "offset": offset,
            "filtros_no_aplicados": no_aplicados,
            "cifradas": [c for c in columnas if _es_cifrada(c)],
            # Tipo declarado de cada columna: el editor lo muestra (v1.9.9).
            "tipos": tipos,
            "editable": _es_editable(db_name, tabla),  # El frontend muestra el modo edición solo si es True
            # v1.9.11
            "cola": es_cola,
            "orden": {"columna": orden_ok[0], "sentido": orden_ok[1]} if orden_ok else None,
            "columnas_json": _columnas_json(columnas, tipos),
            "conteo_por_estado": conteo,
            "sin_posicion": _sin_posicion(columnas, [f[1:] for f in filas], _proveedor_de_base(db_name)) if es_cola else None,
            "tope_exportacion": TOPE_EXPORTACION,
        }
    except HTTPException:
        raise
    except sqlite3.Error as e:
        logger.warning(f"Excepción capturada en db_viewer: {e}")
        return {"error": str(e)}
    finally:
        conn.close()


# ─── Origen: Respaldo (procesados) ───────────────────────────────────────────

def _raiz_respaldos() -> str:
    from app.api.routers.exports import BACKUP_DIR
    return os.path.realpath(BACKUP_DIR)


def _columnas_respaldo() -> list[str]:
    # Las de la descarga de enviados, que replican el registro del respaldo
    # (processor.evento_a_registro_respaldo), sin la columna 'origen'.
    from app.api.routers.exports import COLUMNAS_ENVIADOS
    return [c for c in COLUMNAS_ENVIADOS if c != "origen"]


def _carpeta_integracion(integracion: str) -> str:
    """
    db/backups_diarios/{proveedor}_{entorno}, validada: el nombre tiene que ser
    una carpeta que existe ahí, y la ruta resuelta no puede salir de la raíz.
    """
    from app.api.routers.exports import _SEGMENTO_VALIDO
    raiz = _raiz_respaldos()
    if not integracion or not _SEGMENTO_VALIDO.match(integracion) or "_" not in integracion:
        raise HTTPException(status_code=400, detail="Integración de respaldo inválida.")
    existentes = set(os.listdir(raiz)) if os.path.isdir(raiz) else set()
    if integracion not in existentes:
        raise HTTPException(status_code=404, detail=f"No hay respaldos de '{integracion}'.")
    carpeta = os.path.realpath(os.path.join(raiz, integracion))
    if not carpeta.startswith(raiz + os.sep):
        raise HTTPException(status_code=400, detail="Ruta de respaldo fuera de db/backups_diarios/.")
    return carpeta


def _archivos_de_respaldo(carpeta: str, d_desde: date, d_hasta: date) -> list[str]:
    """
    procesados_*.jsonl del rango, del más nuevo al más viejo. El archivo lleva
    el día de la PURGA, no el del evento: se leen días de más hacia adelante y
    después se filtra por created_at (ver exports.MARGEN_DIAS_PURGA).
    """
    from app.api.routers.exports import MARGEN_DIAS_PURGA, _archivos_por_dia, _dias_del_rango
    raiz = _raiz_respaldos()
    dias = list(_dias_del_rango(d_desde, d_hasta + timedelta(days=MARGEN_DIAS_PURGA)))
    rutas = []
    for ruta in reversed(_archivos_por_dia(carpeta, "procesados", dias)):
        if os.path.realpath(ruta).startswith(raiz + os.sep):
            rutas.append(ruta)
    return rutas


def _lineas_al_reves(ruta: str, bloque: int = 64 * 1024):
    """
    Las líneas de un archivo de la última a la primera, leyendo de a bloques
    desde el final: nunca carga el archivo entero (un día de respaldo a caudal
    de certificación son gigas). En UTF-8 el byte \\n no aparece dentro de un
    carácter multibyte, así que cortar por él es seguro.
    """
    with open(ruta, "rb") as f:
        f.seek(0, os.SEEK_END)
        posicion = f.tell()
        resto = b""
        while posicion > 0:
            leer = min(bloque, posicion)
            posicion -= leer
            f.seek(posicion)
            lineas = (f.read(leer) + resto).split(b"\n")
            resto = lineas[0]
            for linea in reversed(lineas[1:]):
                if linea.strip():
                    yield linea.rstrip(b"\r")   # en Windows el respaldo se escribe con \r\n
        if resto.strip():
            yield resto.rstrip(b"\r")


def _registro_pasa(registro: dict, filtros: dict, d_desde: date, d_hasta: date) -> bool:
    from app.api.routers.exports import _en_rango
    if not _en_rango(registro.get("created_at"), d_desde, d_hasta):
        return False
    if "estado" in filtros and registro.get("status") != filtros["estado"]:
        return False
    for filtro, clave in (("patente", "chassis"), ("envio", "shipment")):
        if filtro in filtros and filtros[filtro].lower() not in str(registro.get(clave) or "").lower():
            return False
    return True


def _registros_de_respaldo(integracion: str, desde: str, hasta: str, filtros: dict):
    """
    (columnas, generador de filas del más reciente al más viejo). El rango es
    obligatorio y respeta el tope de días de las descargas (export_max_days).
    """
    from app.api.routers.exports import _parsear_rango
    carpeta = _carpeta_integracion(integracion)
    if not desde or not hasta:
        raise HTTPException(status_code=400, detail="El respaldo se consulta por rango de fechas: indicá desde y hasta.")
    d_desde, d_hasta, _ = _parsear_rango(desde, hasta)
    columnas = _columnas_respaldo()
    rutas = _archivos_de_respaldo(carpeta, d_desde, d_hasta)

    def filas():
        for ruta in rutas:
            try:
                for linea in _lineas_al_reves(ruta):
                    try:
                        registro = json.loads(linea)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue   # línea cortada por una escritura interrumpida
                    if isinstance(registro, dict) and _registro_pasa(registro, filtros, d_desde, d_hasta):
                        yield _ocultar(columnas, [registro.get(c) for c in columnas])
            except OSError as e:
                logger.warning(f"No se pudo leer el respaldo {ruta}: {e}")

    return columnas, filas


def _recorrer_respaldo(filas, columnas: list[str], orden: tuple[str, str] | None, desde: int, cuantas: int):
    """
    (filas desde..desde+cuantas, total, conteo por estado) en una sola pasada.
    Sin orden pedido, en el orden del respaldo (lo más reciente primero). Con
    orden, en memoria quedan como mucho desde+cuantas filas (heapq), nunca el
    respaldo entero. A igualdad de valor se respeta el orden del respaldo,
    como el rowid en la base.
    """
    i_estado = columnas.index("status")
    conteo: dict[str, int] = {}
    total = 0

    def contadas():
        nonlocal total
        for fila in filas():
            total += 1
            e = str(fila[i_estado])
            conteo[e] = conteo.get(e, 0) + 1
            yield fila

    if orden is None:
        it = contadas()
        elegidas = list(islice(it, desde, desde + cuantas))
        for _ in it:
            pass
    else:
        i = columnas.index(orden[0])
        elegir = heapq.nsmallest if orden[1] == "asc" else heapq.nlargest
        elegidas = elegir(desde + cuantas, contadas(), key=lambda f: _clave_de_orden(f[i]))[desde:]
    return elegidas, total, conteo


def _proveedor_de_integracion(integracion: str) -> str:
    return integracion.rsplit("_", 1)[0]


@router.get("/api/db-viewer/respaldos")
def listar_respaldos(_: None = Depends(verify_dashboard_auth)):
    """Integraciones con respaldo de procesados y el tope de días por consulta."""
    from app.core import config_cache
    raiz = _raiz_respaldos()
    integraciones = []
    if os.path.isdir(raiz):
        for nombre in sorted(os.listdir(raiz)):
            carpeta = os.path.join(raiz, nombre)
            if not os.path.isdir(carpeta) or "_" not in nombre:
                continue
            dias = sorted(
                os.path.basename(r)[len("procesados_"):-len(".jsonl")]
                for r in glob.glob(os.path.join(carpeta, "*", "procesados_*.jsonl"))
            )
            integraciones.append({
                "integracion": nombre,
                "primer_dia": dias[0] if dias else None,
                "ultimo_dia": dias[-1] if dias else None,
                "archivos": len(dias),
            })
    tope = getattr(config_cache.get_settings(), "export_max_days", 7) or 7
    return {"integraciones": integraciones, "max_dias": tope, "estados": list(ESTADOS_DE_EVENTO)}


@router.get("/api/db-viewer/respaldo")
def consultar_respaldo(
    integracion: str = Query(...),
    desde: str = Query(None),
    hasta: str = Query(None),
    limit: int = 50,
    offset: int = 0,
    estado: str = Query(None),
    patente: str = Query(None),
    envio: str = Query(None),
    orden: str = Query(None),
    sentido: str = Query(None),
    _: None = Depends(verify_dashboard_auth),
):
    """
    Una página del respaldo de procesados, lo más reciente primero o en el
    orden pedido. Se recorre en streaming: en memoria solo queda la página
    pedida (con orden, hasta offset+limit filas).
    """
    _validar_pagina(limit, offset)
    filtros = _filtros_pedidos(estado, patente, envio, None, None)
    columnas, filas = _registros_de_respaldo(integracion, desde, hasta, filtros)
    orden_ok = _orden_pedido(orden, sentido, columnas)

    pagina, total, conteo = _recorrer_respaldo(filas, columnas, orden_ok, offset, limit)
    return {
        "origen": "respaldo",
        "columns": columnas,
        "rows": pagina,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filtros_no_aplicados": [],
        "cifradas": [],
        "editable": False,
        # v1.9.11
        "cola": True,
        "orden": {"columna": orden_ok[0], "sentido": orden_ok[1]} if orden_ok else None,
        "columnas_json": _columnas_json(columnas),
        "conteo_por_estado": conteo,
        "sin_posicion": _sin_posicion(columnas, pagina, _proveedor_de_integracion(integracion)),
        "tope_exportacion": TOPE_EXPORTACION,
    }


# ─── Descarga CSV de la vista ────────────────────────────────────────────────

def _celda_csv(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return "true" if valor else "false"
    if isinstance(valor, bytes):
        return valor.hex()
    return str(valor)


def _nombre_seguro(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", texto).strip("_") or "vista"


def _celda_xlsx(valor):
    """Números como número; el resto como el CSV (JSON, bytes, booleanos)."""
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return valor
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False)
    return _celda_csv(valor)


def _aviso_de_recorte(exportadas: int, total: int) -> str:
    return (f"AVISO: la vista tiene {total:,} filas y se exportan las primeras {exportadas:,} "
            f"en el orden elegido. Acotá los filtros para exportar el resto.")


@router.get("/api/db-viewer/descargar")
def descargar_vista(
    request: Request,
    origen: str = Query("base"),
    db_name: str = Query(None),
    table: str = Query(None),
    integracion: str = Query(None),
    search: str = Query(None),
    estado: str = Query(None),
    patente: str = Query(None),
    envio: str = Query(None),
    desde: str = Query(None),
    hasta: str = Query(None),
    orden: str = Query(None),
    sentido: str = Query(None),
    columnas: str = Query(None),
    formato: str = Query("csv"),
    _auth=Depends(verify_dashboard_auth),
):
    """
    CSV o Excel de lo que muestra el visor: los mismos filtros, el mismo orden
    y las columnas elegidas, todas las páginas hasta TOPE_EXPORTACION filas.
    Si se recorta, lo dicen la primera línea del archivo y los encabezados
    X-Visor-Filas / X-Visor-Recortado. Las columnas *_enc salen como
    "(cifrado)". El CSV de la base va en streaming.
    """
    from app.api.routers.exports import csv_para_excel, xlsx_desde_filas
    from app.core.auditor import log_admin_action

    formato = (formato or "").lower()
    if formato not in FORMATOS_EXPORTACION:
        raise HTTPException(status_code=400, detail="'formato' tiene que ser csv o xlsx.")
    filtros_web = {"estado": estado, "patente": patente, "envio": envio, "desde": desde, "hasta": hasta}
    if origen == "base":
        db_path = _ruta_de_base(db_name)
        filtros = _filtros_pedidos(estado, patente, envio, desde, hasta)
        conn = _conectar_lectura(db_path)
        try:
            tabla = _validar_tabla(conn, table)
            todas = _columnas_reales(conn, tabla)
            orden_ok = _orden_pedido(orden, sentido, todas)
            proyeccion = _seleccionar_proyeccion(columnas, todas)
            clausula, parametros, _ = _where(todas, filtros, search)
            total = conn.execute(f"SELECT COUNT(*) FROM {_q(tabla)}{clausula}", parametros).fetchone()[0]
        except Exception:
            conn.close()
            raise

        def filas():
            try:
                cursor = conn.execute(
                    f"SELECT * FROM {_q(tabla)}{clausula}{_order_by(orden_ok)} LIMIT ?",
                    parametros + [TOPE_EXPORTACION])
                while True:
                    lote = cursor.fetchmany(500)
                    if not lote:
                        break
                    for fila in lote:
                        yield _ocultar(todas, fila)
            finally:
                conn.close()

        base_nombre = f"visor_{_nombre_seguro(db_name)}_{_nombre_seguro(tabla)}"
        detalle = {"origen": origen, "db": db_name, "tabla": tabla, "search": search, **filtros_web}
    elif origen == "respaldo":
        filtros = _filtros_pedidos(estado, patente, envio, None, None)
        todas, generador = _registros_de_respaldo(integracion, desde, hasta, filtros)
        orden_ok = _orden_pedido(orden, sentido, todas)
        proyeccion = _seleccionar_proyeccion(columnas, todas)
        if orden_ok is None:
            # Sin orden: una pasada para contar y otra que se escribe en
            # streaming. Con orden, las primeras TOPE en memoria (heapq).
            total = sum(1 for _ in generador())

            def filas():
                return islice(generador(), TOPE_EXPORTACION)
        else:
            elegidas, total, _ = _recorrer_respaldo(generador, todas, orden_ok, 0, TOPE_EXPORTACION)

            def filas():
                return iter(elegidas)

        base_nombre = f"visor_respaldo_{_nombre_seguro(integracion)}_{desde}_a_{hasta}"
        detalle = {"origen": origen, "integracion": integracion, **filtros_web}
    else:
        raise HTTPException(status_code=400, detail="Origen desconocido: 'base' o 'respaldo'.")

    exportadas = min(total, TOPE_EXPORTACION)
    recortado = total > TOPE_EXPORTACION
    nombres = [todas[i] for i in proyeccion]
    detalle.update({"formato": formato, "orden": orden_ok and list(orden_ok), "columnas": nombres,
                    "filas": exportadas, "recortado": recortado})
    log_admin_action("descarga_visor_bd", detalle, request, getattr(_auth, "username", "desconocido"))

    convertir = _celda_csv if formato == "csv" else _celda_xlsx

    def datos():
        if recortado:
            yield [_aviso_de_recorte(exportadas, total)] + [""] * (len(nombres) - 1)
        for fila in filas():
            yield [convertir(fila[i]) for i in proyeccion]

    cabeceras = {"Content-Disposition": f'attachment; filename="{base_nombre}.{formato}"',
                 "X-Visor-Filas": str(exportadas), "X-Visor-Recortado": "true" if recortado else "false"}
    if formato == "csv":
        # Mismo CSV que las exportaciones (exports.csv_para_excel): BOM y ';'.
        return StreamingResponse(csv_para_excel(nombres, datos()),
                                 media_type="text/csv; charset=utf-8", headers=cabeceras)
    return Response(xlsx_desde_filas(nombres, list(datos()), "Visor"), headers=cabeceras,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ─── Edición de celdas (v1.9.9, 3.1) ────────────────────────────────────────

def _info_columna(conn: sqlite3.Connection, tabla: str, columna: str) -> dict:
    """Tipo declarado, NOT NULL y clave primaria de la columna, del esquema real."""
    for _, nombre, tipo, notnull, _defecto, pk in conn.execute(f"PRAGMA table_info({_q(tabla)})"):
        if nombre == columna:
            return {"tipo": (tipo or "").upper(), "notnull": bool(notnull), "pk": bool(pk)}
    raise HTTPException(status_code=400, detail="La columna no existe en esta tabla")


_VERDADEROS, _FALSOS = {"1", "true", "verdadero", "si", "sí"}, {"0", "false", "falso", "no"}


def _convertir(valor, info: dict, columna: str):
    """
    El valor nuevo, validado y convertido según el tipo declarado de la columna.
    Devuelve (valor para SQLite, valor para el modelo). Lanza 400 con el motivo.
    """
    if info["pk"]:
        raise HTTPException(status_code=400, detail=f"'{columna}' es la clave primaria: no se edita.")
    if valor is None:
        if info["notnull"]:
            raise HTTPException(status_code=400, detail=f"'{columna}' no admite NULL.")
        return None, None
    texto = str(valor).strip()
    tipo = info["tipo"]
    try:
        if "BOOL" in tipo:
            if texto.lower() in _VERDADEROS:
                return 1, True
            if texto.lower() in _FALSOS:
                return 0, False
            raise ValueError("se espera true/false o 1/0")
        if "INT" in tipo:
            if not re.fullmatch(r"-?\d+", texto):
                raise ValueError("se espera un número entero")
            return int(texto), int(texto)
        if any(t in tipo for t in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
            numero = float(texto.replace(",", "."))
            return numero, numero
        if "JSON" in tipo:
            objeto = json.loads(texto)
            return json.dumps(objeto, ensure_ascii=False), objeto
        if "TIME" in tipo:
            fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
            return fecha.isoformat(sep=" "), fecha
        if "DATE" in tipo:
            # v1.9.11: una columna DATE guarda AAAA-MM-DD, como la escribe
            # SQLAlchemy. Antes quedaba "AAAA-MM-DD 00:00:00": otro texto para
            # el mismo día, que el índice único de daily_stats no veía repetido.
            dia = date.fromisoformat(texto)
            return dia.isoformat(), dia
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"El valor no es válido para '{columna}' (tipo {tipo or 'sin tipo'}): {e}",
        )
    return str(valor), str(valor)


@router.post("/api/db-viewer/update_cell")
def update_cell(body: CellUpdateRequest, request: Request, cred=Depends(verify_dashboard_auth)):
    """
    Edita una celda de una tabla permitida, con el valor que escribió el
    operador (v1.9.9: antes se mandaba el valor original y no cambiaba nada).

    - Revalida DASHBOARD_PASSWORD.
    - Solo las tablas de EDITABLE_TABLES de system_config_global.db; la cola
      (normalized_rc_events) nunca, ni las columnas *_enc (403).
    - El valor se valida contra el tipo declarado de la columna en la base.
    - provider_config se escribe por la sesión de SQLAlchemy: un cambio de
      use_mock o is_active sale con el MISMO aviso que el panel de
      configuración (app/core/modo_simulado.py). B-13.
    - Queda en el registro de acciones administrativas: usuario, base, tabla,
      fila, columna, valor anterior y nuevo.
    """
    from app.core import config_cache, modo_simulado
    from app.core.auditor import log_admin_action

    correct_pass = get_dashboard_password()
    if not correct_pass or not secrets.compare_digest(body.password.encode(), correct_pass.encode()):
        raise HTTPException(status_code=403, detail="Contraseña de administrador incorrecta")

    # v1.9.11: la cola, con su motivo. Editarla a mano puede mandar a RC algo
    # que nadie midió (ni el contrato ni el diferencial).
    if body.table in TABLAS_COLA:
        raise HTTPException(
            status_code=403,
            detail=f"La cola ({body.table}) es de solo lectura: editarla a mano puede mandar a RC "
                   "algo que nadie midió."
        )
    # Whitelist estricta — rechazo explícito de tablas operativas
    if body.table not in EDITABLE_TABLES:
        raise HTTPException(
            status_code=403,
            detail=f"La tabla '{body.table}' es de solo lectura. Edición no permitida."
        )
    if not re.match(r'^[a-zA-Z0-9_]+$', body.table):
        raise HTTPException(status_code=400, detail="Nombre de tabla inválido")
    if not re.match(r'^[a-zA-Z0-9_]+$', body.column_name):
        raise HTTPException(status_code=400, detail="Nombre de columna inválido")
    # Una columna cifrada se ve como "(cifrado)": editarla escribiría ese texto
    # (o un valor sin cifrar) encima de la credencial. Se cambia desde la
    # configuración, que la cifra (v1.9.8).
    if _es_cifrada(body.column_name):
        raise HTTPException(
            status_code=403,
            detail=f"'{body.column_name}' está cifrada: se cambia desde la configuración, no desde el visor."
        )

    safe_db_path = _resolve_db_path(body.db_name)
    if not safe_db_path or not os.path.exists(safe_db_path):
        raise HTTPException(status_code=400, detail="Ruta de base de datos inválida")
    if not _es_editable(body.db_name, body.table):
        raise HTTPException(
            status_code=403,
            detail=f"En '{body.db_name}' no se edita: las tablas de configuración se editan en {BASE_EDITABLE}."
        )

    conn = sqlite3.connect(safe_db_path)
    try:
        if body.table not in _tablas_reales(conn):
            raise HTTPException(status_code=400, detail="La tabla no existe en esta base")
        info = _info_columna(conn, body.table, body.column_name)
        valor_sql, valor_modelo = _convertir(body.new_value, info, body.column_name)
        fila = conn.execute(f"SELECT {_q(body.column_name)} FROM {_q(body.table)} WHERE rowid = ?",
                            (body.rowid,)).fetchone()
        if fila is None:
            raise HTTPException(status_code=404, detail="No se encontró el registro para actualizar")
        anterior = fila[0]

        usuario = getattr(cred, "username", "desconocido")
        es_config = (body.table == "provider_config"
                     and body.db_name.replace("\\", "/") == "system_config_global.db")
        if es_config:
            # Por la sesión: el aviso de modo simulado (y de activación) sale
            # igual que desde el panel de configuración.
            conn.close()
            from app.database import get_session
            from app.models.config_models import ProviderConfig
            with modo_simulado.usuario(usuario, "visor de BD"):
                db = get_session("system_config", "global")
                try:
                    conf = db.get(ProviderConfig, body.rowid)
                    if conf is None or not hasattr(conf, body.column_name):
                        raise HTTPException(status_code=404, detail="No se encontró el registro para actualizar")
                    setattr(conf, body.column_name, valor_modelo)
                    db.commit()
                finally:
                    db.close()
        else:
            cursor = conn.execute(f"UPDATE {_q(body.table)} SET {_q(body.column_name)} = ? WHERE rowid = ?",
                                  (valor_sql, body.rowid))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="No se encontró el registro para actualizar")
            conn.commit()
        config_cache.invalidate()

        # v1.9.11: usuario e id también dentro del detalle, además de user=.
        log_admin_action("edicion_celda_visor", {
            "usuario": usuario, "base": body.db_name, "tabla": body.table, "id": body.rowid,
            "fila": body.rowid, "columna": body.column_name, "antes": anterior, "despues": valor_sql,
        }, request, usuario)
        return {"status": "success", "message": "Celda actualizada correctamente",
                "antes": anterior, "despues": valor_sql}
    except HTTPException:
        raise
    except (sqlite3.IntegrityError, IntegrityErrorSQLA) as e:
        # p. ej. el índice único de daily_stats (v1.9.11): el valor nuevo
        # repetiría otra fila. Nada se escribió.
        raise HTTPException(status_code=409, detail=f"El valor choca con otra fila de la tabla: {e}")
    except Exception as e:
        logger.error(f"Error al actualizar celda: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
