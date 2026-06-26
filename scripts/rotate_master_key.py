#!/usr/bin/env python3
"""
Script de rotación de Master Key para SysAdmins.
Este script permite migrar toda la base de datos de una llave vieja a una nueva.

Uso:
  OLD_KEY="gAAAA..." NEW_KEY="gAAAA..." python scripts/rotate_master_key.py
"""
import os
import sys
import sqlite3
from cryptography.fernet import Fernet, InvalidToken

def get_db_path():
    db_path = "./db/system_config_global.db"
    if not os.path.exists(db_path):
        print(f"Error: No se encontró la base de datos en {db_path}")
        sys.exit(1)
    return db_path

def main():
    old_key = os.getenv("OLD_KEY")
    new_key = os.getenv("NEW_KEY")
    
    if not old_key or not new_key:
        print("Error: Debes proveer OLD_KEY y NEW_KEY como variables de entorno.")
        print('Ejemplo: OLD_KEY="key1" NEW_KEY="key2" python scripts/rotate_master_key.py')
        sys.exit(1)
        
    try:
        old_f = Fernet(old_key.encode())
        new_f = Fernet(new_key.encode())
    except Exception as e:
        print(f"Error inicializando llaves Fernet: {e}")
        sys.exit(1)
        
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    print("Iniciando rotación de llaves maestras...")
    
    try:
        cursor.execute("SELECT id, rc_password_enc, fetch_config_enc, webhook_auth_secret_enc, provider_name FROM provider_config")
        rows = cursor.fetchall()
        
        migrated = 0
        for row_id, rc_pass, fetch_cfg, webhook_sec, p_name in rows:
            updates = {}
            
            def rotate_val(val):
                if not val or not val.startswith("gAAAAAB"): return val
                try:
                    pt = old_f.decrypt(val.encode()).decode()
                    return new_f.encrypt(pt.encode()).decode()
                except InvalidToken:
                    print(f"  [!] Advertencia: No se pudo descifrar valor para el proveedor {p_name}")
                    return val
            
            new_rc = rotate_val(rc_pass)
            new_fetch = rotate_val(fetch_cfg)
            new_webhook = rotate_val(webhook_sec)
            
            if new_rc != rc_pass: updates["rc_password_enc"] = new_rc
            if new_fetch != fetch_cfg: updates["fetch_config_enc"] = new_fetch
            if new_webhook != webhook_sec: updates["webhook_auth_secret_enc"] = new_webhook
            
            if updates:
                set_clauses = ", ".join([f"{k} = ?" for k in updates])
                values = list(updates.values()) + [row_id]
                cursor.execute(f"UPDATE provider_config SET {set_clauses} WHERE id = ?", values)
                migrated += 1
                
        if migrated > 0:
            conn.commit()
            print(f"Rotación completada exitosamente. Se actualizaron {migrated} proveedores.")
            print("Importante: Ahora debes actualizar tu archivo .env con la NEW_KEY y reiniciar la aplicación.")
        else:
            print("No se encontraron registros que requirieran rotación (o falló el descifrado).")
            
    except Exception as e:
        print(f"Error fatal durante la rotación: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
