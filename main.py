from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.core.logging_config import setup_logging, watch_log_config
setup_logging()

import asyncio
from contextlib import asynccontextmanager
import os
import logging

logger = logging.getLogger(__name__)

from app.api.routers import schmitz, dashboard, health, inspector, dynamic_webhook, db_viewer, vehicles, audit_logs, admin_config
from app.api.routers.schmitz import start_webhook_batch_processor, router_spec as schmitz_router_spec
from app.api.routers.dashboard import broadcast_loop, record_push_latency
from app.worker.processor import worker_loop
import time
from fastapi import Request

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ----- STARTUP -----
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
    await start_webhook_batch_processor()

    yield

    # ----- SHUTDOWN -----
    # Cancelar tareas graceful al cerrar la app
    task_worker.cancel()
    task_broadcast.cancel()
    task_watch_logs.cancel()
    # Esperar cancelación sin bloquear el shutdown
    for task in (task_worker, task_broadcast, task_watch_logs):
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

app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.middleware("http")
async def measure_push_latency(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = time.perf_counter() - start_time
    
    # Identify if it's a push webhook
    path = request.url.path
    if request.method == "POST":
        provider = None
        if path == "/Json/Data" or path.startswith("/schmitz/"):
            provider = "schmitz"
        elif "/webhook" in path:
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2 and parts[0] != "api" and parts[0] != "inspector":
                provider = parts[0]
                
        if provider:
            record_push_latency(provider, process_time)
            
    return response



if __name__ == "__main__":
    import uvicorn, os
    is_dev = os.getenv("APP_ENV", "production").lower() == "development"
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=is_dev,
        reload_dirs=["app"] if is_dev else None,
        reload_excludes=["db/*", "audit/*", "*.db", "*.db-wal", "*.db-shm"] if is_dev else None,
    )
