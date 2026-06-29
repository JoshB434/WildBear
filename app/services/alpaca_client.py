import json
from typing import Any, Dict

import requests

from app.config import settings
from app.database import trading_store


class AlpacaPaperBroker:
    def __init__(self) -> None:
        self._orders: list[Dict[str, Any]] = []
        self._session = requests.Session()
        self._session.trust_env = False
        self._paper_balance_fallback = 10000.0

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
            response = self._session.post(
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

    def get_account_balance(self) -> float:
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.get(
                f"{settings.alpaca_base_url}/account",
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            account = response.json()
            cash = account.get("cash")
            if cash is None:
                return self._paper_balance_fallback
            balance = float(cash)
            return balance if balance > 0 else self._paper_balance_fallback
        except requests.RequestException:
            return self._paper_balance_fallback

    def submit_buy_with_balance_limit(self, symbol: str, account_balance: float | None = None, buy_pct: float = 0.15) -> Dict[str, Any]:
        balance = account_balance if account_balance is not None else self.get_account_balance()
        if balance <= 0:
            balance = self._paper_balance_fallback

        max_dollar_amount = balance * max(0.0, min(1.0, buy_pct))
        if max_dollar_amount <= 0:
            return {
                "symbol": symbol.upper(),
                "side": "buy",
                "quantity": 0,
                "status": "blocked",
                "reason": "buy percentage is invalid",
                "broker": "alpaca-paper",
                "configured": bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key),
            }

        quantity = max(1, int(max_dollar_amount // 100))
        return self.submit_order(symbol, "buy", quantity)

    def submit_sell_all(self, symbol: str) -> Dict[str, Any]:
        return self.submit_order(symbol, "sell", 999999)


alpaca_paper_broker = AlpacaPaperBroker()
