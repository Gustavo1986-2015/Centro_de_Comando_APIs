import os
import re
import sqlite3
import glob
import logging
import secrets
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from app.core.auth import verify_dashboard_auth

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
    # Raíz (system_config_global.db) + subcarpetas de proveedores
    files = glob.glob(f"{db_dir}/*.db") + glob.glob(f"{db_dir}/**/*.db")
    result = []
    for f in sorted(files):
        rel = os.path.relpath(f, db_dir).replace("\\", "/")
        result.append({"name": rel})
    return result

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
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        return {"tables": tables}
    except Exception as e:
        logger.warning(f"Excepción capturada en db_viewer: {e}")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()

@router.get("/api/db-viewer/query")
def execute_query(db_name: str = Query(...), table: str = Query(...), limit: int = 50, offset: int = 0, _: None = Depends(verify_dashboard_auth)):
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
        
        # Incluir rowid como identificador único universal de SQLite (funciona aunque no haya PK)
        cursor.execute(f"SELECT rowid, * FROM {table} LIMIT ? OFFSET ?", (limit, offset))
        rows = cursor.fetchall()
        
        # Obtener los nombres de las columnas (prefijado con __rowid__ para el frontend)
        cursor.execute(f"PRAGMA table_info({table})")
        columns = ["__rowid__"] + [col[1] for col in cursor.fetchall()]
        
        # Obtener conteo total
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
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
    correct_pass = os.getenv("DASHBOARD_PASSWORD", "")
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
