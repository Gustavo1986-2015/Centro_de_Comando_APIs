from fastapi import FastAPI
import asyncio

from app.api.routers import schmitz, dashboard, health
from app.api.routers.schmitz import start_webhook_batch_processor
from app.worker.processor import worker_loop

app = FastAPI(title="Centro de Comando en Vivo - Telemática")

# Incluir routers
app.include_router(schmitz.router)
app.include_router(dashboard.router)
app.include_router(health.router)

@app.on_event("startup")
async def startup_event():
    """Iniciar workers background cuando la API arranca."""
    asyncio.create_task(worker_loop())
    await start_webhook_batch_processor()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["app"],
        reload_excludes=["db/*", "audit/*", "*.db", "*.db-wal", "*.db-shm"]
    )
