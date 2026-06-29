from fastapi import APIRouter

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
    market_data = alpaca_market_data_service.get_stock_snapshot(symbol, timeframe="1Day", limit=5)
    return {
        "symbol": symbol.upper(),
        "name": f"{symbol.upper()} Market",
        "market_data": market_data,
    }
