import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from app.api.v1.api import api_router
from app.database import trading_store
from app.persistence import load_state
from app.schemas import RiskSettings


async def keep_alive_ping() -> None:
    """Background task to keep Render app awake during trading hours (7:00 AM - 8:00 PM CDT, Monday-Friday) to catch pre-market, regular, and after-hours alerts."""
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        
        # Check if we're within trading hours (7:00 AM - 8:00 PM Central Time, Mon-Fri)
        # This covers pre-market (7 AM), regular hours (9:30 AM - 4 PM), and after-hours (4-8 PM)
        now_ct = datetime.now(ZoneInfo("America/Chicago"))
        is_weekday = now_ct.weekday() < 5  # Monday=0 to Friday=4
        is_trading_hours = (
            (now_ct.hour == 7) or  # 7:00 AM - 7:59 AM (pre-market start)
            (now_ct.hour >= 8 and now_ct.hour < 20)  # 8:00 AM - 7:59 PM (covers all trading)
        )
        
        if is_weekday and is_trading_hours:
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    await client.get("https://wildbear.onrender.com/", timeout=5)
            except Exception:
                pass  # Silently fail if pinging doesn't work


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    
    # Initialize default risk settings if not already set
    if trading_store.get_risk_settings() is None:
        trading_store.save_risk_settings(
            RiskSettings(
                max_position_size=50,  # Fallback max shares per trade
                daily_loss_limit=5.0,
                stop_loss_pct=0.05,
                take_profit_pct=0.1,
                cooldown_minutes=30,
                max_open_positions=10,
                max_consecutive_losses=5,
                # Dynamic allocation tiers (percentage-based)
                first_buy_allocation_pct=25.0,      # 1st buy: 25% of account
                second_buy_allocation_pct=25.0,     # 2nd buy: 25% of account
                subsequent_buy_allocation_pct=7.5,  # 3rd+: 7.5% of account
                max_total_allocation_pct=75.0,      # Max 75% total invested
            )
        )
    
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
