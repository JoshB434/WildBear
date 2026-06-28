from fastapi import APIRouter

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
    return {"symbol": symbol.upper(), "name": f"{symbol.upper()} Market"}
