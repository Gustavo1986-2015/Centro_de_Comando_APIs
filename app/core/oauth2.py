"""
Token OAuth2 por client_credentials, compartido por todo el hub.

Hasta la v1.9.3 vivía en app/worker/pull_engine.py. Se movió acá en la
v1.9.4, SIN cambiar su comportamiento, para que el resolutor de nombres de
Tive (app/providers/tive/resolutor.py) lo use sin depender del motor PULL.

pull_engine reimporta estos mismos objetos (_TOKEN_CACHE, _TOKEN_LOCK,
ProviderAuthError, get_oauth2_token como _get_oauth2_token): el caché es
UNO solo, compartido con el token de Protrack, como antes.

Único cambio: el aviso de renovación ya no incluye el client_id. Las
credenciales no se escriben en logs.
"""
import asyncio
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CACHÉ DE TOKENS (P1-4)
# Evita pedir un token nuevo en cada ciclo de PULL. Sin esto, con un intervalo
# de 11s se generan ~7.800 llamadas/día a /api/authorization con la misma cuenta,
# lo que agota el rate limit del proveedor y provoca fallos intermitentes de auth.
# ─────────────────────────────────────────────────────────────────────────────
_TOKEN_CACHE: dict[str, dict] = {}   # {cache_key: {"token": str, "expires_at": float}}
_TOKEN_LOCK = asyncio.Lock()

# Fallback SOLO por si el proveedor no incluye 'expires_in' en la respuesta
# (no debería pasar, pero mejor no reventar el flujo si ocurre).
# El TTL real se toma del campo expires_in de cada respuesta de /api/authorization.
TOKEN_TTL_FALLBACK_SECONDS = 1500

# Margen de seguridad: renovar un poco antes del vencimiento real para evitar
# que un token expire a mitad de una llamada en curso.
TOKEN_SAFETY_MARGIN_SECONDS = 120


class ProviderAuthError(Exception):
    """La autenticación con el proveedor falló. Aborta el ciclo, no encola nada."""


async def get_oauth2_token(token_url: str, client_id: str, client_secret: str,
                            formato_body: str = "form", scope: str = "") -> str:
    """
    Token OAuth2 por `client_credentials`, reutilizando el cacheado si sigue vigente.

    Transversal: sirve para cualquier API que use este flujo estándar. Las dos
    variantes que existen en la práctica se eligen por configuración:

      form       application/x-www-form-urlencoded — lo que dice el estándar
                 OAuth2 (RFC 6749) y lo que usa la mayoría.
      multipart  multipart/form-data — lo que exige Tive. Un cliente OAuth
                 genérico que mande "form" recibe error de Tive.

    `expires_in` se acepta como número o como texto: Tive lo manda como
    "3600", entre comillas, aunque el estándar lo define numérico.
    """
    if not token_url:
        raise ProviderAuthError("auth_type=oauth2_client_credentials pero falta token_url.")
    if not client_id or not client_secret:
        raise ProviderAuthError(
            "auth_type=oauth2_client_credentials requiere client_id (auth_user) "
            "y client_secret (auth_pass)."
        )
    # El guardián de destinos vive en el motor PULL; se importa al llamar
    # para no crear un ciclo de imports.
    from app.worker.pull_engine import _verificar_destino_permitido
    _verificar_destino_permitido(token_url)

    cache_key = f"oauth2|{token_url}|{client_id}"
    async with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and cached["expires_at"] > time.time():
            return cached["token"]

        campos = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            campos["scope"] = scope

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if formato_body == "multipart":
                    # httpx arma multipart/form-data cuando recibe `files`.
                    # (None, valor) = campo de texto, no un archivo.
                    resp = await client.post(
                        token_url, files={k: (None, v) for k, v in campos.items()},
                        headers={"accept": "application/json"},
                    )
                else:
                    resp = await client.post(
                        token_url, data=campos, headers={"accept": "application/json"},
                    )
                resp.raise_for_status()
                datos = resp.json()
        except httpx.HTTPStatusError as e:
            raise ProviderAuthError(
                f"El proveedor rechazó la autenticación OAuth2 en {token_url}: "
                f"HTTP {e.response.status_code}. Verificar client_id, client_secret "
                f"y el formato del cuerpo ('{formato_body}')."
            ) from e
        except httpx.HTTPError as e:
            raise ProviderAuthError(f"Error de red al pedir token a {token_url}: {e}") from e
        except json.JSONDecodeError as e:
            raise ProviderAuthError(f"La respuesta de {token_url} no es JSON válido: {e}") from e

        token = datos.get("access_token")
        if not token:
            raise ProviderAuthError(f"La respuesta de {token_url} no trae access_token.")

        try:
            expires_in = int(datos.get("expires_in"))
            if expires_in <= 0:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning(
                f"Respuesta OAuth2 sin 'expires_in' válido, "
                f"usando fallback de {TOKEN_TTL_FALLBACK_SECONDS}s."
            )
            expires_in = TOKEN_TTL_FALLBACK_SECONDS

        ttl_efectivo = max(expires_in - TOKEN_SAFETY_MARGIN_SECONDS, 60)
        _TOKEN_CACHE[cache_key] = {"token": token, "expires_at": time.time() + ttl_efectivo}
        # Sin client_id ni secreto: las credenciales nunca se escriben en logs
        # (el client_id de Tive es un nombre legible, ej. "Envios Assistcargo").
        logger.info(
            f"Token OAuth2 renovado en {token_url}. Vigencia informada {expires_in}s, "
            f"se cachea {ttl_efectivo}s."
        )
        return token


def invalidar_token(token_url: str, client_id: str) -> bool:
    """Descarta el token cacheado (por ejemplo, ante un 401). True si había uno."""
    return _TOKEN_CACHE.pop(f"oauth2|{token_url}|{client_id}", None) is not None
