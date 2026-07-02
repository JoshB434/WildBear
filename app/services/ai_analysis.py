import json
import os
from typing import Any, Dict

from openai import OpenAI

from app.config import settings


class AITradingAnalysisService:
    def __init__(self) -> None:
        self._client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def analyze(
        self,
        symbol: str,
        timeframe: str,
        notes: str | None = None,
        market_data: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        market_data_text = self._format_market_data(market_data)
        if self._client is not None:
            try:
                response = self._client.chat.completions.create(
                    model=os.getenv("OPENAI_MODEL", "gpt-4-mini"),
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a cautious stock trading assistant. Use the live market snapshot and trading note together. Return JSON with symbol, signal (buy/sell/hold), confidence (0-1), and rationale. Only recommend a buy or sell when the evidence is strong.",
                        },
                        {
                            "role": "user",
                            "content": f"Analyze {symbol} for {timeframe}. Trading note: {notes or 'No notes provided'}. Live market snapshot: {market_data_text}. Respond with JSON only.",
                        },
                    ],
                )
                text = response.choices[0].message.content.strip()
                if text.startswith("```"):
                    text = text.strip("`").strip()
                parsed = self._parse_json_response(text)
                if parsed:
                    return {
                        "symbol": symbol.upper(),
                        "timeframe": timeframe,
                        "notes": notes or "",
                        "signal": str(parsed.get("signal", "hold")).lower(),
                        "confidence": float(parsed.get("confidence", 0.0)),
                        "model": f"openai:{os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')}",
                        "rationale": parsed.get("rationale", ""),
                    }
            except Exception:
                pass

        notes_text = (notes or "").lower()
        market_text = market_data_text.lower()
        if "buy" in notes_text and "breakout" in notes_text:
            signal = "buy"
            confidence = 0.82
        elif "sell" in notes_text and "breakout" in notes_text:
            signal = "sell"
            confidence = 0.82
        elif "sell" in notes_text:
            signal = "sell"
            confidence = 0.74
        elif "buy" in notes_text:
            signal = "buy"
            confidence = 0.72
        elif market_data and isinstance(market_data, dict):
            change_pct = float(market_data.get("change_pct") or 0.0)
            if change_pct > 0:
                signal = "buy"
                confidence = 0.7
            elif change_pct < 0:
                signal = "sell"
                confidence = 0.7
            else:
                signal = "hold"
                confidence = 0.68
        else:
            signal = "hold"
            confidence = 0.68

        return {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": signal,
            "confidence": confidence,
            "model": "local-rule-based",
        }

    def _format_market_data(self, market_data: Dict[str, Any] | None) -> str:
        if not market_data:
            return "No live market data available."

        try:
            return json.dumps(market_data, default=str, sort_keys=True)
        except Exception:
            return str(market_data)

    def _parse_json_response(self, text: str) -> Dict[str, Any] | None:
        try:
            return json.loads(text)
        except Exception:
            return None


aitrading_analysis_service = AITradingAnalysisService()
