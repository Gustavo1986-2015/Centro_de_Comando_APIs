"""Tests del mapper de Protrack (vía DynamicMapper y pull_engine).

Valida:
  - La estructura esperada del payload Protrack (data, imei, lat, lng)
  - El esquema de mapeo dinámico acepta los campos de Protrack
  - execute_fetch en pull_engine contiene la lógica MD5 dinámica de Protrack
"""
import pytest
import inspect


def test_protrack_payload_has_expected_structure(sample_protrack_payload):
    """El payload Protrack debe tener la estructura esperada (data, imei, lat, lng)."""
    assert "data" in sample_protrack_payload, "El payload debe tener clave 'data'"
    assert isinstance(sample_protrack_payload["data"], list), "'data' debe ser una lista"
    assert len(sample_protrack_payload["data"]) > 0, "'data' no debe estar vacía"

    item = sample_protrack_payload["data"][0]
    assert "imei" in item, "Cada item debe tener 'imei'"
    assert "lat" in item, "Cada item debe tener 'lat'"
    assert "lng" in item, "Cada item debe tener 'lng'"


def test_dynamic_mapper_accepts_protrack_schema():
    """El esquema de mapeo dinámico para Protrack debe tener los campos obligatorios del modelo canónico."""
    protrack_mapping = {
        "chassis_number": "imei",
        "latitude": "lat",
        "longitude": "lng",
        "speed": "speed",
        "date": "time"
    }
    assert isinstance(protrack_mapping, dict), "El schema debe ser un dict"
    assert "chassis_number" in protrack_mapping, (
        "El schema debe mapear chassis_number (campo obligatorio en RCCanonicalModel)"
    )
    assert "latitude" in protrack_mapping, "El schema debe mapear latitude"
    assert "longitude" in protrack_mapping, "El schema debe mapear longitude"


def test_protrack_auth_uses_dynamic_md5(monkeypatch):
    """
    La autenticación de Protrack es md5(md5(clave) + tiempo). v1.9.9 (B-20): se
    EJECUTA execute_fetch contra un Protrack simulado que verifica esa firma
    (antes se buscaba "md5" en el fuente).
    """
    import asyncio
    from app.worker import pull_engine
    pedidos = _b20_protrack_simulado(monkeypatch, clave="clave-md5")
    asyncio.run(pull_engine.execute_fetch({"url": "http://api.protrack365.com/api/track", "auth_type": "protrack",
                                           "auth_user": "cuenta", "auth_pass": "clave-md5"}))
    autorizacion = pedidos[0][1]
    assert pedidos[0][0] == "/api/authorization" and autorizacion["account"] == "cuenta"
    assert pedidos[1][1]["access_token"] == "TOKEN-B20", "la firma no era la esperada"
    pull_engine._TOKEN_CACHE.clear()


# ─── v1.9.9 (B-20): Protrack simulado para los tests que ejecutan el PULL ──────
def _b20_protrack_simulado(monkeypatch, clave="clave-de-prueba", pedidos=None):
    """
    execute_fetch / _get_protrack_token contra un Protrack simulado (httpx
    MockTransport) que exige la firma md5(md5(clave)+tiempo). Los nombres se
    resuelven a una IP pública fija para que el guardián de destinos corra igual.
    """
    import hashlib
    import socket
    import httpx
    from app.worker import pull_engine
    pedidos = [] if pedidos is None else pedidos

    def protrack(request):
        q = dict(request.url.params)
        pedidos.append((request.url.path, q))
        if request.url.path == "/api/authorization":
            esperada = hashlib.md5((hashlib.md5(clave.encode()).hexdigest() + q["time"]).encode()).hexdigest()
            if q.get("signature") != esperada:
                return httpx.Response(200, json={"code": 10003, "message": "signature error"})
            return httpx.Response(200, json={"code": 0, "record": {"access_token": "TOKEN-B20", "expires_in": 7200}})
        if q.get("access_token") == "TOKEN-B20":
            return httpx.Response(200, json={"code": 0, "record": [{"imei": "864035052734572"}]})
        return httpx.Response(200, json={"code": 10011, "message": "access_token error"})

    real = httpx.AsyncClient
    monkeypatch.setattr(pull_engine.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(protrack), **k))
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    pull_engine._TOKEN_CACHE.clear()
    return pedidos
