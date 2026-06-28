from typing import Any, Dict

from app.database import trading_store
from app.schemas import AlertCreate


class TradingViewAlertService:
    def __init__(self) -> None:
        self._alerts: list[Dict[str, Any]] = []

    def receive_alert(self, ticker: str, action: str, price: float | None = None, strategy: str | None = None) -> Dict[str, Any]:
        alert = {
            "ticker": ticker.upper(),
            "action": action.lower(),
            "price": price,
            "strategy": strategy,
            "received": True,
        }
        self._alerts.append(alert)
        trading_store.create_alert(AlertCreate(ticker=ticker, action=action, price=price, strategy=strategy))
        return alert


tradingview_alert_service = TradingViewAlertService()
