from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.core.logging_config import setup_logging, watch_log_config
setup_logging()

import asyncio
from contextlib import asynccontextmanager
import os
import logging

logger = logging.getLogger(__name__)

from app.api.routers import schmitz, dashboard, health, inspector, dynamic_webhook, db_viewer, vehicles, audit_logs, admin_config, exports, config_backup
from app.api.routers.schmitz import start_webhook_batch_processor, router_spec as schmitz_router_spec
from app.api.routers.dashboard import broadcast_loop, record_push_latency
from app.worker.processor import worker_loop
import time
from fastapi import Request

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ----- STARTUP -----
    # Se valida antes que nada: en producción, arrancar sin contraseña dejaría
    # el panel, el visor de base de datos y las credenciales de los proveedores
    # accesibles. Mejor no levantar el servicio que levantarlo desprotegido.
    from app.core.auth import verificar_credenciales_al_arrancar
    verificar_credenciales_al_arrancar()

    # v1.9.8: la versión en ejecución, en la consola, al arrancar.
    from app.version import __version__ as version_hub
    logging.getLogger("main").info(f"Hub Telemático Assistcargo v{version_hub}: arrancando.")

    import concurrent.futures
    loop = asyncio.get_running_loop()
    thread_pool_size = int(os.getenv("THREAD_POOL_SIZE", "64"))
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=thread_pool_size)
    )
    logger.info(f"Thread pool size: {thread_pool_size}")

    task_worker = asyncio.create_task(worker_loop())
    task_broadcast = asyncio.create_task(broadcast_loop())
    task_watch_logs = asyncio.create_task(watch_log_config())
    
    # Hilo fantasma de métricas internas (Kill Switch)
    from app.core.health_metrics import telemetry_daemon_loop
    task_telemetry = asyncio.create_task(telemetry_daemon_loop())
    
    await start_webhook_batch_processor()

    # Resolutor del nombre del tracker por la API de Tive (v1.9.4). Corre
    # siempre; no consulta nada si el interruptor está apagado o faltan las
    # credenciales.
    from app.providers.tive import resolutor as resolutor_tive
    task_resolutor_tive = asyncio.create_task(resolutor_tive.bucle())

    yield

    # ----- SHUTDOWN -----
    # Cancelar tareas graceful al cerrar la app
    task_worker.cancel()
    task_broadcast.cancel()
    task_watch_logs.cancel()
    task_telemetry.cancel()
    task_resolutor_tive.cancel()
    # Esperar cancelación sin bloquear el shutdown
    for task in (task_worker, task_broadcast, task_watch_logs, task_telemetry, task_resolutor_tive):
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="Centro de Comando en Vivo - Telemática", lifespan=lifespan)

# Incluir routers
app.include_router(schmitz.router)
app.include_router(schmitz_router_spec)
app.include_router(dashboard.router)
app.include_router(db_viewer.router)
app.include_router(vehicles.router)
app.include_router(audit_logs.router)
app.include_router(admin_config.router)
app.include_router(health.router)
app.include_router(inspector.router)
app.include_router(dynamic_webhook.router)
app.include_router(exports.router)
app.include_router(config_backup.router)

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

@app.middleware("http")
async def measure_push_latency(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = time.perf_counter() - start_time
    
    # Identify if it's a push webhook
    path = request.url.path
    if request.method == "POST":
        provider = None
        entorno = None
        if path == "/Json/Data" or path.startswith("/schmitz/"):
            provider = "schmitz"
        elif path.startswith("/webhook/dynamic/"):
            # v1.9.6: el nombre de la integración y no "webhook", y solo si el
            # webhook la ACEPTÓ. Antes todo el webhook genérico caía bajo
            # "webhook:<env>", con los rechazos por firma y las URL a
            # integraciones inexistentes adentro; y una clave por cada nombre
            # que llegue dejaría a cualquiera crear claves sin límite.
            aceptada = getattr(request.state, "push_aceptada", None)
            if aceptada:
                provider, entorno = aceptada
        elif "/webhook" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[0] != "api" and parts[0] != "inspector":
                provider = parts[0]
                
        if provider:
            # Separado por entorno: agrupar test y prod bajo la misma clave
            # mezclaba tráfico de prueba con el real.
            entorno = entorno or (request.query_params.get("env") or "prod").lower()
            record_push_latency(f"{provider}:{entorno}", process_time)
            
    return response



if __name__ == "__main__":
    import uvicorn
    is_dev = os.getenv("APP_ENV", "production").lower() == "development"
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=is_dev,
        reload_dirs=["app"] if is_dev else None,
        reload_excludes=["db/*", "audit/*", "*.db", "*.db-wal", "*.db-shm"] if is_dev else None,
    )
