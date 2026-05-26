import os
import logging
from logging.handlers import TimedRotatingFileHandler
import json
from datetime import datetime, timezone

# Directorio raíz para auditoría
AUDIT_DIR = "audit"

def setup_provider_logger(provider: str, format_type: str = "jsonl") -> logging.Logger:
    """
    Configura y devuelve un logger específico. Mantiene historial indefinido (backupCount=0).
    format_type debe ser 'jsonl' o 'log'.
    """
    logger_name = f"auditor_{provider}_{format_type}"
    logger = logging.getLogger(logger_name)
    
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False 

    provider_dir = os.path.join(AUDIT_DIR, provider)
    os.makedirs(provider_dir, exist_ok=True)

    log_file = os.path.join(provider_dir, f"{provider}.{format_type}")

    handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=0,
        encoding="utf-8"
    )

    handler.suffix = "%Y-%m-%d"
    
    def namer(default_name):
        dir_name, file_name = os.path.split(default_name)
        base_name = file_name.replace(f".{format_type}.", "_") + f".{format_type}"
        return os.path.join(dir_name, base_name)
        
    handler.namer = namer

    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    
    logger.addHandler(handler)
    return logger

def audit_event(provider: str, payload: dict):
    """
    Guarda el evento simultáneamente en formato estricto (.jsonl) y formato humano (.log)
    """
    logger_jsonl = setup_provider_logger(provider, format_type="jsonl")
    logger_human = setup_provider_logger(provider, format_type="log")
    
    # Envolver el payload con metadatos útiles
    audit_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "payload": payload
    }
    
    # 1. Guardado JSONL estricto para máquinas
    json_str_strict = json.dumps(audit_record, ensure_ascii=False)
    logger_jsonl.info(json_str_strict)
    
    # 2. Guardado LOG formateado para humanos
    json_str_human = json.dumps(audit_record, ensure_ascii=False, indent=4)
    logger_human.info(json_str_human + "\n" + "-"*80)
