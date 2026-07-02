from fastapi import APIRouter

from app.config import settings
from app.services.market_data import alpaca_market_data_service

router = APIRouter()


@router.get("", include_in_schema=False)
def list_markets_root():
    return {
        "markets": [
            {"symbol": "AAPL", "name": "Apple"},
            {"symbol": "MSFT", "name": "Microsoft"},
            {"symbol": "NVDA", "name": "NVIDIA"},
        ]
    }


@router.get("/")
def list_markets():
    return {
        "markets": [
            {"symbol": "AAPL", "name": "Apple"},
            {"symbol": "MSFT", "name": "Microsoft"},
            {"symbol": "NVDA", "name": "NVIDIA"},
        ]
    }


@router.get("/{symbol}")
def get_market(symbol: str):
    try:
        market_data = alpaca_market_data_service.get_stock_snapshot(symbol, timeframe="1Day", limit=5)
    except Exception as e:
        return {
            "symbol": symbol.upper(),
            "error": str(e),
            "regime": {"allowed": False, "reasons": ["data-fetch-error"]},
        }

    # Regime filter check with hardcoded thresholds to avoid settings import issues
    try:
        average_volume = float(market_data.get("average_volume") or 0)
        change_pct = float(market_data.get("change_pct") or 0)
    except (ValueError, TypeError):
        average_volume = 0
        change_pct = 0

    REGIME_MIN_VOLUME = 1000000
    REGIME_MAX_CHANGE = 10.0

    reasons = []
    if average_volume < REGIME_MIN_VOLUME:
        reasons.append("low-average-volume")
    if abs(change_pct) > REGIME_MAX_CHANGE:
        reasons.append("excess-volatility")
    
    is_available = market_data.get("available", False)
    regime_allowed = len(reasons) == 0 and is_available

    return {
        "symbol": symbol.upper(),
        "name": f"{symbol.upper()} Market",
        "market_data": market_data,
        "regime": {
            "allowed": regime_allowed,
            "reasons": reasons,
            "average_volume": average_volume,
            "change_pct": change_pct,
            "source": market_data.get("source", "unknown"),
        },
    }
