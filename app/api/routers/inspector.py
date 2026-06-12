from fastapi import APIRouter, Request, Body, HTTPException, Depends
from app.api.routers.dashboard import verify_dashboard_auth
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import requests
import uuid
import ipaddress
import socket
import urllib.parse

router = APIRouter(prefix="/inspector", tags=["API Inspector"])

# Caché en memoria para los payloads recibidos (útil para la sesión actual del usuario)
CACHED_PAYLOADS: Dict[str, Any] = {}


def _is_safe_url(url: str) -> bool:
    """Bloquea URLs que apunten a IPs privadas, loopback o metadata de cloud (anti-SSRF)."""
    try:
        host = urllib.parse.urlparse(url).hostname
        if not host:
            return False
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except Exception:
        return False

@router.post("/catch/{session_id}")
async def catch_webhook(session_id: str, request: Request, _: None = Depends(verify_dashboard_auth)):
    """
    Modo PUSH (Webhooks):
    Atrapa cualquier payload enviado a esta URL temporal y lo guarda en memoria.
    """
    try:
        payload = await request.json()
    except Exception:
        # Intentar parsear como texto si no es JSON puro
        body_bytes = await request.body()
        payload = {"raw_text": body_bytes.decode('utf-8', errors='replace')}
        
    CACHED_PAYLOADS[session_id] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "headers": dict(request.headers),
        "payload": payload
    }
    
    return {"status": "ok", "message": "Payload atrapado exitosamente", "session_id": session_id}

@router.get("/catch/{session_id}/latest")
async def get_latest_catch(session_id: str, _: None = Depends(verify_dashboard_auth)):
    """
    Permite al UI consultar (sondear) si ya llegó el payload a la URL temporal.
    """
    if session_id in CACHED_PAYLOADS:
        return {"has_data": True, "data": CACHED_PAYLOADS[session_id]}
    return {"has_data": False}

@router.post("/fetch")
async def fetch_api(request_data: dict = Body(...), _: None = Depends(verify_dashboard_auth)):
    """
    Modo PULL (Mini-Postman):
    Realiza una solicitud HTTP a nombre del cliente para evitar problemas de CORS del navegador.
    Soporta: Basic Auth, Bearer Token, Headers personalizados.
    """
    import time
    import hashlib
    import urllib.parse
    
    url = request_data.get("url")
    method = request_data.get("method", "GET").upper()
    headers = request_data.get("headers", {})
    body = request_data.get("body")
    auth_type = request_data.get("auth_type", "none")
    auth_user = request_data.get("auth_user")
    auth_pass = request_data.get("auth_pass")
    bearer_token = request_data.get("bearer_token")
    
    auth_tuple = None
    if auth_type == "basic" and auth_user and auth_pass:
        auth_tuple = (auth_user, auth_pass)
    
    if auth_type == "bearer" and bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
        
    if auth_type == "dynamic_md5" and auth_user and auth_pass:
        # Lógica abstracta de Firma Dinámica: time + md5(md5(pass) + time)
        # Usado comúnmente por APIs de Telemetría (Ej. Protrack) y Pasarelas de Pago.
        current_unix_time = str(int(time.time()))
        pass_md5 = hashlib.md5(auth_pass.encode()).hexdigest()
        signature = hashlib.md5((pass_md5 + current_unix_time).encode()).hexdigest()
        
        # Inyectar parámetros estándar de firma en la URL
        parsed_url = urllib.parse.urlparse(url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        query_params['time'] = [current_unix_time]
        query_params['account'] = [auth_user]
        query_params['signature'] = [signature]
        
        new_query = urllib.parse.urlencode(query_params, doseq=True)
        url = urllib.parse.urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, parsed_url.params, new_query, parsed_url.fragment))
    
    
    if not url:
        raise HTTPException(status_code=400, detail="La URL es requerida")

    if not _is_safe_url(url):
        raise HTTPException(status_code=400, detail="URL no permitida: apunta a un host interno o reservado.")
        
    try:
        start = time.perf_counter()
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            auth=auth_tuple,
            json=body if isinstance(body, (dict, list)) else None,
            data=body if isinstance(body, str) else None,
            timeout=30,
            verify=False
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        
        try:
            resp_data = response.json()
        except:
            resp_data = response.text
            
        return {
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "headers": dict(response.headers),
            "payload": resp_data
        }
    except requests.exceptions.ConnectTimeout:
        raise HTTPException(status_code=504, detail="Timeout: El servidor no respondió en 30 segundos.")
    except requests.exceptions.ConnectionError as e:
        raise HTTPException(status_code=502, detail=f"No se pudo conectar: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/fetch-token")
async def fetch_token(request_data: dict = Body(...), _: None = Depends(verify_dashboard_auth)):
    """
    Obtiene un Bearer Token de un endpoint OAuth / API Key.
    Soporta flujos: client_credentials, password grant, o API Key simple.
    """
    import time
    
    token_url = request_data.get("token_url")
    token_method = request_data.get("token_method", "POST").upper()
    token_body = request_data.get("token_body", {})
    token_headers = request_data.get("token_headers", {"Content-Type": "application/x-www-form-urlencoded"})
    auth_user = request_data.get("auth_user")
    auth_pass = request_data.get("auth_pass")
    
    if not token_url:
        raise HTTPException(status_code=400, detail="La URL del token es requerida")

    if not _is_safe_url(token_url):
        raise HTTPException(status_code=400, detail="URL no permitida: apunta a un host interno o reservado.")
    
    auth_tuple = None
    if auth_user and auth_pass:
        auth_tuple = (auth_user, auth_pass)
    
    try:
        start = time.perf_counter()
        response = requests.request(
            method=token_method,
            url=token_url,
            headers=token_headers,
            auth=auth_tuple,
            data=token_body if isinstance(token_body, (dict, str)) else None,
            timeout=15,
            verify=False
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        
        try:
            resp_data = response.json()
        except:
            resp_data = {"raw": response.text}
        
        # Intentar extraer el token automáticamente de formatos comunes
        extracted_token = None
        if isinstance(resp_data, dict):
            for key in ["access_token", "token", "accessToken", "id_token", "jwt", "api_key", "apiKey", "key"]:
                if key in resp_data:
                    extracted_token = resp_data[key]
                    break
        
        return {
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "payload": resp_data,
            "extracted_token": extracted_token
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

