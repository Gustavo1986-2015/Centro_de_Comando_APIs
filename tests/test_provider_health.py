"""
Tests del registro de salud de integraciones (app/core/provider_health.py).

El objetivo del módulo es que un fallo de integración (auth caída, diccionario
vacío, proveedor devolviendo errores) sea visible en el dashboard en segundos,
en lugar de descubrirse horas después por eventos con datos vacíos llegando a RC.
"""
import pytest

from app.core import provider_health as ph


@pytest.fixture(autouse=True)
def _reset():
    ph.reset()
    yield
    ph.reset()


def _get(snapshot, provider, env):
    return next(x for x in snapshot if x["provider"] == provider and x["env"] == env)


# ── Registro básico ──────────────────────────────────────────────────────────

def test_snapshot_vacio_al_inicio():
    assert ph.get_health_snapshot() == []


def test_set_mode_crea_la_entrada():
    ph.set_mode("protrack", "prod", "pull")
    snap = ph.get_health_snapshot()
    assert len(snap) == 1
    assert _get(snap, "protrack", "prod")["mode"] == "pull"


def test_normaliza_mayusculas():
    ph.set_mode("PROTRACK", "PROD", "pull")
    ph.report_fetch_ok("protrack", "prod")
    # Debe ser la misma entrada, no dos
    assert len(ph.get_health_snapshot()) == 1


def test_multiples_integraciones_se_separan():
    ph.set_mode("protrack", "prod", "pull")
    ph.set_mode("protrack", "test", "pull")
    ph.set_mode("schmitz", "prod", "push")
    assert len(ph.get_health_snapshot()) == 3


def test_snapshot_ordenado_por_proveedor_y_env():
    ph.set_mode("schmitz", "test", "push")
    ph.set_mode("protrack", "prod", "pull")
    ph.set_mode("protrack", "test", "pull")
    snap = ph.get_health_snapshot()
    assert [(x["provider"], x["env"]) for x in snap] == [
        ("protrack", "prod"), ("protrack", "test"), ("schmitz", "test")
    ]


# ── Derivación de estado ─────────────────────────────────────────────────────

def test_estado_ok_cuando_todo_funciona():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_dict_sync_ok("protrack", "prod", 31)
    ph.report_fetch_ok("protrack", "prod")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "ok"
    assert e["dict_count"] == 31


def test_estado_error_si_falla_la_autenticacion():
    """Caso real: code=10011 access_token error."""
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_error("protrack", "prod", "access_token error")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "error"
    assert e["auth_ok"] is False
    assert "access_token" in e["auth_error"]


def test_estado_error_si_el_diccionario_esta_vacio():
    """
    REGRESIÓN — incidente de producción.

    Con el diccionario vacío, el PULL no tiene IDs que consultar y el proveedor
    devuelve error. Ese error terminaba en RC como eventos UNKNOWN. Ahora el
    estado debe marcarse en rojo antes de que eso ocurra.
    """
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_dict_error("protrack", "prod", "code=10011 access_token error")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "error"
    assert e["dict_count"] == 0
    assert "Sin IDs" in e["detail"]


def test_estado_warn_si_el_diccionario_falla_pero_tiene_datos_previos():
    """Sincronizó antes, ahora falla: sigue operando con datos viejos."""
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_dict_sync_ok("protrack", "prod", 31)
    ph.report_fetch_ok("protrack", "prod")
    ph.report_dict_error("protrack", "prod", "timeout")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "warn"
    assert e["dict_count"] == 31


def test_estado_warn_si_pull_nunca_trajo_datos():
    ph.set_mode("protrack", "prod", "pull")
    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "warn"
    assert e["fetch_age_sec"] is None


def test_estado_error_si_el_fetch_falla():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_fetch_ok("protrack", "prod")
    ph.report_fetch_error("protrack", "prod", "connection timeout")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "error"


def test_auth_error_tiene_prioridad_sobre_diccionario():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_dict_sync_ok("protrack", "prod", 31)
    ph.report_auth_error("protrack", "prod", "credenciales inválidas")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "error"
    assert e["detail"].startswith("Auth:")


# ── Recuperación ─────────────────────────────────────────────────────────────

def test_sync_exitosa_limpia_el_error_previo():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_dict_error("protrack", "prod", "fallo temporal")
    ph.report_dict_sync_ok("protrack", "prod", 31)
    ph.report_fetch_ok("protrack", "prod")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "ok"
    assert e["dict_error"] is None


def test_auth_ok_limpia_el_error_previo():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_error("protrack", "prod", "token vencido")
    ph.report_auth_ok("protrack", "prod")
    ph.report_fetch_ok("protrack", "prod")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["auth_ok"] is True
    assert e["auth_error"] is None
    assert e["status"] == "ok"


def test_fetch_ok_limpia_el_error_previo():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_fetch_error("protrack", "prod", "timeout")
    ph.report_fetch_ok("protrack", "prod")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "ok"
    assert e["fetch_error"] is None


# ── Serialización para el frontend ───────────────────────────────────────────

def test_edades_en_segundos_no_timestamps():
    """El frontend no debe depender del reloj del cliente."""
    ph.set_mode("protrack", "prod", "pull")
    ph.report_dict_sync_ok("protrack", "prod", 5)
    ph.report_fetch_ok("protrack", "prod")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert isinstance(e["dict_age_sec"], int) and e["dict_age_sec"] >= 0
    assert isinstance(e["fetch_age_sec"], int) and e["fetch_age_sec"] >= 0
    assert "dict_last_sync_ts" not in e


def test_errores_se_truncan():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_dict_error("protrack", "prod", "x" * 1000)
    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert len(e["dict_error"]) <= 300


def test_diccionario_deshabilitado_no_marca_error():
    """Un proveedor sin diccionario configurado es válido, no un fallo."""
    ph.set_mode("schmitz", "prod", "push")
    ph.report_dict_disabled("schmitz", "prod")
    ph.report_fetch_ok("schmitz", "prod")

    e = _get(ph.get_health_snapshot(), "schmitz", "prod")
    assert e["dict_enabled"] is False
    assert e["status"] == "ok"


def test_conteo_real_prevalece_sobre_sync_fallida():
    """
    REGRESIÓN — chip contradictorio en producción.

    El sync del diccionario fallaba por credenciales, pero la tabla tenía 30 IDs
    de sincronizaciones previas y el PULL funcionaba con normalidad. El panel
    mostraba "Diccionario vacío" mientras la consola traía 30 eventos por ciclo.

    El conteo real de la tabla debe prevalecer sobre el resultado de la sync.
    """
    ph.set_mode("protrack", "test", "pull")
    ph.report_auth_ok("protrack", "test")
    ph.report_dict_error("protrack", "test", "code=20001 account or password error")
    ph.report_dict_count("protrack", "test", 30)   # lo que hay realmente en la tabla
    ph.report_fetch_ok("protrack", "test")

    e = _get(ph.get_health_snapshot(), "protrack", "test")
    assert e["dict_count"] == 30
    assert e["status"] == "warn"                    # opera, pero no se actualiza
    assert e["detail"].startswith("Sync:")


def test_conteo_cero_si_la_tabla_esta_realmente_vacia():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_dict_error("protrack", "prod", "auth error")
    ph.report_dict_count("protrack", "prod", 0)

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["status"] == "error"
    assert "Sin IDs" in e["detail"]


def test_report_dict_count_habilita_el_diccionario():
    """Si hay IDs en tabla, el diccionario está en uso aunque no haya sincronizado aún."""
    ph.set_mode("protrack", "test", "pull")
    ph.report_dict_count("protrack", "test", 12)

    e = _get(ph.get_health_snapshot(), "protrack", "test")
    assert e["dict_enabled"] is True
    assert e["dict_count"] == 12


# ── Causa real del error visible en el chip ──────────────────────────────────

def test_short_error_extrae_el_mensaje_del_proveedor():
    """
    El error crudo viene envuelto en ruido (URL, cuenta, dict de respuesta).
    En el chip solo cabe la causa. Genérico: busca los campos de mensaje
    habituales sin asumir un proveedor concreto.
    """
    crudo = (
        "El proveedor rechazó la autenticación. "
        "URL=http://api.x.com/api/authorization account=user "
        "respuesta={'code': 20001, 'message': 'account or password error'}"
    )
    assert ph._short_error(crudo) == "account or password error"


def test_short_error_soporta_comillas_dobles():
    assert ph._short_error('{"code": 10005, "msg": "invalid token"}') == "invalid token"


def test_short_error_trunca_mensajes_largos():
    assert len(ph._short_error("x" * 500)) <= 60


def test_short_error_con_vacio():
    assert ph._short_error(None) == ""
    assert ph._short_error("") == ""


def test_detalle_de_auth_muestra_la_causa():
    """REGRESIÓN: antes el chip decía 'Autenticación fallando' sin explicar por qué."""
    ph.set_mode("protrack", "test", "pull")
    ph.report_auth_error(
        "protrack", "test",
        "El proveedor rechazó la autenticación. respuesta={'code': 20001, "
        "'message': 'account or password error'}"
    )
    e = _get(ph.get_health_snapshot(), "protrack", "test")
    assert e["detail"] == "Auth: account or password error"


def test_detalle_de_sync_muestra_la_causa():
    ph.set_mode("protrack", "test", "pull")
    ph.report_auth_ok("protrack", "test")
    ph.report_dict_count("protrack", "test", 29)
    ph.report_dict_error("protrack", "test", "{'code': 10011, 'message': 'access_token error'}")
    ph.report_fetch_ok("protrack", "test")

    e = _get(ph.get_health_snapshot(), "protrack", "test")
    assert e["status"] == "warn"
    assert e["detail"] == "Sync: access_token error"


def test_detalle_de_fetch_muestra_la_causa():
    ph.set_mode("protrack", "prod", "pull")
    ph.report_auth_ok("protrack", "prod")
    ph.report_fetch_error("protrack", "prod", "{'code': 10005, 'message': 'missing required parameter:imeis'}")

    e = _get(ph.get_health_snapshot(), "protrack", "prod")
    assert e["detail"] == "Proveedor: missing required parameter:imeis"


def test_snapshot_es_serializable_a_json():
    import json
    ph.set_mode("protrack", "prod", "pull")
    ph.report_dict_sync_ok("protrack", "prod", 31)
    ph.report_fetch_ok("protrack", "prod")
    json.dumps(ph.get_health_snapshot())   # no debe lanzar
