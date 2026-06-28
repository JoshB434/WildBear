import os
from typing import Any, Dict

from openai import OpenAI

from app.config import settings


class AITradingAnalysisService:
    def __init__(self) -> None:
        self._client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def analyze(self, symbol: str, timeframe: str, notes: str | None = None) -> Dict[str, Any]:
        if self._client is not None:
            try:
                response = self._client.responses.create(
                    model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                    input=[
                        {
                            "role": "system",
                            "content": "You are a cautious stock trading assistant. Return JSON with symbol, signal (buy/sell/hold), confidence (0-1), and rationale. Only recommend a buy or sell when the evidence is strong.",
                        },
                        {
                            "role": "user",
                            "content": f"Analyze {symbol} for {timeframe} using this trading note: {notes or 'No notes provided'}. Respond with JSON only.",
                        },
                    ],
                )
                text = response.output_text.strip()
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
        if "buy" in notes_text and "breakout" in notes_text:
            signal = "buy"
            confidence = 0.82
        elif "sell" in notes_text and "breakout" in notes_text:
            signal = "hold"
            confidence = 0.72
        elif "sell" in notes_text:
            signal = "sell"
            confidence = 0.74
        elif "buy" in notes_text:
            signal = "buy"
            confidence = 0.72
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

    def _parse_json_response(self, text: str) -> Dict[str, Any] | None:
        import json

        try:
            return json.loads(text)
        except Exception:
            return None


aitrading_analysis_service = AITradingAnalysisService()
