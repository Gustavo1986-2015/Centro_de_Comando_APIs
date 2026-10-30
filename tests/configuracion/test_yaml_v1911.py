"""
v1.9.11 · 6.3 — Respaldo YAML: ida y vuelta de todo lo nuevo.

Por los endpoints reales (exportar, simular, importar):
  · los interruptores del módulo de Tive, incluidos velocidad_equipos (v1.9.10)
    y enviar_sin_posicion (v1.9.11);
  · la opción de coordenadas 0,0 de cada integración (v1.9.11);
  · la retención de descartes (v1.9.10).
Exportar → cambiar todo en la base → importar deja lo exportado, y volver a
exportar da el mismo archivo. Un YAML de antes, sin esas claves, se importa
y rige lo que el código tiene por defecto.
"""
import yaml

from app.core import contrato
from app.providers.tive import modulo as tive
from tests.proveedores.tive.test_v195_yaml_tive import YAML as YAML_TIVE_V195, _filas, auth, cliente  # noqa: F401

NO_POR_DEFECTO = {clave: not valor for clave, valor in tive.INTERRUPTORES.items()}


def _sesion():
    from app.database import get_session
    return get_session("system_config", "global")


def _preparar(opciones_tive, ceros_schmitz, dias, filas):
    from app.models.config_models import ProviderConfig, SystemSettings
    db = _sesion()
    try:
        conf = db.query(ProviderConfig).filter_by(provider_name="tive", env="prod").first()
        if conf is None:
            conf = ProviderConfig(provider_name="tive", env="prod", provider_type="push", is_active=True,
                                  use_mock=True, mapping_schema={})
            db.add(conf)
        conf.module_options = opciones_tive
        db.query(ProviderConfig).filter_by(provider_name="schmitz").one().coordenadas_cero = ceros_schmitz
        settings = db.query(SystemSettings).first()
        if settings is None:
            settings = SystemSettings()
            db.add(settings)
        settings.descartes_retencion_dias = dias
        settings.descartes_max_filas = filas
        db.commit()
    finally:
        db.close()


def _exportar(cliente, auth):
    r = cliente.get("/api/config/export", auth=auth)
    assert r.status_code == 200, r.text
    return r.text


def _sin_marca(texto):
    datos = yaml.safe_load(texto)
    datos.pop("exportado", None)
    return datos


def _importar(cliente, auth, texto):
    r = cliente.post("/api/config/import", auth=auth,
                     json={"contenido": texto, "sobrescribir": True, "confirmacion": "IMPORTAR"})
    assert r.status_code == 200, r.text
    return r.json()


def _vigente():
    from app.models.config_models import SystemSettings
    filas = _filas()
    db = _sesion()
    try:
        s = db.query(SystemSettings).first()
        return {
            "tive": tive.opciones_efectivas(filas[("tive", "prod")].module_options),
            "ceros": {k: v.coordenadas_cero or contrato.CEROS_ENVIAR for k, v in filas.items()},
            "descartes": (s.descartes_retencion_dias, s.descartes_max_filas),
        }
    finally:
        db.close()


def test_ida_y_vuelta_de_interruptores_opcion_0_0_y_retencion_de_descartes(cliente, auth):
    _preparar(NO_POR_DEFECTO, contrato.CEROS_FILTRAR, 3, 20_000)
    texto = _exportar(cliente, auth)
    datos = yaml.safe_load(texto)
    prov = {(p["nombre"], p["entorno"]): p for p in datos["proveedores"]}
    assert prov[("tive", "prod")]["opciones_modulo"] == NO_POR_DEFECTO, "viajan los 7, también los nuevos"
    assert prov[("tive", "prod")]["opciones_modulo"]["enviar_sin_posicion"] is False
    assert prov[("tive", "prod")]["opciones_modulo"]["velocidad_equipos"] is True
    assert prov[("schmitz", "prod")]["coordenadas_cero"] == "filtrar"
    assert prov[("protrack", "prod")]["coordenadas_cero"] == "enviar", "sin valor guardado: lo que rige"
    general = datos["configuracion_general"]
    assert (general["descartes_retencion_dias"], general["descartes_max_filas"]) == (3, 20_000)

    # Todo distinto en la base…
    _preparar({}, contrato.CEROS_ENVIAR, 10, 90_000)
    assert _vigente()["tive"] == tive.INTERRUPTORES
    simulado = cliente.post("/api/config/import/simular", auth=auth,
                            json={"contenido": texto, "sobrescribir": True}).json()
    campos = {c["campo"] for c in simulado["configuracion_general"]}
    assert {"descartes_retencion_dias", "descartes_max_filas"} <= campos, "la simulación lo anuncia"
    # …y el YAML lo devuelve como estaba.
    _importar(cliente, auth, texto)
    v = _vigente()
    assert v["tive"] == NO_POR_DEFECTO
    assert v["ceros"][("schmitz", "prod")] == "filtrar" and v["ceros"][("protrack", "prod")] == "enviar"
    assert v["descartes"] == (3, 20_000)
    assert _sin_marca(_exportar(cliente, auth)) == _sin_marca(texto), "exportar de nuevo da el mismo archivo"


def test_un_yaml_de_antes_se_importa_y_rigen_los_valores_por_defecto(cliente, auth):
    """El YAML de Tive de v1.9.5 no conoce velocidad_equipos, enviar_sin_posicion ni coordenadas_cero."""
    for clave in ("velocidad_equipos", "enviar_sin_posicion", "coordenadas_cero", "descartes"):
        assert clave not in YAML_TIVE_V195
    plan = cliente.post("/api/config/import/simular", auth=auth,
                        json={"contenido": YAML_TIVE_V195, "sobrescribir": False})
    assert plan.status_code == 200, plan.text
    _importar(cliente, auth, YAML_TIVE_V195)
    v = _vigente()
    assert v["tive"]["velocidad_equipos"] is False and v["tive"]["enviar_sin_posicion"] is True
    del_yaml = yaml.safe_load(YAML_TIVE_V195)["proveedores"][0]["opciones_modulo"]
    assert v["tive"] == tive.opciones_efectivas(del_yaml), "lo del YAML, y lo que no trae, por defecto"
    assert v["ceros"][("tive", "prod")] == contrato.CEROS_ENVIAR
    # La retención de descartes: sin nada guardado, la de siempre al exportar.
    general = yaml.safe_load(_exportar(cliente, auth))["configuracion_general"]
    assert (general["descartes_retencion_dias"], general["descartes_max_filas"]) == (7, 50_000)


def test_un_respaldo_completo_viejo_sin_claves_nuevas_no_pisa_lo_guardado(cliente, auth):
    """
    Un respaldo general de v1.9.9 (sin retención de descartes ni 0,0): lo que
    no trae no se toca; lo guardado sigue rigiendo.
    """
    _preparar(dict(tive.INTERRUPTORES), contrato.CEROS_FILTRAR, 3, 20_000)
    datos = yaml.safe_load(_exportar(cliente, auth))
    for p in datos["proveedores"]:
        p.pop("coordenadas_cero", None)
        (p.get("opciones_modulo") or {}).pop("enviar_sin_posicion", None)
    for clave in ("descartes_retencion_dias", "descartes_max_filas"):
        datos["configuracion_general"].pop(clave)
    _preparar(dict(tive.INTERRUPTORES, enviar_sin_posicion=False), contrato.CEROS_FILTRAR, 3, 20_000)
    _importar(cliente, auth, yaml.safe_dump(datos, allow_unicode=True))
    v = _vigente()
    assert v["ceros"][("schmitz", "prod")] == "filtrar" and v["descartes"] == (3, 20_000)
    # opciones_modulo viaja como bloque: sin la clave nueva, rige su valor por defecto (encendido).
    assert v["tive"]["enviar_sin_posicion"] is True
