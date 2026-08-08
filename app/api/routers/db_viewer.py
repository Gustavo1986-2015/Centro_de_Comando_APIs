import os
import re
import sqlite3
import glob
import logging
import secrets
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

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
_TABLAS_INTERNAS = {"sqlite_sequence"}


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
        })
    return result


def _es_huerfana(rel: str) -> bool:
    """
    Determina si un archivo .db corresponde al esquema vigente.

    Esquema actual (app/database.py):
      system_config_global.db      archivo maestro en la raíz
      {provider}/{env}.db          colas operativas por proveedor

    Cualquier otra forma es residual: bases de esquemas viejos o generadas por
    corridas de tests. No se borran automáticamente (podrían tener datos que el
    operador quiera rescatar), solo se marcan.
    """
    if rel == "system_config_global.db":
        return False
    partes = rel.split("/")
    if len(partes) == 2 and partes[1] in ("prod.db", "test.db"):
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
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        nombres = [row[0] for row in cursor.fetchall()]

        # Conteo por tabla: permite ver de un vistazo cuáles tienen datos sin
        # tener que consultarlas una por una.
        tables = []
        for n in nombres:
            filas = None
            if re.match(r'^[a-zA-Z0-9_]+$', n):
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {n}")
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

@router.get("/api/db-viewer/query")
def execute_query(
    db_name: str = Query(...), 
    table: str = Query(...), 
    limit: int = 50, 
    offset: int = 0,
    search: str = Query(None),
    _: None = Depends(verify_dashboard_auth)
):
    """Retorna los datos y las columnas de una tabla seleccionada. Incluye rowid para edición."""
    db_path = _resolve_db_path(db_name)
    if not db_path:
        raise HTTPException(status_code=400, detail="Ruta de base de datos inválida")
    if not os.path.exists(db_path):
        return {"error": "Base de datos no encontrada"}
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        if not re.match(r'^[a-zA-Z0-9_]+$', table):
            return {"error": "Nombre de tabla inválido"}
        
        # Búsqueda genérica: antes solo funcionaba en normalized_rc_events y
        # sobre dos columnas fijas. Ahora recorre todas las columnas de texto de
        # cualquier tabla, así el filtro sirve igual para el diccionario, la
        # configuración de proveedores o cualquier tabla futura.
        search_clause = ""
        search_params = []
        if search:
            cursor.execute(f"PRAGMA table_info({table})")
            # Los nombres vienen del PRAGMA (no del usuario), pero se validan
            # igual antes de interpolarlos en el SQL.
            columnas = [
                c[1] for c in cursor.fetchall()
                if re.match(r'^[a-zA-Z0-9_]+$', c[1])
            ]
            if columnas:
                condiciones = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in columnas)
                search_clause = f" WHERE {condiciones}"
                search_params = [f"%{search}%"] * len(columnas)
        
        # Incluir rowid como identificador único universal de SQLite (funciona aunque no haya PK)
        query = f"SELECT rowid, * FROM {table}{search_clause} LIMIT ? OFFSET ?"
        cursor.execute(query, search_params + [limit, offset])
        rows = cursor.fetchall()
        
        # Obtener los nombres de las columnas (prefijado con __rowid__ para el frontend)
        cursor.execute(f"PRAGMA table_info({table})")
        columns = ["__rowid__"] + [col[1] for col in cursor.fetchall()]
        
        # Obtener conteo total
        count_query = f"SELECT COUNT(*) FROM {table}{search_clause}"
        cursor.execute(count_query, search_params)
        total = cursor.fetchone()[0]
        
        return {
            "columns": columns,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "editable": table in EDITABLE_TABLES  # El frontend muestra el modo edición solo si es True
        }
    except Exception as e:
        logger.warning(f"Excepción capturada en db_viewer: {e}")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()

@router.post("/api/db-viewer/update_cell")
def update_cell(body: CellUpdateRequest, _: None = Depends(verify_dashboard_auth)):
    """
    Edita una celda específica de una tabla permitida.
    Requiere revalidar DASHBOARD_PASSWORD para confirmar la operación.
    Las tablas operativas (normalized_rc_events, etc.) son de SOLO LECTURA y siempre serán rechazadas.
    """
    # Ajuste 1 (Claude): Validar con la contraseña real del .env, no con un PIN cosmético
    correct_pass = get_dashboard_password()
    if not secrets.compare_digest(body.password.encode(), correct_pass.encode()):
        raise HTTPException(status_code=403, detail="Contraseña de administrador incorrecta")

    # Ajuste 2 (Claude): Whitelist estricta — rechazo explícito de tablas operativas
    if body.table not in EDITABLE_TABLES:
        raise HTTPException(
            status_code=403,
            detail=f"La tabla '{body.table}' es de solo lectura. Edición no permitida."
        )

    # Validar nombres para prevenir SQL injection
    if not re.match(r'^[a-zA-Z0-9_]+$', body.table):
        raise HTTPException(status_code=400, detail="Nombre de tabla inválido")
    if not re.match(r'^[a-zA-Z0-9_]+$', body.column_name):
        raise HTTPException(status_code=400, detail="Nombre de columna inválido")
        
    safe_db_path = _resolve_db_path(body.db_name)
    if not safe_db_path or not os.path.exists(safe_db_path):
        raise HTTPException(status_code=400, detail="Ruta de base de datos inválida")

    try:
        conn = sqlite3.connect(safe_db_path)
        cursor = conn.cursor()
        
        # En SQLite, 'rowid' identifica la fila física inequívocamente
        sql = f"UPDATE {body.table} SET {body.column_name} = ? WHERE rowid = ?"
        cursor.execute(sql, (body.new_value, body.rowid))
        
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="No se encontró el registro para actualizar")
            
        conn.commit()
        return {"status": "success", "message": "Celda actualizada correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error al actualizar celda: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
    finally:
        if 'conn' in locals():
            conn.close()
