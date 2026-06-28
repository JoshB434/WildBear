from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.api import api_router
from app.persistence import load_state


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    yield


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
