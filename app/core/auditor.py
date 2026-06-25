import os
import json
from datetime import datetime, timezone
import logging
logger = logging.getLogger(__name__)


# Directorio raíz para auditoría
AUDIT_DIR = "audit"

def audit_event(provider: str, payload: dict):
    """
    Guarda el evento crudo entrante en formato estricto (.jsonl) agrupado por mes.
    Ej: audit/schmitz_test/2026-06/crudos_2026-06-23.jsonl
    """
    now = datetime.now()
    audit_record = {
        "timestamp": now.astimezone(timezone.utc).isoformat(),
        "provider": provider,
        "payload": payload
    }
    
    month_str = now.strftime("%Y-%m")
    day_str = now.strftime("%Y-%m-%d")
    
    month_dir = os.path.join(AUDIT_DIR, provider, month_str)
    os.makedirs(month_dir, exist_ok=True)
    
    log_file = os.path.join(month_dir, f"crudos_{day_str}.jsonl")
    json_str_strict = json.dumps(audit_record, ensure_ascii=False)
    
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json_str_strict + "\n")
    except Exception as e:
        logger.warning(f"Excepción silenciada en ejecución: {e}")
        print(f"Error escribiendo auditoria para {provider}: {e}")
