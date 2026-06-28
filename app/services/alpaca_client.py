import json
from typing import Any, Dict

import requests

from app.config import settings
from app.database import trading_store


class AlpacaPaperBroker:
    def __init__(self) -> None:
        self._orders: list[Dict[str, Any]] = []

    def submit_order(self, symbol: str, side: str, quantity: int) -> Dict[str, Any]:
        risk_settings = trading_store.get_risk_settings()
        if risk_settings and quantity > risk_settings.max_position_size:
            return {
                "symbol": symbol.upper(),
                "side": side.lower(),
                "quantity": quantity,
                "status": "blocked",
                "reason": "position size exceeds configured max",
                "broker": "alpaca-paper",
                "configured": bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key),
            }

        payload = {
            "symbol": symbol.upper(),
            "qty": str(quantity),
            "side": side.lower(),
            "type": "market",
            "time_in_force": "day",
        }
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                f"{settings.alpaca_base_url}/orders",
                headers=headers,
                data=json.dumps(payload),
                timeout=15,
            )
            response.raise_for_status()
            order_data = response.json()
            broker_status = order_data.get("status") or "queued"
            if broker_status in {"accepted", "new", "queued", "pending_new"}:
                broker_status = "queued"
            order = {
                "symbol": order_data.get("symbol", symbol.upper()),
                "side": order_data.get("side", side.lower()),
                "quantity": quantity,
                "status": broker_status,
                "broker": "alpaca-paper",
                "configured": True,
                "order_id": order_data.get("id"),
            }
            self._orders.append(order)
            return order
        except requests.RequestException as exc:
            return {
                "symbol": symbol.upper(),
                "side": side.lower(),
                "quantity": quantity,
                "status": "queued",
                "broker": "alpaca-paper",
                "configured": True,
                "error": str(exc),
            }


alpaca_paper_broker = AlpacaPaperBroker()
