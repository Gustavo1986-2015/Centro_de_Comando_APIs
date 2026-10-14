"""
Reintento manual de eventos fallidos (v1.9.9, 3.8).

Caso real (13/10): una caída de RC dejó 35 eventos en 'failed' y la única
forma de reenviarlos fue SQL a mano en el servidor. Desde el panel, con el
filtro "Fallidos" de Última Actividad Global, ahora se puede:

  · reintentar una fila;
  · ver los fallidos AGRUPADOS por integración y motivo de RC (key/code de la
    excepción, o la categoría) y elegir qué grupos reintentar. Los rechazos
    permanentes (BUSINESS, PROTOCOL) se marcan: van a volver a fallar salvo
    que se corrija el dato.

Reglas, validadas acá y no en el navegador:
  · solo se tocan eventos en 'failed' de una base de COLA de una integración
    configurada. Nunca 'sent', 'simulado', 'pending' ni 'processing', aunque se
    pidan por id;
  · el pedido trae la cantidad que el operador confirmó. Si al ejecutar la
    cantidad real es otra (llegaron o se purgaron fallidos mientras miraba),
    no se toca nada y se pide revisar de nuevo;
  · cada evento vuelve a 'pending' con retry_count 0 y next_retry_at NULL, y se
    despierta al worker. El worker respeta el modo simulado de la integración
    como siempre: si está en simulado, no llama a RC;
  · el resultado anterior de RC no se pierde: antes de cambiar el estado, el
    evento tal como estaba (estado, job_id, respuesta de RC) se escribe en el
    respaldo de procesados del día (db/backups_diarios/...), con la marca
    "reintento_manual" (usuario y fecha). Se escribe aunque el respaldo de
    procesados esté desactivado: es el único rastro del motivo del fallo;
  · queda en el registro de acciones administrativas: usuario, integraciones,
    cantidad, grupos elegidos y fecha.
"""
import asyncio
import json
import logging
import os
import re
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel, Field

from app.core.auditor import log_admin_action
from app.core.auth import verify_dashboard_auth
from app.database import es_base_de_cola, get_session
from app.models.config_models import ProviderConfig
from app.models.db_models import NormalizedRCEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reintentos", tags=["Reintento manual"])

_NOMBRE = re.compile(r"^[a-z0-9_]+$")
_TANDA_SQL = 500

# Categoría según el prefijo del job_id que deja el worker o el cliente SOAP.
_PREFIJOS = (
    ("rc_business", "BUSINESS"),
    ("rc_protocol", "PROTOCOL"),
    ("rc_auth", "AUTH"),
    ("rc_retry", "TRANSPORT"),
    ("rc_conn_err", "TRANSPORT"),
    ("rc_err_", "TRANSPORT"),
    ("rc_sin_respuesta", "TRANSPORT"),
    ("worker_err", "INTERNO"),
)
PERMANENTES = {"BUSINESS", "PROTOCOL"}
AVISO_PERMANENTE = "Va a volver a fallar salvo que se corrija el dato."


def clasificar(job_id: str | None, rc_response: str | None) -> tuple[str, str]:
    """
    (motivo, categoría) de un evento fallido. El motivo es el key/code de la
    excepción de RC si vino (por ejemplo "CGI:SESSION_STORE_UNAVAILABLE (4023)");
    si no, la categoría.
    """
    categoria = next((c for p, c in _PREFIJOS if (job_id or "").startswith(p)), "OTRO")
    texto = rc_response or ""
    # RC marcó el error como temporal (1.6). Los fallidos de antes de la
    # v1.9.9 tienen job_id de negocio, pero con esta marca no son permanentes:
    # son justamente los que dejó la caída del 13/10.
    if re.search(r"\bseverity:\s*retry\b", texto, re.IGNORECASE):
        categoria = "TRANSPORT"
    clave = re.search(r"\bkey:\s*([^,]+)", texto, re.IGNORECASE)
    codigo = re.search(r"\bcode:\s*([^,]+)", texto, re.IGNORECASE)
    if clave:
        motivo = clave.group(1).strip() + (f" ({codigo.group(1).strip()})" if codigo else "")
    else:
        motivo = categoria
    return motivo, categoria


def _ruta_cola(provider: str, env: str) -> str:
    return os.path.join("db", provider, f"{env}.db")


def _integraciones(provider: str | None = None, env: str | None = None) -> list[tuple[str, str]]:
    """
    Integraciones configuradas con base de cola, opcionalmente filtradas.
    Los nombres se validan contra el esquema real: tienen que existir en
    provider_config y tener una base de cola (nunca una base de estado).
    """
    for nombre, valor in (("integración", provider), ("entorno", env)):
        if valor is not None and not _NOMBRE.match(valor):
            raise HTTPException(status_code=400, detail=f"Nombre de {nombre} inválido.")
    db = get_session("system_config", "global")
    try:
        pares = sorted({(c.provider_name.lower(), c.env.lower()) for c in db.query(ProviderConfig).all()
                        if c.provider_name and c.env})
    finally:
        db.close()
    elegidas = [(p, e) for p, e in pares
                if (provider is None or p == provider) and (env is None or e == env)]
    if (provider is not None or env is not None) and not elegidas:
        raise HTTPException(status_code=404, detail="No existe esa integración.")
    return [(p, e) for p, e in elegidas if es_base_de_cola(_ruta_cola(p, e))]


def _grupos_sync(provider: str | None, env: str | None) -> list[dict]:
    """Fallidos agrupados por integración y motivo, con los ids de cada grupo."""
    grupos: "OrderedDict[str, dict]" = OrderedDict()
    for p, e in _integraciones(provider, env):
        db = get_session(p, e)
        try:
            filas = (db.query(NormalizedRCEvent.id, NormalizedRCEvent.job_id, NormalizedRCEvent.rc_response)
                     .filter(NormalizedRCEvent.status == "failed")
                     .order_by(NormalizedRCEvent.id).all())
        finally:
            db.close()
        for id_, job_id, respuesta in filas:
            motivo, categoria = clasificar(job_id, respuesta)
            clave = f"{p}/{e}|{motivo}"
            g = grupos.get(clave)
            if g is None:
                g = grupos[clave] = {
                    "id": clave, "integracion": f"{p}/{e}", "provider": p, "env": e,
                    "motivo": motivo, "categoria": categoria,
                    "permanente": categoria in PERMANENTES,
                    "aviso": AVISO_PERMANENTE if categoria in PERMANENTES else "",
                    "cantidad": 0, "ejemplo": (respuesta or job_id or "")[:300], "ids": [],
                }
            g["cantidad"] += 1
            g["ids"].append(id_)
    return list(grupos.values())


@router.get("/fallidos")
async def resumen_de_fallidos(
    provider: str | None = Query(None),
    env: str | None = Query(None),
    _: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """Resumen agrupado por integración y motivo de RC. No modifica nada."""
    grupos = await asyncio.to_thread(_grupos_sync, provider and provider.lower(), env and env.lower())
    return {
        "grupos": [{k: v for k, v in g.items() if k != "ids"} for g in grupos],
        "total": sum(g["cantidad"] for g in grupos),
    }


class PedidoReintento(BaseModel):
    """Una fila (provider + env + ids) o grupos del resumen, con la cantidad confirmada."""
    provider: str | None = None
    env: str | None = None
    ids: list[int] = Field(default_factory=list)
    grupos: list[str] = Field(default_factory=list)
    cantidad_confirmada: int


def _reintentar_sync(pedido: PedidoReintento, usuario: str) -> dict:
    if bool(pedido.ids) == bool(pedido.grupos):
        raise HTTPException(status_code=400, detail="Indicar ids de una integración o grupos del resumen, no los dos.")

    # Qué se pidió, por integración: {(p, e): [ids]}
    por_integracion: dict[tuple[str, str], list[int]] = {}
    grupos_elegidos = []
    if pedido.ids:
        if not pedido.provider or not pedido.env:
            raise HTTPException(status_code=400, detail="Falta la integración de los eventos.")
        p, e = pedido.provider.lower(), pedido.env.lower()
        if (p, e) not in _integraciones(p, e):
            raise HTTPException(status_code=404, detail="Esa integración no tiene base de cola.")
        por_integracion[(p, e)] = sorted(set(pedido.ids))
    else:
        disponibles = {g["id"]: g for g in _grupos_sync(None, None)}
        faltan = [g for g in pedido.grupos if g not in disponibles]
        if faltan:
            raise HTTPException(status_code=409, detail=(
                f"Ya no hay fallidos en: {', '.join(faltan)}. Volvé a abrir el resumen."))
        for clave in dict.fromkeys(pedido.grupos):
            g = disponibles[clave]
            por_integracion.setdefault((g["provider"], g["env"]), []).extend(g["ids"])
            grupos_elegidos.append({"grupo": clave, "categoria": g["categoria"], "cantidad": g["cantidad"]})

    from app.worker.processor import evento_a_registro_respaldo

    # Primero se cuenta, con la misma regla con la que se va a escribir: solo
    # lo que HOY está en 'failed'. Si no coincide con lo confirmado, nada.
    sesiones = {}
    elegibles = {}
    try:
        for (p, e), ids in por_integracion.items():
            db = sesiones[(p, e)] = get_session(p, e)
            filas = []
            for i in range(0, len(ids), _TANDA_SQL):
                filas += (db.query(NormalizedRCEvent)
                          .filter(NormalizedRCEvent.id.in_(ids[i:i + _TANDA_SQL]),
                                  NormalizedRCEvent.status == "failed").all())
            elegibles[(p, e)] = filas
        total = sum(len(f) for f in elegibles.values())
        if total == 0:
            raise HTTPException(status_code=409, detail="Ninguno de esos eventos está en 'failed'. No se reintentó nada.")
        if total != pedido.cantidad_confirmada:
            raise HTTPException(status_code=409, detail=(
                f"La cantidad cambió: se confirmaron {pedido.cantidad_confirmada} y hoy hay {total} "
                f"en 'failed'. No se reintentó nada; volvé a revisar."))

        ahora = datetime.now(timezone.utc)
        marca = {"usuario": usuario, "fecha": ahora.isoformat()}
        local = datetime.now().astimezone()
        resultado = {}
        for (p, e), filas in elegibles.items():
            if not filas:
                continue
            # El resultado anterior de RC, al respaldo de procesados del día.
            carpeta = os.path.join("db", "backups_diarios", f"{p}_{e}", local.strftime("%Y-%m"))
            os.makedirs(carpeta, exist_ok=True)
            with open(os.path.join(carpeta, f"procesados_{local.strftime('%Y-%m-%d')}.jsonl"), "a",
                      encoding="utf-8") as f:
                for r in filas:
                    f.write(json.dumps({**evento_a_registro_respaldo(r, e), "reintento_manual": marca},
                                       ensure_ascii=False) + "\n")
            db = sesiones[(p, e)]
            ids = [r.id for r in filas]
            cambiados = 0
            for i in range(0, len(ids), _TANDA_SQL):
                cambiados += (db.query(NormalizedRCEvent)
                              .filter(NormalizedRCEvent.id.in_(ids[i:i + _TANDA_SQL]),
                                      NormalizedRCEvent.status == "failed")
                              .update({NormalizedRCEvent.status: "pending",
                                       NormalizedRCEvent.retry_count: 0,
                                       NormalizedRCEvent.next_retry_at: None,
                                       NormalizedRCEvent.updated_at: ahora.replace(tzinfo=None)},
                                      synchronize_session=False))
            db.commit()
            resultado[f"{p}/{e}"] = cambiados
    finally:
        for db in sesiones.values():
            db.close()

    return {"reintentados": sum(resultado.values()), "por_integracion": resultado,
            "grupos": grupos_elegidos, "ids": pedido.ids}


@router.post("/reintentar")
async def reintentar_fallidos(
    pedido: PedidoReintento,
    request: Request,
    credenciales: HTTPBasicCredentials = Depends(verify_dashboard_auth),
):
    """Vuelve a 'pending' los fallidos elegidos y despierta a los workers."""
    usuario = credenciales.username
    resultado = await asyncio.to_thread(_reintentar_sync, pedido, usuario)

    from app.worker.processor import trigger_worker
    for integracion in resultado["por_integracion"]:
        p, e = integracion.split("/")
        trigger_worker(p, e)

    log_admin_action("reintento_manual", {
        "integraciones": resultado["por_integracion"],
        "cantidad": resultado["reintentados"],
        "grupos": resultado["grupos"],
        "ids": resultado["ids"][:50],
        "fecha": datetime.now(timezone.utc).isoformat(),
    }, request, usuario)
    logger.warning(
        f"[REINTENTO MANUAL] {usuario} volvió a la cola {resultado['reintentados']} evento(s) fallido(s): "
        + ", ".join(f"{k}={v}" for k, v in resultado["por_integracion"].items())
        + (f" | grupos: {', '.join(g['grupo'] for g in resultado['grupos'])}" if resultado["grupos"] else "")
    )
    return {"ok": True, "reintentados": resultado["reintentados"], "por_integracion": resultado["por_integracion"]}
