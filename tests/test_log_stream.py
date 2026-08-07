"""
Tests del streaming de logs hacia el dashboard (app/core/log_stream.py).

El punto crítico es el enmascarado: los logs contienen URLs completas con
access_token, signature y contraseñas de los proveedores. Exponerlos en el
navegador equivaldría a filtrar credenciales de terceros.

El enmascarado es genérico por nombre de parámetro, no por proveedor: cubre
Protrack, Schmitz y cualquier integración futura sin cambios.
"""
import json
import pytest

from app.core.log_stream import mask_secrets, _parse_line, read_recent


# ── Enmascarado de credenciales ──────────────────────────────────────────────

def test_enmascara_access_token_en_query_string():
    """Caso real: las URLs de Protrack llevan el token completo en la query."""
    crudo = ("GET http://api.protrack365.com/api/track?imeis=864035052734572"
             "&access_token=A17861146451624ae4c5ad1d218fca0b6c7108cc52e1e32")
    salida = mask_secrets(crudo)

    assert "A17861146451624ae4c5ad1d218fca0b6c7108cc52e1e32" not in salida
    assert "access_token=A178***" in salida


def test_enmascara_signature():
    crudo = "GET /api/authorization?time=1786114644&signature=252d2299a27e8ad72001cc7a1ffb108c"
    salida = mask_secrets(crudo)
    assert "252d2299a27e8ad72001cc7a1ffb108c" not in salida
    assert "signature=252d***" in salida


@pytest.mark.parametrize("crudo,secreto", [
    ('{"password": "gps202secreto", "user": "x"}', "gps202secreto"),
    ("{'auth_pass': 'clave_larga_123'}", "clave_larga_123"),
    ("x-api-key: abc123def456ghi789", "abc123def456ghi789"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
    ("api_key=SUPERSECRETO12345", "SUPERSECRETO12345"),
])
def test_enmascara_variantes_de_credenciales(crudo, secreto):
    """Cubre query string, JSON con comillas dobles, repr de Python y headers."""
    assert secreto not in mask_secrets(crudo)


def test_conserva_prefijo_para_correlacionar():
    """
    Se dejan los primeros 4 caracteres a propósito: permite ver si dos líneas
    usan el mismo token (útil al diagnosticar rotaciones) sin exponerlo.
    """
    salida = mask_secrets("access_token=ABCD1234567890XYZ")
    assert salida.startswith("access_token=ABCD")
    assert "1234567890XYZ" not in salida


def test_oculta_por_completo_los_valores_cortos():
    """Un valor corto no se prefija: 4 de 6 caracteres sería casi el valor entero."""
    salida = mask_secrets("password=abc123")
    assert "abc123" not in salida


def test_no_toca_datos_operativos():
    """Los IMEIs, patentes y mensajes de error deben seguir siendo legibles."""
    casos = [
        "imeis=864035052734572,868307060968914",
        "Diccionario actualizado: 29 registros guardados.",
        "respuesta={'code': 20001, 'message': 'account or password error'}",
        "batch_processed provider=protrack env=test sent=87 failed=0",
    ]
    for c in casos:
        assert mask_secrets(c) == c


def test_mask_secrets_con_vacio():
    assert mask_secrets("") == ""
    assert mask_secrets(None) is None


# ── Parseo de líneas ─────────────────────────────────────────────────────────

def test_parse_linea_json_del_logger():
    linea = json.dumps({
        "asctime": "2026-08-07T10:26:44-0300",
        "levelname": "INFO",
        "name": "app.worker.pull_engine",
        "message": "Diccionario actualizado: 29 registros",
    })
    r = _parse_line(linea)
    assert r["level"] == "INFO"
    assert r["logger"] == "app.worker.pull_engine"
    assert "29 registros" in r["message"]


def test_parse_linea_enmascara_el_mensaje():
    """El enmascarado debe aplicarse también al pasar por el parser."""
    linea = json.dumps({
        "asctime": "2026-08-07T10:26:44-0300",
        "levelname": "INFO",
        "name": "httpx",
        "message": "GET /api/track?access_token=SECRETO_LARGO_12345",
    })
    assert "SECRETO_LARGO_12345" not in _parse_line(linea)["message"]


def test_parse_linea_no_json():
    """Tracebacks y salida de librerías no vienen en JSON: no deben perderse."""
    r = _parse_line("Traceback (most recent call last):")
    assert r["level"] == "INFO"
    assert "Traceback" in r["message"]


def test_parse_linea_vacia():
    assert _parse_line("") is None
    assert _parse_line("   \n") is None


def test_parse_nivel_siempre_en_mayusculas():
    linea = json.dumps({"levelname": "warning", "message": "x"})
    assert _parse_line(linea)["level"] == "WARNING"


# ── Lectura del archivo ──────────────────────────────────────────────────────

def test_read_recent_sin_archivo(tmp_path, monkeypatch):
    """Si el archivo aún no existe, devolver lista vacía en lugar de fallar."""
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(tmp_path / "no-existe.jsonl"))
    assert read_recent(10) == []


def test_read_recent_devuelve_las_ultimas(tmp_path, monkeypatch):
    archivo = tmp_path / "app.jsonl"
    with open(archivo, "w", encoding="utf-8") as f:
        for i in range(50):
            f.write(json.dumps({"levelname": "INFO", "message": f"linea {i}"}) + "\n")

    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))
    logs = read_recent(10)

    assert len(logs) == 10
    assert "linea 49" in logs[-1]["message"]   # la más reciente al final


def test_read_recent_acota_el_limite(tmp_path, monkeypatch):
    """Un n desmedido no debe permitir cargar el archivo entero en memoria."""
    archivo = tmp_path / "app.jsonl"
    archivo.write_text(json.dumps({"message": "x"}) + "\n", encoding="utf-8")
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    assert len(read_recent(999999)) <= 2000


def test_read_recent_enmascara(tmp_path, monkeypatch):
    """Verificación de punta a punta: nada sensible sale de la lectura."""
    archivo = tmp_path / "app.jsonl"
    archivo.write_text(
        json.dumps({"message": "GET /x?access_token=TOKENSECRETO123456"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    assert "TOKENSECRETO123456" not in read_recent(1)[0]["message"]
