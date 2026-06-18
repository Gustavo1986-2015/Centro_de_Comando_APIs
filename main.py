from fastapi import FastAPI
import asyncio

from app.api.routers import schmitz, dashboard, health, inspector, dynamic_webhook
from app.api.routers.schmitz import start_webhook_batch_processor, router_spec as schmitz_router_spec
from app.api.routers.dashboard import broadcast_loop, record_push_latency
from app.worker.processor import worker_loop
import time
from fastapi import Request

app = FastAPI(title="Centro de Comando en Vivo - Telemática")

# Incluir routers
app.include_router(schmitz.router)
app.include_router(schmitz_router_spec)
app.include_router(dashboard.router)
app.include_router(health.router)
app.include_router(inspector.router)
app.include_router(dynamic_webhook.router)

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

@app.on_event("startup")
async def startup_event():
    """Iniciar workers background cuando la API arranca."""
    import concurrent.futures
    loop = asyncio.get_running_loop()
    # Aumentar drásticamente el pool de hilos para que zeep y sqlite no se pongan en cola
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=200))
    asyncio.create_task(worker_loop())
    asyncio.create_task(broadcast_loop())
    await start_webhook_batch_processor()

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
