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
        
        # Route to primary source first, with fallback
        primary_source = settings.market_data_primary_source
        
        if primary_source == "yahoo":
            result = self._get_yahoo_snapshot(symbol, timeframe, limit)
            if result.get("available"):
                return {**result, "source": "yahoo"}
            # Fall back to Alpaca if Yahoo fails
            result = self._get_alpaca_snapshot(symbol, timeframe, limit)
            return {**result, "source": "alpaca"}
        else:
            # Alpaca is primary, Yahoo is fallback
            result = self._get_alpaca_snapshot(symbol, timeframe, limit)
            if result.get("available"):
                return {**result, "source": "alpaca"}
            result = self._get_yahoo_snapshot(symbol, timeframe, limit)
            return {**result, "source": "yahoo"}

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

    def _get_yahoo_snapshot(self, symbol: str, timeframe: str = "1Day", limit: int = 5) -> Dict[str, Any]:
        """Fetch market data from Yahoo Finance."""
        try:
            # Yahoo Finance endpoint via public API
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
            response = self._session.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            # Extract chart data
            result = data.get("chart", {}).get("result", [{}])[0]
            timestamps = result.get("timestamp", [])
            quotes = result.get("indicators", {}).get("quote", [{}])[0]
            
            if not timestamps or not quotes:
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "available": False,
                    "bars": [],
                }
            
            # Build bars
            bars = []
            for i, ts in enumerate(timestamps):
                bar = {
                    "time": ts,
                    "open": quotes.get("open", [None])[i],
                    "high": quotes.get("high", [None])[i],
                    "low": quotes.get("low", [None])[i],
                    "close": quotes.get("close", [None])[i],
                    "volume": quotes.get("volume", [None])[i],
                }
                # Only include bars with valid data
                if all(bar[k] is not None for k in ["open", "high", "low", "close", "volume"]):
                    bars.append(bar)
            
            if not bars:
                return {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "available": False,
                    "bars": [],
                }
            
            latest_bar = bars[-1]
            previous_bar = bars[-2] if len(bars) > 1 else bars[-1]
            
            current_close = float(latest_bar["close"])
            previous_close = float(previous_bar["close"])
            change_pct = ((current_close - previous_close) / previous_close * 100.0) if previous_close else 0.0
            average_volume = sum(float(bar.get("volume", 0)) for bar in bars) / len(bars) if bars else 0.0
            
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "available": True,
                "bars": bars,
                "latest_bar": latest_bar,
                "latest_quote": {
                    "bid": None,
                    "ask": None,
                    "midpoint": current_close,
                },
                "latest_price": current_close,
                "previous_close": previous_close,
                "change_pct": round(change_pct, 4),
                "average_volume": round(average_volume, 2),
            }
        except Exception as exc:
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