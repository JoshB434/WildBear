import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.api import api_router
from app.persistence import load_state


async def keep_alive_ping() -> None:
    """Background task to keep Render app from sleeping by pinging the health endpoint every 10 minutes."""
    while True:
        await asyncio.sleep(600)  # 10 minutes
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.get("https://wildbear.onrender.com/", timeout=5)
        except Exception:
            pass  # Silently fail if pinging doesn't work


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    
    # Start keep-alive task to prevent Render free tier sleep
    keep_alive_task = asyncio.create_task(keep_alive_ping())
    
    try:
        yield
    finally:
        keep_alive_task.cancel()
        try:
            await keep_alive_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="AI Trading Bot API",
    version="1.0.0",
    description="Paper-trading, TradingView alerts, and AI-assisted stock analysis API",
    lifespan=lifespan,
)


@app.get("/")
def read_root():
    return {"message": "AI Trading Bot API", "status": "online"}


app.include_router(api_router, prefix="/api/v1")
