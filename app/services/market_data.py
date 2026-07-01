import json
from typing import Any, Dict, List

import requests

from app.config import settings


class AlpacaMarketDataService:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.trust_env = False

    def get_stock_snapshot(self, symbol: str, timeframe: str = "1Day", limit: int = 5) -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        
        # Use Alpaca as primary source (Yahoo Finance API has rate limiting issues)
        result = self._get_alpaca_snapshot(symbol, timeframe, limit)
        if result.get("available"):
            return {**result, "source": "alpaca"}
        
        # Fallback to simpler approach if Alpaca fails
        return {**result, "source": "alpaca"}

    def _get_alpaca_snapshot(self, symbol: str, timeframe: str = "1Day", limit: int = 5) -> Dict[str, Any]:
        """Fetch market data from Alpaca API."""
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }

        try:
            bars_response = self._session.get(
                f"{settings.alpaca_data_base_url}/stocks/bars/latest",
                headers=headers,
                params={"symbols": symbol},
                timeout=15,
            )
            bars_response.raise_for_status()
            bars_payload = bars_response.json()
            bars = self._extract_bars(bars_payload, symbol)
            if not bars:
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "available": False,
                    "bars": [],
                }

            latest_bar = self._normalize_bar(bars[-1])

            quote_response = self._session.get(
                f"{settings.alpaca_data_base_url}/stocks/{symbol}/quotes/latest",
                headers=headers,
                timeout=15,
            )
            quote_response.raise_for_status()
            quote_payload = quote_response.json()
            latest_quote = self._normalize_quote(quote_payload)

            previous_bar = self._normalize_bar(bars[-2]) if len(bars) > 1 else latest_bar
            current_close = float(latest_bar["close"])
            previous_close = float(previous_bar["close"]) if previous_bar else current_close
            change_pct = ((current_close - previous_close) / previous_close * 100.0) if previous_close else 0.0
            average_volume = sum(float(bar.get("volume", 0.0)) for bar in bars) / len(bars) if bars else 0.0

            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "available": True,
                "bars": [self._normalize_bar(bar) for bar in bars],
                "latest_bar": latest_bar,
                "latest_quote": latest_quote,
                "latest_price": float(latest_quote.get("midpoint") or latest_bar["close"]),
                "previous_close": previous_close,
                "change_pct": round(change_pct, 4),
                "average_volume": round(average_volume, 2),
            }
        except requests.RequestException as exc:
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "available": False,
                "error": str(exc),
                "bars": [],
            }



    def build_analysis_summary(self, snapshot: Dict[str, Any] | None) -> str:
        if not snapshot:
            return "No live market data available."

        if not snapshot.get("available"):
            error = snapshot.get("error")
            return f"Market data unavailable for {snapshot.get('symbol', 'unknown')}: {error or 'unknown error'}"

        latest = snapshot.get("latest_bar") or {}
        quote = snapshot.get("latest_quote") or {}
        return (
            f"Live market data for {snapshot.get('symbol')}: "
            f"close={latest.get('close')}, high={latest.get('high')}, low={latest.get('low')}, "
            f"volume={latest.get('volume')}, bid={quote.get('bid')}, ask={quote.get('ask')}, "
            f"last_price={snapshot.get('latest_price')}, change_pct={snapshot.get('change_pct')}%, "
            f"avg_volume={snapshot.get('average_volume')}"
        )

    def _extract_bars(self, payload: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
        bars = payload.get("bars")
        if isinstance(bars, dict):
            bars = bars.get(symbol, [])
            if isinstance(bars, dict):
                bars = [bars]
        if not isinstance(bars, list):
            return []
        return [bar for bar in bars if isinstance(bar, dict)]

    def _normalize_bar(self, bar: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "time": bar.get("t") or bar.get("time"),
            "open": bar.get("o") or bar.get("open"),
            "high": bar.get("h") or bar.get("high"),
            "low": bar.get("l") or bar.get("low"),
            "close": bar.get("c") or bar.get("close"),
            "volume": bar.get("v") or bar.get("volume"),
        }

    def _normalize_quote(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        quote = payload.get("quote") if isinstance(payload, dict) else None
        if not isinstance(quote, dict):
            return {}

        bid = quote.get("bp")
        ask = quote.get("ap")
        midpoint = None
        if bid is not None and ask is not None:
            midpoint = (float(bid) + float(ask)) / 2.0

        return {
            "time": quote.get("t"),
            "bid": bid,
            "ask": ask,
            "midpoint": midpoint,
        }


alpaca_market_data_service = AlpacaMarketDataService()