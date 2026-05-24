import os
import logging
from logging.handlers import TimedRotatingFileHandler
import json

# Directorio raíz para auditoría
AUDIT_DIR = "audit"

def setup_provider_logger(provider: str) -> logging.Logger:
    """
    Configura y devuelve un logger para un proveedor específico.
    Guarda los logs en formato JSONL y rota diariamente a medianoche,
    manteniendo 30 días de historial.
    """
    logger = logging.getLogger(f"auditor_{provider}")
    
    # Si el logger ya tiene handlers, significa que ya fue configurado (evita duplicados en la misma ejecución)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False  # No propagar al root logger

    # Crear directorio del proveedor si no existe
    provider_dir = os.path.join(AUDIT_DIR, provider)
    os.makedirs(provider_dir, exist_ok=True)

    # El archivo base será ej. audit/schmitz/schmitz.jsonl
    # TimedRotatingFileHandler añadirá la fecha al rotar.
    log_file = os.path.join(provider_dir, f"{provider}.jsonl")

    # Rotar a medianoche (midnight), mantener 30 días (backupCount=30)
    handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )

    # Definir la extensión y formato para los archivos rotados
    # Ej: schmitz_2026-05-23.jsonl (ajustando el namer)
    handler.suffix = "%Y-%m-%d"
    
    def namer(default_name):
        # default_name viene como audit/schmitz/schmitz.jsonl.2026-05-23
        # Queremos convertirlo a audit/schmitz/schmitz_2026-05-23.jsonl
        dir_name, file_name = os.path.split(default_name)
        base_name = file_name.replace(".jsonl.", "_") + ".jsonl"
        return os.path.join(dir_name, base_name)
        
    handler.namer = namer

    # Formateador simple que solo imprime el mensaje (que será el JSON crudo o transformado)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    
    logger.addHandler(handler)
    return logger

def audit_event(provider: str, payload: dict):
    """
    Guarda un evento en el archivo de auditoría del proveedor en formato JSONL.
    """
    logger = setup_provider_logger(provider)
    # Convertir a JSON string, asegurar que sea una sola línea
    json_str = json.dumps(payload, ensure_ascii=False)
    logger.info(json_str)
