"""
Rate limiting de webhooks entrantes.

Problema que resuelve: los endpoints de ingesta (/Json/Data y
/webhook/dynamic/{provider}) no tienen límite de peticiones. Un proveedor con
un bug de reenvío, un reintento agresivo tras una caída, o tráfico malicioso
pueden saturar el worker y la cola SQLite sin que nada lo contenga.

Diseño:
  - Ventana deslizante en memoria, por integración (provider + env).
  - Sin dependencias externas: el proyecto ya usa módulos propios en app/core
    para estado compartido (state_dedup, provider_health, resync) y agregar
    slowapi obligaría a rebuild de imagen por una funcionalidad de 100 líneas.
  - Transversal: el límite aplica igual a cualquier proveedor. El default es
    generoso y se ajusta por variable de entorno.

Nota sobre el límite por defecto: Schmitz envía ~80 eventos/min en el peor caso
documentado (40 cada 30s). 600/min deja un margen de 7x antes de rechazar nada
legítimo, y aun así corta una ráfaga descontrolada.
"""
import os
import time
import threading
from collections import deque

# { "provider|env": deque[timestamps] }
_HITS: dict[str, deque] = {}
_LOCK = threading.Lock()

WINDOW_SECONDS = 60


def _limit() -> int:
    """Peticiones permitidas por ventana. Configurable sin tocar código."""
    try:
        return max(int(os.getenv("WEBHOOK_RATE_LIMIT_PER_MIN", "600")), 1)
    except (TypeError, ValueError):
        return 600


def _key(provider: str, env: str) -> str:
    return f"{(provider or 'unknown').lower()}|{(env or 'prod').lower()}"


def check_rate_limit(provider: str, env: str) -> tuple[bool, int, int]:
    """
    Registra una petición y decide si se permite.

    Retorna (permitida, restantes, retry_after_segundos).
    Cuando se supera el límite, retry_after indica cuántos segundos faltan para
    que la petición más antigua salga de la ventana.
    """
    limit = _limit()
    now = time.time()
    k = _key(provider, env)

    with _LOCK:
        if k not in _HITS:
            _HITS[k] = deque()
        hits = _HITS[k]

        # Descartar lo que ya salió de la ventana
        cutoff = now - WINDOW_SECONDS
        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= limit:
            retry_after = max(int(hits[0] + WINDOW_SECONDS - now) + 1, 1)
            return False, 0, retry_after

        hits.append(now)
        return True, limit - len(hits), 0


def get_usage(provider: str, env: str) -> dict:
    """Uso actual de una integración, para exponer en el panel de salud."""
    limit = _limit()
    now = time.time()
    k = _key(provider, env)

    with _LOCK:
        hits = _HITS.get(k)
        if not hits:
            return {"used": 0, "limit": limit, "pct": 0}
        cutoff = now - WINDOW_SECONDS
        used = sum(1 for t in hits if t >= cutoff)

    return {"used": used, "limit": limit, "pct": round(used / limit * 100, 1)}


def reset():
    """Solo para tests."""
    with _LOCK:
        _HITS.clear()
