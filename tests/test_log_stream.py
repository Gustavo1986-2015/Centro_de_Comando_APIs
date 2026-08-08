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


# ── Filtro por nivel del lado del servidor ───────────────────────────────────

def _escribir_log(ruta, entradas):
    with open(ruta, "w", encoding="utf-8") as f:
        for nivel, msg in entradas:
            f.write(json.dumps({"levelname": nivel, "message": msg, "name": "test"}) + "\n")


def test_filtro_por_nivel_recorre_todo_el_archivo(tmp_path, monkeypatch):
    """
    REGRESIÓN: el filtro se aplicaba en el navegador sobre las últimas N líneas
    cargadas. Con tráfico alto, esas N líneas son unos segundos de log: buscar
    errores devolvía una consola vacía aunque existieran más atrás en el archivo.
    """
    archivo = tmp_path / "app.jsonl"
    # Un error viejo, sepultado bajo 500 líneas de INFO
    entradas = [("ERROR", "fallo antiguo importante")]
    entradas += [("INFO", f"ruido {i}") for i in range(500)]
    _escribir_log(archivo, entradas)

    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    # Sin filtro, con un buffer chico el error queda fuera
    recientes = read_recent(100)
    assert not any(r["level"] == "ERROR" for r in recientes)

    # Con filtro en el servidor, aparece
    errores = read_recent(100, min_level="ERROR")
    assert len(errores) == 1
    assert "fallo antiguo" in errores[0]["message"]


def test_filtro_incluye_los_niveles_superiores(tmp_path, monkeypatch):
    """Pedir WARNING debe traer también los ERROR: son más graves, no menos."""
    archivo = tmp_path / "app.jsonl"
    _escribir_log(archivo, [
        ("DEBUG", "detalle"), ("INFO", "normal"),
        ("WARNING", "atencion"), ("ERROR", "problema"), ("CRITICAL", "grave"),
    ])
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    niveles = {r["level"] for r in read_recent(100, min_level="WARNING")}
    assert niveles == {"WARNING", "ERROR", "CRITICAL"}


def test_filtro_error_excluye_lo_menos_grave(tmp_path, monkeypatch):
    archivo = tmp_path / "app.jsonl"
    _escribir_log(archivo, [
        ("INFO", "a"), ("WARNING", "b"), ("ERROR", "c"), ("CRITICAL", "d"),
    ])
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    niveles = {r["level"] for r in read_recent(100, min_level="ERROR")}
    assert niveles == {"ERROR", "CRITICAL"}


def test_sin_filtro_devuelve_todos_los_niveles(tmp_path, monkeypatch):
    archivo = tmp_path / "app.jsonl"
    _escribir_log(archivo, [("INFO", "a"), ("WARNING", "b"), ("ERROR", "c")])
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    assert len(read_recent(100)) == 3
    assert len(read_recent(100, min_level=None)) == 3


def test_filtro_respeta_el_limite_y_devuelve_lo_mas_reciente(tmp_path, monkeypatch):
    archivo = tmp_path / "app.jsonl"
    _escribir_log(archivo, [("ERROR", f"error {i}") for i in range(50)])
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    r = read_recent(10, min_level="ERROR")
    assert len(r) == 10
    assert "error 49" in r[-1]["message"]   # el más reciente al final


def test_nivel_invalido_no_filtra(tmp_path, monkeypatch):
    """Un nivel desconocido no debe ocultar todo el log."""
    archivo = tmp_path / "app.jsonl"
    _escribir_log(archivo, [("INFO", "a"), ("ERROR", "b")])
    monkeypatch.setattr("app.core.log_stream.LOG_FILE", str(archivo))

    assert len(read_recent(100, min_level="INEXISTENTE")) == 2


# ── Cambio de nivel en caliente desde el panel ───────────────────────────────

def test_cambio_de_nivel_en_caliente_afecta_a_la_aplicacion():
    """
    Permite diagnosticar en producción sin acceso al servidor: subir el detalle
    desde el panel en lugar de editar el .env y reiniciar el contenedor.
    """
    import logging
    from app.core.logging_config import set_runtime_level, get_current_levels

    original = get_current_levels()["app"]
    try:
        set_runtime_level("DEBUG")
        assert logging.getLogger("app.worker.processor").getEffectiveLevel() == logging.DEBUG
        assert get_current_levels()["app"] == "DEBUG"
    finally:
        set_runtime_level(original)


def test_subir_el_detalle_no_arrastra_a_las_librerias():
    """
    sqlalchemy en DEBUG imprime cada consulta y zeep cada XML SOAP: con decenas
    de mensajes por segundo eso entierra la información propia y llena el disco.
    """
    import logging
    from app.core.logging_config import set_runtime_level, get_current_levels

    original = get_current_levels()["app"]
    try:
        set_runtime_level("DEBUG")
        assert logging.getLogger("sqlalchemy").getEffectiveLevel() > logging.DEBUG
        assert logging.getLogger("zeep").getEffectiveLevel() > logging.DEBUG
    finally:
        set_runtime_level(original)


def test_las_librerias_se_pueden_subir_explicitamente():
    import logging
    from app.core.logging_config import set_runtime_level, get_current_levels

    original = get_current_levels()
    try:
        set_runtime_level("DEBUG", "DEBUG")
        assert logging.getLogger("sqlalchemy").getEffectiveLevel() == logging.DEBUG
    finally:
        set_runtime_level(original["app"], original["libs"])


def test_nivel_invalido_es_rechazado():
    from app.core.logging_config import set_runtime_level

    with pytest.raises(ValueError):
        set_runtime_level("NO_EXISTE")


def test_el_cambio_informa_el_nivel_anterior():
    """El operador debe poder ver desde dónde cambió, para volver atrás."""
    from app.core.logging_config import set_runtime_level, get_current_levels

    original = get_current_levels()["app"]
    try:
        set_runtime_level("INFO")
        r = set_runtime_level("WARNING")
        assert r["previous"] == "INFO"
        assert r["app"] == "WARNING"
    finally:
        set_runtime_level(original)


# ── Aislamiento del log de la suite ──────────────────────────────────────────

def test_la_suite_no_escribe_en_el_log_de_la_aplicacion():
    """
    REGRESIÓN: los tests provocan errores a propósito (descifrados que fallan,
    respuestas de error de proveedores, tokens inválidos). Cuando escribían en
    logs/app.jsonl esos mensajes aparecían en la consola del panel junto a los
    errores reales, y no había forma de distinguir unos de otros.
    """
    import os
    from app.core.logging_config import get_log_file_path

    ruta = get_log_file_path()
    assert "app.jsonl" not in os.path.basename(ruta) or "test" in ruta.lower(), (
        f"La suite está escribiendo en el log de la aplicación: {ruta}"
    )
    assert os.environ.get("LOG_FILE_PATH"), "LOG_FILE_PATH debe estar definida en la suite"


def test_la_ruta_del_log_es_configurable(monkeypatch, tmp_path):
    """Permite aislar el log en tests y reubicarlo en un despliegue si hace falta."""
    from app.core.logging_config import get_log_file_path

    destino = str(tmp_path / "otro.jsonl")
    monkeypatch.setenv("LOG_FILE_PATH", destino)
    assert get_log_file_path() == destino


def test_sin_variable_usa_la_ruta_por_defecto(monkeypatch):
    from app.core.logging_config import get_log_file_path

    monkeypatch.delenv("LOG_FILE_PATH", raising=False)
    assert get_log_file_path().replace("\\", "/").endswith("logs/app.jsonl")
