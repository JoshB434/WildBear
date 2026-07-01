import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from app.api.v1.api import api_router
from app.persistence import load_state


async def keep_alive_ping() -> None:
    """Background task to keep Render app awake during market hours (8:30 AM - 4:30 PM CDT, Monday-Friday)."""
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        
        # Check if we're within market hours (8:30 AM - 4:30 PM Central Time, Mon-Fri)
        now_ct = datetime.now(ZoneInfo("America/Chicago"))
        is_weekday = now_ct.weekday() < 5  # Monday=0 to Friday=4
        is_market_hours = (
            (now_ct.hour == 8 and now_ct.minute >= 30) or  # 8:30 AM or later
            (now_ct.hour >= 9 and now_ct.hour < 16) or  # 9 AM - 3:59 PM
            (now_ct.hour == 16 and now_ct.minute < 30)  # 4:00 PM - 4:29 PM
        )
        
        if is_weekday and is_market_hours:
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
