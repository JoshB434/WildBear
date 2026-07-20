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
        # Risk check: Only apply position size limit to BUY orders
        # Sell orders should be allowed to sell any held position
        if side.lower() == "buy":
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

    def get_position(self, symbol: str) -> float | None:
        """Get current position size for a symbol."""
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.get(
                f"{settings.alpaca_base_url}/positions/{symbol.upper()}",
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            position_data = response.json()
            return float(position_data.get("qty", 0))
        except requests.RequestException:
            return None

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

    def _get_account_value(self) -> float:
        """Get total account equity (cash + positions)"""
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
            portfolio_value = float(account.get("portfolio_value", 0))
            if portfolio_value > 0:
                return portfolio_value
            # Fallback to cash only if portfolio_value not available
            cash = float(account.get("cash", 0))
            return cash if cash > 0 else self._paper_balance_fallback
        except requests.RequestException:
            return self._paper_balance_fallback

    def _get_invested_percentage(self) -> float:
        """Get percentage of account currently invested in positions"""
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        try:
            account_response = self._session.get(
                f"{settings.alpaca_base_url}/account",
                headers=headers,
                timeout=15,
            )
            account_response.raise_for_status()
            account = account_response.json()
            
            portfolio_value = float(account.get("portfolio_value", 0))
            if portfolio_value <= 0:
                return 0.0
            
            positions_response = self._session.get(
                f"{settings.alpaca_base_url}/positions",
                headers=headers,
                timeout=15,
            )
            positions_response.raise_for_status()
            positions = positions_response.json() if isinstance(positions_response.json(), list) else []
            
            total_position_value = 0.0
            for position in positions:
                position_value = float(position.get("market_value", 0))
                total_position_value += abs(position_value)
            
            invested_pct = (total_position_value / portfolio_value) * 100
            return min(invested_pct, 100.0)
        except requests.RequestException:
            return 0.0

    def submit_buy_with_balance_limit(self, symbol: str, account_balance: float | None = None, buy_pct: float = 0.15) -> Dict[str, Any]:
        """
        Dynamic position sizing based on number of active positions:
        - 1st buy signal (no positions): 25% of account
        - 2nd buy signal (1 position): 25% of account
        - 3rd+ buy signals (2+ positions): 5-10% of account
        - Max total allocation: 75% of account
        
        Args:
            symbol: Stock ticker to buy
            account_balance: Optional override account balance
            buy_pct: Fallback percentage if dynamic sizing unavailable
        """
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        
        try:
            # Get account info
            account_response = self._session.get(
                f"{settings.alpaca_base_url}/account",
                headers=headers,
                timeout=15,
            )
            account_response.raise_for_status()
            account = account_response.json()
            
            # Get positions
            positions_response = self._session.get(
                f"{settings.alpaca_base_url}/positions",
                headers=headers,
                timeout=15,
            )
            positions_response.raise_for_status()
            positions = positions_response.json() if isinstance(positions_response.json(), list) else []
            
            total_account_value = float(account.get("portfolio_value", 0))
            if total_account_value <= 0:
                total_account_value = self._paper_balance_fallback
            
            # Calculate current investment percentage
            total_position_value = 0.0
            for position in positions:
                position_value = float(position.get("market_value", 0))
                total_position_value += abs(position_value)
            
            current_invested_pct = (total_position_value / total_account_value) * 100 if total_account_value > 0 else 0.0
            num_positions = len(positions)
            
            # Get risk settings for allocation percentages
            risk_settings = trading_store.get_risk_settings()
            if risk_settings:
                first_buy_pct = risk_settings.first_buy_allocation_pct
                second_buy_pct = risk_settings.second_buy_allocation_pct
                subsequent_buy_pct = risk_settings.subsequent_buy_allocation_pct
                max_allocation = risk_settings.max_total_allocation_pct
            else:
                # Defaults
                first_buy_pct = 25.0
                second_buy_pct = 25.0
                subsequent_buy_pct = 7.5
                max_allocation = 75.0
            
            # Determine allocation percentage based on tier
            if num_positions == 0:
                # First buy: configured percentage of account
                allocation_pct = first_buy_pct
            elif num_positions == 1:
                # Second buy: configured percentage of account (additional)
                allocation_pct = second_buy_pct
            else:
                # Subsequent buys: configured percentage
                allocation_pct = subsequent_buy_pct
            
            # Check if adding this allocation would exceed max
            projected_invested = current_invested_pct + allocation_pct
            if projected_invested > max_allocation:
                # Scale back to not exceed max
                remaining_allocation = max_allocation - current_invested_pct
                if remaining_allocation <= 0:
                    return {
                        "symbol": symbol.upper(),
                        "side": "buy",
                        "quantity": 0,
                        "status": "blocked",
                        "reason": f"account already at {current_invested_pct:.1f}% allocation (max {max_allocation}%)",
                        "broker": "alpaca-paper",
                        "configured": bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key),
                    }
                allocation_pct = remaining_allocation
            
            # Calculate dollar amount and quantity
            max_dollar_amount = total_account_value * (allocation_pct / 100.0)
            quantity = max(1, int(max_dollar_amount // 100))
            
            # Apply per-share max_position_size limit if configured (as fallback safety)
            if risk_settings is not None and risk_settings.max_position_size > 0:
                quantity = min(quantity, risk_settings.max_position_size)
            
            return self.submit_order(symbol, "buy", quantity)
        
        except requests.RequestException:
            # Fallback to simpler calculation
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
            risk_settings = trading_store.get_risk_settings()
            if risk_settings is not None:
                quantity = min(quantity, risk_settings.max_position_size)
            else:
                quantity = min(quantity, 5)
            return self.submit_order(symbol, "buy", quantity)

    def submit_sell_all(self, symbol: str) -> Dict[str, Any]:
        """Sell entire position for a symbol. Fetches current position size from broker."""
        position = self.get_position(symbol)
        if position is None or position <= 0:
            return {
                "symbol": symbol.upper(),
                "side": "sell",
                "quantity": 0,
                "status": "blocked",
                "reason": "no position to sell",
                "broker": "alpaca-paper",
                "configured": bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key),
            }
        return self.submit_order(symbol, "sell", int(position))


alpaca_paper_broker = AlpacaPaperBroker()
