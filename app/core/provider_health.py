"""
Registro de salud de las integraciones (en memoria).

Motivación: cuando el diccionario de un proveedor PULL falla al sincronizar, o
la autenticación deja de funcionar, el síntoma visible son eventos con datos
vacíos que llegan a RC horas después. El operador no tiene forma de ver la causa
sin leer logs del servidor.

Este módulo mantiene el último estado conocido de cada integración para que el
dashboard lo muestre en tiempo real. Es deliberadamente volátil (se reinicia con
el proceso): refleja el estado del worker actual, no un histórico.
"""
import re
import time
import threading
from typing import Any

# { "protrack|prod": {...} }
_HEALTH: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _short_error(raw: str, max_len: int = 60) -> str:
    """
    Extrae la parte útil del mensaje de error de un proveedor.

    Las APIs de tracking suelen envolver la causa real dentro de un JSON
    ({'code': 20001, 'message': 'account or password error'}). Mostrar el texto
    completo en un chip lo vuelve ilegible; mostrar solo "Auth fallando" oculta
    la causa. Esta función busca el campo de mensaje habitual y cae al truncado
    si no lo encuentra. Es genérica: no asume ningún proveedor en particular.
    """
    if not raw:
        return ""
    txt = str(raw)

    # 'message': '...', "msg": "...", 'error': '...'
    m = re.search(r"['\"](?:message|msg|error|detail)['\"]\s*:\s*['\"]([^'\"]+)['\"]", txt)
    if m:
        txt = m.group(1)
    else:
        # Mensaje plano tras un separador tipo ": " al final de la cadena
        parts = [p.strip() for p in txt.split(". ") if p.strip()]
        if parts:
            txt = parts[-1]

    txt = txt.strip()
    return txt if len(txt) <= max_len else txt[: max_len - 1] + "\u2026"


def _key(provider: str, env: str) -> str:
    return f"{provider.lower()}|{env.lower()}"


def _entry(provider: str, env: str) -> dict:
    """Devuelve (creando si hace falta) el registro de una integración."""
    k = _key(provider, env)
    if k not in _HEALTH:
        _HEALTH[k] = {
            "provider": provider.lower(),
            "env": env.lower(),
            "mode": None,              # "pull" | "push"
            "dict_enabled": False,
            "dict_count": 0,
            "dict_last_sync_ts": None,   # última sincronización exitosa
            "dict_last_error": None,
            "auth_ok": None,             # None = todavía sin intentar
            "auth_last_error": None,
            "last_fetch_ok_ts": None,    # último PULL exitoso
            "last_fetch_error": None,
        }
    return _HEALTH[k]


# ── Reportes desde el worker ─────────────────────────────────────────────────

def set_mode(provider: str, env: str, mode: str):
    """Marca la integración como 'pull' o 'push'."""
    with _LOCK:
        _entry(provider, env)["mode"] = mode


def report_dict_sync_ok(provider: str, env: str, count: int):
    with _LOCK:
        e = _entry(provider, env)
        e["dict_enabled"] = True
        e["dict_count"] = count
        e["dict_last_sync_ts"] = time.time()
        e["dict_last_error"] = None


def report_dict_count(provider: str, env: str, count: int):
    """
    Reporta cuántos IDs hay realmente en la tabla del diccionario.

    Se llama desde el poll loop en cada ciclo, no solo tras una sincronización
    exitosa: si el sync falla pero la tabla tiene datos de antes, la integración
    sigue operando y el panel debe mostrarlo así.
    """
    with _LOCK:
        e = _entry(provider, env)
        e["dict_count"] = count
        if count > 0:
            e["dict_enabled"] = True


def report_dict_error(provider: str, env: str, error: str):
    with _LOCK:
        e = _entry(provider, env)
        e["dict_enabled"] = True
        e["dict_last_error"] = str(error)[:300]


def report_dict_disabled(provider: str, env: str):
    with _LOCK:
        e = _entry(provider, env)
        e["dict_enabled"] = False
        e["dict_last_error"] = None


def report_auth_ok(provider: str, env: str):
    with _LOCK:
        e = _entry(provider, env)
        e["auth_ok"] = True
        e["auth_last_error"] = None


def report_auth_error(provider: str, env: str, error: str):
    with _LOCK:
        e = _entry(provider, env)
        e["auth_ok"] = False
        e["auth_last_error"] = str(error)[:300]


def report_fetch_ok(provider: str, env: str):
    with _LOCK:
        e = _entry(provider, env)
        e["last_fetch_ok_ts"] = time.time()
        e["last_fetch_error"] = None


def report_fetch_error(provider: str, env: str, error: str):
    with _LOCK:
        _entry(provider, env)["last_fetch_error"] = str(error)[:300]


# ── Lectura para el dashboard ────────────────────────────────────────────────

def _derive_status(e: dict) -> tuple[str, str]:
    """
    Deriva el estado semántico de una integración.

    error → hay algo roto que impide traer datos correctos
    warn  → funciona parcialmente, o falta información para operar bien
    ok    → todo normal
    """
    if e.get("auth_ok") is False:
        causa = _short_error(e.get("auth_last_error"))
        return "error", f"Auth: {causa}" if causa else "Autenticación fallando"

    if e.get("last_fetch_error"):
        causa = _short_error(e.get("last_fetch_error"))
        return "error", f"Proveedor: {causa}" if causa else "Error al consultar el proveedor"

    if e.get("dict_enabled"):
        if e.get("dict_count", 0) == 0:
            # Sin IDs en tabla: el PULL no tiene qué consultar
            causa = _short_error(e.get("dict_last_error"))
            return "error", f"Sin IDs — {causa}" if causa else "Diccionario vacío — sin IDs"
        if e.get("dict_last_error"):
            # Hay datos de sincronizaciones previas: opera, pero no se actualiza.
            # No se descubren vehículos nuevos hasta resolver el error.
            causa = _short_error(e.get("dict_last_error"))
            return "warn", f"Sync: {causa}" if causa else "Sync fallando — usando datos previos"

    if e.get("mode") == "pull" and e.get("last_fetch_ok_ts") is None:
        return "warn", "Sin datos todavía"

    return "ok", "Operativo"


def _rate_usage(provider: str, env: str) -> dict:
    """Uso del rate limit de la integración. Aislado para no acoplar módulos."""
    try:
        from app.core.rate_limit import get_usage
        return get_usage(provider, env)
    except Exception:
        return {"used": 0, "limit": 0, "pct": 0}


def get_health_snapshot() -> list[dict]:
    """
    Snapshot serializable de todas las integraciones registradas.
    Los timestamps se entregan como antigüedad en segundos para que el
    frontend no dependa del reloj del cliente.
    """
    now = time.time()
    out = []
    with _LOCK:
        for e in _HEALTH.values():
            status, detail = _derive_status(e)
            out.append({
                "provider": e["provider"],
                "env": e["env"],
                "mode": e["mode"],
                "status": status,
                "detail": detail,
                "dict_enabled": e["dict_enabled"],
                "dict_count": e["dict_count"],
                "dict_age_sec": (
                    int(now - e["dict_last_sync_ts"]) if e["dict_last_sync_ts"] else None
                ),
                "dict_error": e["dict_last_error"],
                "auth_ok": e["auth_ok"],
                "auth_error": e["auth_last_error"],
                "fetch_age_sec": (
                    int(now - e["last_fetch_ok_ts"]) if e["last_fetch_ok_ts"] else None
                ),
                "fetch_error": e["last_fetch_error"],
                "rate": _rate_usage(e["provider"], e["env"]),
            })
    out.sort(key=lambda x: (x["provider"], x["env"]))
    return out


def reset():
    """Solo para tests."""
    with _LOCK:
        _HEALTH.clear()
