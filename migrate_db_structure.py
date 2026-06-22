"""
migrate_db_structure.py
-----------------------
Script de migración ONE-TIME: mueve las bases de datos de la estructura plana
(db/protrack_test.db) a la nueva estructura en subcarpetas (db/protrack/test.db).

EJECUTAR CON LA APP DETENIDA.
"""
import os
import shutil
import glob

DB_DIR = "./db"

# Archivos que deben quedarse en la raíz (nunca mover)
ROOT_ONLY = {"system_config_global.db"}

# Extensiones a mover junto con el .db principal
SIDE_EXTENSIONS = [".db", ".db-shm", ".db-wal"]

def migrate():
    files = glob.glob(f"{DB_DIR}/*.db")
    moved = []
    skipped = []

    for filepath in files:
        filename = os.path.basename(filepath)

        if filename in ROOT_ONLY:
            skipped.append(f"  [SKIP]  {filename}  (archivo maestro, permanece en raíz)")
            continue

        # Esperamos formato: {provider}_{env}.db
        name_no_ext = filename.replace(".db", "")
        parts = name_no_ext.split("_")
        if len(parts) < 2:
            skipped.append(f"  [SKIP]  {filename}  (formato desconocido)")
            continue

        # Último segmento = env (prod/test), el resto = provider
        env = parts[-1]
        provider = "_".join(parts[:-1])

        # Crear subcarpeta
        target_dir = os.path.join(DB_DIR, provider)
        os.makedirs(target_dir, exist_ok=True)

        # Mover el .db y sus archivos WAL/SHM asociados
        for ext in SIDE_EXTENSIONS:
            src = filepath.replace(".db", ext) if ext != ".db" else filepath
            if not os.path.exists(src):
                continue
            dst = os.path.join(target_dir, f"{env}{ext}")
            shutil.move(src, dst)
            moved.append(f"  [MOVE]  {os.path.relpath(src, DB_DIR)}  →  {provider}/{env}{ext}")

    print("\n=== Migración de Estructura de Base de Datos ===\n")
    for m in moved:
        print(m)
    for s in skipped:
        print(s)
    print(f"\nTotal movidos: {len(moved)} archivos | Omitidos: {len(skipped)} archivos")
    print("\n✅ Migración completada. Puedes encender la app de nuevo.\n")

if __name__ == "__main__":
    migrate()
