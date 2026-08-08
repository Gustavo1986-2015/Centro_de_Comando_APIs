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


# Límite por defecto: 12.000 req/min = 200 req/s.
#
# Dimensionado sobre la prueba de certificación de Schmitz: 80 eventos/segundo
# sostenidos durante 24 horas (4.800/min). El default deja 2,5x de margen sobre
# ese pico para absorber ráfagas sin rechazar tráfico legítimo, y aun así corta
# un reenvío descontrolado.
#
# Un límite por debajo del volumen real del proveedor no protege nada: hace
# fallar la integración. Antes estaba en 600/min, que habría rechazado el 87%
# del tráfico de esa prueba.
_DEFAULT_LIMIT = 12000


# Caché del límite configurado por proveedor en la base.
# Consultar la BD en cada petición sería inviable con caudales de decenas de
# peticiones por segundo, y el valor cambia solo cuando alguien lo edita en el
# panel: 30 segundos de desfase es un intercambio razonable.
_DB_LIMIT_TTL = 30
_db_limit_cache: dict[str, tuple[float, int | None]] = {}


def _limit_desde_db(provider: str) -> int | None:
    """Límite configurado en el panel para este proveedor, o None si no hay."""
    ahora = time.time()
    cacheado = _db_limit_cache.get(provider)
    if cacheado and cacheado[0] > ahora:
        return cacheado[1]

    valor = None
    try:
        from app.database import get_session
        from app.models.config_models import ProviderConfig
        db = get_session("system_config", "global")
        try:
            fila = (
                db.query(ProviderConfig.rate_limit_per_min)
                .filter(ProviderConfig.provider_name == provider.lower())
                .filter(ProviderConfig.rate_limit_per_min.isnot(None))
                .first()
            )
            if fila and fila[0]:
                valor = max(int(fila[0]), 1)
        finally:
            db.close()
    except Exception:
        # Ante cualquier problema con la base se cae al límite por entorno:
        # el control de caudal no debe depender de que la BD responda.
        valor = None

    _db_limit_cache[provider] = (ahora + _DB_LIMIT_TTL, valor)
    return valor


def invalidate_limit_cache(provider: str | None = None):
    """Fuerza la relectura tras guardar la configuración desde el panel."""
    if provider:
        _db_limit_cache.pop(provider.lower(), None)
    else:
        _db_limit_cache.clear()


def _limit(provider: str | None = None) -> int:
    """
    Peticiones permitidas por ventana para una integración.

    Precedencia:
      1. Lo configurado en el panel para ese proveedor (columna rate_limit_per_min)
      2. Variable de entorno por proveedor:  WEBHOOK_RATE_LIMIT_SCHMITZ=20000
      3. Variable de entorno global:         WEBHOOK_RATE_LIMIT_PER_MIN=12000
      4. Default del código
    """
    if provider:
        de_db = _limit_desde_db(provider)
        if de_db:
            return de_db

        especifico = os.getenv(f"WEBHOOK_RATE_LIMIT_{provider.upper()}")
        if especifico:
            try:
                return max(int(especifico), 1)
            except (TypeError, ValueError):
                pass

    try:
        return max(int(os.getenv("WEBHOOK_RATE_LIMIT_PER_MIN", str(_DEFAULT_LIMIT))), 1)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def _key(provider: str, env: str) -> str:
    return f"{(provider or 'unknown').lower()}|{(env or 'prod').lower()}"


def check_rate_limit(provider: str, env: str) -> tuple[bool, int, int]:
    """
    Registra una petición y decide si se permite.

    Retorna (permitida, restantes, retry_after_segundos).
    Cuando se supera el límite, retry_after indica cuántos segundos faltan para
    que la petición más antigua salga de la ventana.
    """
    limit = _limit(provider)
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
    limit = _limit(provider)
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
    _db_limit_cache.clear()
