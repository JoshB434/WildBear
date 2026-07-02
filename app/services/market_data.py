import json
from typing import Any, Dict, List

import requests

from app.config import settings

_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


class AlpacaMarketDataService:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.trust_env = False

    def get_stock_snapshot(self, symbol: str, timeframe: str = "1Day", limit: int = 5) -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        
        # Try Yahoo Finance first (Alpaca paper API has restricted historical data access)
        result = self._get_yahoo_snapshot(symbol, timeframe, limit)
        if result.get("available"):
            return {**result, "source": "yahoo"}
        
        # Fall back to Alpaca
        result = self._get_alpaca_snapshot(symbol, timeframe, limit)
        return {**result, "source": "alpaca"}

    def _get_yahoo_snapshot(self, symbol: str, timeframe: str = "1Day", limit: int = 5) -> Dict[str, Any]:
        """Fetch market data from Yahoo Finance (no API key required)."""
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d"
            response = requests.get(url, headers=_YAHOO_HEADERS, timeout=15)
            response.raise_for_status()
            data = response.json()

            result = data.get("chart", {}).get("result", [])
            if not result:
                return {"symbol": symbol, "timeframe": timeframe, "available": False, "bars": []}

            chart = result[0]
            timestamps = chart.get("timestamp", [])
            quotes = chart.get("indicators", {}).get("quote", [{}])[0]
            meta = chart.get("meta", {})

            if not timestamps or not quotes:
                return {"symbol": symbol, "timeframe": timeframe, "available": False, "bars": []}

            # Build bars, keeping only complete bars (non-null close)
            bars = []
            closes = quotes.get("close", [])
            for i, ts in enumerate(timestamps):
                close_val = closes[i] if i < len(closes) else None
                if close_val is None:
                    continue
                bars.append({
                    "time": ts,
                    "open": quotes.get("open", [None])[i],
                    "high": quotes.get("high", [None])[i],
                    "low": quotes.get("low", [None])[i],
                    "close": close_val,
                    "volume": quotes.get("volume", [None])[i] or 0,
                })

            if not bars:
                return {"symbol": symbol, "timeframe": timeframe, "available": False, "bars": []}

            latest_bar = bars[-1]
            previous_bar = bars[-2] if len(bars) > 1 else bars[-1]
            current_close = float(latest_bar["close"])
            previous_close = float(previous_bar["close"])
            change_pct = ((current_close - previous_close) / previous_close * 100.0) if previous_close else 0.0
            # Use all historical bars for average volume (excludes today's partial bar)
            historical_bars = bars[:-1] if len(bars) > 1 else bars
            average_volume = sum(float(b.get("volume", 0)) for b in historical_bars) / len(historical_bars)

            current_price = meta.get("regularMarketPrice") or current_close

            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "available": True,
                "bars": bars,
                "latest_bar": latest_bar,
                "latest_quote": {
                    "bid": None,
                    "ask": None,
                    "midpoint": float(current_price),
                },
                "latest_price": float(current_price),
                "previous_close": previous_close,
                "change_pct": round(change_pct, 4),
                "average_volume": round(average_volume, 2),
            }
        except Exception as exc:
            return {"symbol": symbol, "timeframe": timeframe, "available": False, "error": str(exc), "bars": []}

    def _get_alpaca_snapshot(self, symbol: str, timeframe: str = "1Day", limit: int = 5) -> Dict[str, Any]:
        """Fetch market data from Alpaca API."""
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }

        try:
            bars_response = self._session.get(
                f"{settings.alpaca_data_base_url}/stocks/{symbol}/bars",
                headers=headers,
                params={"timeframe": "1Day", "limit": limit, "adjustment": "raw"},
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
            # Raw Alpaca bars use 'v' for volume before normalization
            average_volume = sum(float(bar.get("v", 0.0)) for bar in bars) / len(bars) if bars else 0.0

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