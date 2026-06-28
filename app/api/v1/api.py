from fastapi import APIRouter

from app.api.v1.routes import assets, health, integration, market, trading

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(market.router, prefix="/market", tags=["market"])
api_router.include_router(assets.router, tags=["assets"])
api_router.include_router(trading.router, prefix="/trading", tags=["trading"])
api_router.include_router(integration.router, prefix="/integration", tags=["integration"])
