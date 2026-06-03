"""
Cliente HTTP asíncrono para la API REST de Protrack365.

Responsabilidades:
- Gestión del access_token con renovación automática (token válido 2h).
- Firma HMAC-MD5: signature = md5( md5(password) + str(unix_time) )
- Obtención de lista de dispositivos: /api/device/list
- Posición actual de IMEIs: /api/track (lotes de hasta 100)
"""

import hashlib
import time
import logging
import os
import asyncio
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────
# Constantes
# ────────────────────────────────────────────
PROTRACK_BASE_URL = "http://api.protrack365.com"

# Códigos de error Protrack que indican token inválido/expirado
TOKEN_ERROR_CODES = {10002, 10003, 10010, 10011, 10012}

# Umbral en segundos para renovar el token proactivamente antes de que expire
TOKEN_REFRESH_THRESHOLD_SEC = 600  # renovar si quedan < 10 minutos


def _md5(text: str) -> str:
    """Retorna el hash MD5 de un string en hexadecimal lowercase (32 chars)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _build_signature(password: str, unix_time: int) -> str:
    """
    Calcula la firma requerida por Protrack:
      signature = md5( md5(password) + str(unix_time) )
    """
    return _md5(_md5(password) + str(unix_time))


class ProtrackClient:
    """
    Cliente async para la API de Protrack365.
    Instanciar una vez por entorno (test/prod) y reutilizar.
    """

    def __init__(self, account: str, password: str, base_url: str = PROTRACK_BASE_URL):
        self.account = account
        self.password = password
        self.base_url = base_url.rstrip("/")

        # Estado del token
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0  # epoch seconds
        self._token_lock = asyncio.Lock()

    # ────────────────────────────────────────
    # Autenticación
    # ────────────────────────────────────────

    def _token_is_valid(self) -> bool:
        """True si el token existe y quedan más de TOKEN_REFRESH_THRESHOLD_SEC para que expire."""
        if not self._access_token:
            return False
        remaining = self._token_expires_at - time.time()
        return remaining > TOKEN_REFRESH_THRESHOLD_SEC

    async def _fetch_token(self) -> str:
        """Llama a /api/authorization y devuelve el nuevo access_token."""
        unix_time = int(time.time())
        signature = _build_signature(self.password, unix_time)

        params = {
            "time": unix_time,
            "account": self.account,
            "signature": signature,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{self.base_url}/api/authorization", params=params)
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                f"Protrack auth error code={data.get('code')}: {data.get('message', '')}"
            )

        record = data["record"]
        token = record["access_token"]
        expires_in = int(record.get("expires_in", 7200))

        self._access_token = token
        self._token_expires_at = time.time() + expires_in
        logger.info(f"[Protrack] Token renovado para cuenta '{self.account}'. Expira en {expires_in}s.")
        return token

    async def get_token(self) -> str:
        """
        Devuelve el token vigente. Si está próximo a vencer o no existe, lo renueva.
        Thread-safe gracias al asyncio.Lock.
        """
        if self._token_is_valid():
            return self._access_token

        async with self._token_lock:
            # Doble check dentro del lock para evitar renovaciones duplicadas
            if self._token_is_valid():
                return self._access_token
            return await self._fetch_token()

    # ────────────────────────────────────────
    # Helpers internos
    # ────────────────────────────────────────

    async def _get(self, path: str, params: dict) -> dict:
        """
        Realiza un GET autenticado. Si la respuesta indica token inválido,
        renueva el token una vez y reintenta.
        """
        token = await self.get_token()
        params["access_token"] = token

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            resp.raise_for_status()
            data = resp.json()

        # Si el token expiró durante la llamada, reintentar una vez
        if data.get("code") in TOKEN_ERROR_CODES:
            logger.warning(f"[Protrack] Token inválido (code={data.get('code')}). Renovando y reintentando...")
            async with self._token_lock:
                self._access_token = None  # forzar renovación
            token = await self._fetch_token()
            params["access_token"] = token

            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{self.base_url}{path}", params=params)
                resp.raise_for_status()
                data = resp.json()

        return data

    # ────────────────────────────────────────
    # Endpoints de negocio
    # ────────────────────────────────────────

    async def get_devices(self) -> list[dict]:
        """
        Lista todos los dispositivos de la cuenta (máx. 500 por llamada).
        Retorna lista de dicts con: imei, devicename, devicetype, platenumber,
        onlinetime, platformduetime.
        """
        data = await self._get("/api/device/list", {})
        code = data.get("code")

        if code != 0:
            logger.error(f"[Protrack] get_devices error code={code}: {data.get('message', '')}")
            return []

        record = data.get("record", [])
        if not isinstance(record, list):
            return []

        logger.debug(f"[Protrack] {len(record)} dispositivos obtenidos.")
        return record

    async def get_track(self, imeis: list[str]) -> list[dict]:
        """
        Obtiene la última posición GPS de una lista de IMEIs.
        Protrack permite máx. 100 IMEIs por petición; esta función
        pagina automáticamente en lotes de 100.
        """
        if not imeis:
            return []

        all_records = []
        batch_size = 100

        for i in range(0, len(imeis), batch_size):
            batch = imeis[i : i + batch_size]
            imeis_str = ",".join(batch)

            try:
                data = await self._get("/api/track", {"imeis": imeis_str})
                code = data.get("code")

                if code == 0:
                    records = data.get("record", [])
                    if isinstance(records, list):
                        all_records.extend(records)
                elif code == 20023:
                    # "No Data" — normal si los dispositivos no han transmitido
                    logger.debug(f"[Protrack] get_track: sin datos para lote {i // batch_size + 1}")
                else:
                    logger.warning(
                        f"[Protrack] get_track error code={code} para lote {i // batch_size + 1}: "
                        f"{data.get('message', '')}"
                    )
            except Exception as e:
                logger.error(f"[Protrack] get_track excepción en lote {i // batch_size + 1}: {e}")

        logger.debug(f"[Protrack] get_track: {len(all_records)} registros de posición obtenidos.")
        return all_records


# ────────────────────────────────────────────────────────────────────────────
# Fábrica de clientes por entorno
# ────────────────────────────────────────────────────────────────────────────

_clients: dict[str, "ProtrackClient"] = {}


def get_protrack_client(env: str) -> ProtrackClient:
    """
    Devuelve (y crea si no existe) el cliente Protrack para el entorno indicado.
    Lee credenciales desde variables de entorno según el patrón:
      PROTRACK_ACCOUNT_<ENV> / PROTRACK_PASSWORD_<ENV>
    """
    env_upper = env.upper()
    key = env_upper

    if key not in _clients:
        account = os.getenv(f"PROTRACK_ACCOUNT_{env_upper}", "")
        password = os.getenv(f"PROTRACK_PASSWORD_{env_upper}", "")

        if not account or not password:
            raise RuntimeError(
                f"[Protrack] Credenciales no configuradas para entorno '{env}'. "
                f"Definir PROTRACK_ACCOUNT_{env_upper} y PROTRACK_PASSWORD_{env_upper} en .env"
            )

        _clients[key] = ProtrackClient(account=account, password=password)
        logger.info(f"[Protrack] Cliente inicializado para entorno '{env}' con cuenta '{account}'.")

    return _clients[key]
