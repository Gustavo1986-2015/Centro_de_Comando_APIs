from fastapi import FastAPI
import asyncio

from app.api.routers import schmitz, dashboard, health, inspector, dynamic_webhook
from app.api.routers.schmitz import start_webhook_batch_processor
from app.api.routers.dashboard import broadcast_loop
from app.worker.processor import worker_loop

app = FastAPI(title="Centro de Comando en Vivo - Telemática")

# Incluir routers
app.include_router(schmitz.router)
app.include_router(dashboard.router)
app.include_router(health.router)
app.include_router(inspector.router)
app.include_router(dynamic_webhook.router)

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
