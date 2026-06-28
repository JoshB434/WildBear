from typing import Any, Dict

from app.config import settings


class AITradingAnalysisService:
    def analyze(self, symbol: str, timeframe: str, notes: str | None = None) -> Dict[str, Any]:
        return {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "buy" if "breakout" in (notes or "").lower() else "hold",
            "confidence": 0.78,
            "model": "openai-compatible" if settings.openai_api_key else "local-rule-based",
        }


aitrading_analysis_service = AITradingAnalysisService()
