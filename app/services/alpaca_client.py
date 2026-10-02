import json
from datetime import datetime, timezone
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

    def list_open_orders(self, symbol: str | None = None) -> list[Dict[str, Any]]:
        """Return currently open orders from Alpaca."""
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        params: Dict[str, Any] = {"status": "open", "direction": "desc", "limit": 200}
        if symbol:
            params["symbols"] = symbol.upper()
        try:
            response = self._session.get(
                f"{settings.alpaca_base_url}/orders",
                headers=headers,
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []
        except requests.RequestException:
            return []

    def get_last_filled_buy_price(self, symbol: str) -> float | None:
        """Fetch the fill price of the most recent filled buy order from broker history."""
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        params: Dict[str, Any] = {
            "status": "closed",
            "direction": "desc",
            "limit": 50,
            "symbols": symbol.upper(),
        }
        try:
            response = self._session.get(
                f"{settings.alpaca_base_url}/orders",
                headers=headers,
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            orders = response.json()
            if not isinstance(orders, list):
                return None

            for order in orders:
                if str(order.get("side") or "").lower() != "buy":
                    continue
                if str(order.get("status") or "").lower() != "filled":
                    continue
                filled_avg_price = order.get("filled_avg_price")
                if filled_avg_price is None:
                    continue
                try:
                    return float(filled_avg_price)
                except (TypeError, ValueError):
                    continue
            return None
        except requests.RequestException:
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by id. Returns True when cancel request is accepted."""
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.delete(
                f"{settings.alpaca_base_url}/orders/{order_id}",
                headers=headers,
                timeout=15,
            )
            return response.status_code in {200, 202, 204}
        except requests.RequestException:
            return False

    def _is_bot_protective_stop(self, order: Dict[str, Any], symbol: str) -> bool:
        """Identify bot-created protective stop orders for a symbol."""
        if str(order.get("symbol") or "").upper() != symbol.upper():
            return False
        if str(order.get("side") or "").lower() != "sell":
            return False
        order_type = str(order.get("type") or "").lower()
        if order_type not in {"stop", "stop_limit"}:
            return False
        client_order_id = str(order.get("client_order_id") or "")
        return client_order_id.startswith(f"bot-stop-{symbol.upper()}-")

    def _extract_order_qty(self, order: Dict[str, Any]) -> int:
        raw_qty = order.get("qty")
        if raw_qty is None:
            raw_qty = order.get("quantity")
        try:
            return max(0, int(float(raw_qty or 0)))
        except (TypeError, ValueError):
            return 0

    def _extract_order_stop_price(self, order: Dict[str, Any]) -> float:
        raw_price = order.get("stop_price")
        if raw_price is None:
            raw_price = order.get("stopPrice")
        try:
            return float(raw_price or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def cancel_existing_protective_stops(self, symbol: str) -> Dict[str, Any]:
        """Cancel existing open bot-created protective stops for a symbol."""
        open_orders = self.list_open_orders(symbol)
        candidates = [o for o in open_orders if self._is_bot_protective_stop(o, symbol)]
        cancelled_ids: list[str] = []
        failed_ids: list[str] = []

        for order in candidates:
            order_id = str(order.get("id") or "")
            if not order_id:
                continue
            if self.cancel_order(order_id):
                cancelled_ids.append(order_id)
            else:
                failed_ids.append(order_id)

        return {
            "symbol": symbol.upper(),
            "candidates": len(candidates),
            "cancelled": len(cancelled_ids),
            "cancelled_order_ids": cancelled_ids,
            "failed": len(failed_ids),
            "failed_order_ids": failed_ids,
        }

    def submit_protective_stop_sell(
        self,
        symbol: str,
        quantity: int,
        reference_price: float,
        stop_loss_pct: float,
    ) -> Dict[str, Any]:
        """Submit a protective stop-sell order for an existing long position."""
        safe_stop_pct = max(0.001, min(0.5, float(stop_loss_pct)))
        stop_price = round(reference_price * (1.0 - safe_stop_pct), 2)
        return self._submit_protective_stop_sell_at_price(symbol, quantity, stop_price, safe_stop_pct)

    def _submit_protective_stop_sell_at_price(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        stop_loss_pct: float | None = None,
    ) -> Dict[str, Any]:
        """Submit a protective stop-sell order at an explicit stop price."""
        if stop_price <= 0:
            return {
                "symbol": symbol.upper(),
                "side": "sell",
                "quantity": quantity,
                "status": "blocked",
                "reason": "invalid-stop-price",
                "broker": "alpaca-paper",
            }

        payload = {
            "symbol": symbol.upper(),
            "qty": str(max(1, int(quantity))),
            "side": "sell",
            "type": "stop",
            "time_in_force": "gtc",
            "stop_price": str(stop_price),
            "client_order_id": f"bot-stop-{symbol.upper()}-{int(datetime.now(timezone.utc).timestamp())}",
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
            return {
                "symbol": order_data.get("symbol", symbol.upper()),
                "side": order_data.get("side", "sell"),
                "quantity": int(quantity),
                "status": broker_status,
                "broker": "alpaca-paper",
                "configured": True,
                "order_id": order_data.get("id"),
                "type": "stop",
                "stop_price": stop_price,
                "stop_loss_pct": stop_loss_pct,
            }
        except requests.RequestException as exc:
            return {
                "symbol": symbol.upper(),
                "side": "sell",
                "quantity": int(quantity),
                "status": "queued",
                "broker": "alpaca-paper",
                "configured": True,
                "type": "stop",
                "stop_price": stop_price,
                "stop_loss_pct": stop_loss_pct,
                "error": str(exc),
            }

    def upsert_consolidated_protective_stop(
        self,
        symbol: str,
        reference_price: float,
        stop_loss_pct: float,
        fallback_quantity: int,
    ) -> Dict[str, Any]:
        """Keep tighter existing stops; otherwise cancel older stops and place one consolidated stop."""
        safe_stop_pct = max(0.001, min(0.5, float(stop_loss_pct)))
        new_stop_price = round(reference_price * (1.0 - safe_stop_pct), 2)

        quantity = int(fallback_quantity)
        snapshot = self.get_position_snapshot(symbol)
        if snapshot is not None:
            position_qty = int(float(snapshot.get("qty") or 0.0))
            if position_qty > 0:
                quantity = position_qty

        if quantity <= 0:
            return {
                "symbol": symbol.upper(),
                "status": "blocked",
                "reason": "no-position-for-protective-stop",
                "replaced_existing_stops": {
                    "symbol": symbol.upper(),
                    "candidates": 0,
                    "cancelled": 0,
                    "cancelled_order_ids": [],
                    "failed": 0,
                    "failed_order_ids": [],
                },
            }

        existing_orders = self.list_open_orders(symbol)
        existing_stops = [order for order in existing_orders if self._is_bot_protective_stop(order, symbol)]
        if existing_stops:
            tightest_existing_stop = max(self._extract_order_stop_price(order) for order in existing_stops)
            covered_qty = sum(self._extract_order_qty(order) for order in existing_stops)
            if tightest_existing_stop > 0 and tightest_existing_stop >= new_stop_price:
                if covered_qty >= quantity:
                    return {
                        "symbol": symbol.upper(),
                        "side": "sell",
                        "quantity": covered_qty,
                        "status": "kept-existing",
                        "broker": "alpaca-paper",
                        "configured": True,
                        "type": "stop",
                        "stop_price": tightest_existing_stop,
                        "stop_loss_pct": safe_stop_pct,
                        "reason": "tighter-existing-stop",
                        "replaced_existing_stops": {
                            "symbol": symbol.upper(),
                            "candidates": len(existing_stops),
                            "cancelled": 0,
                            "cancelled_order_ids": [],
                            "failed": 0,
                            "failed_order_ids": [],
                        },
                    }

                supplemental_qty = quantity - covered_qty
                supplemental_stop = self._submit_protective_stop_sell_at_price(
                    symbol,
                    supplemental_qty,
                    tightest_existing_stop,
                    safe_stop_pct,
                )
                supplemental_stop["reason"] = "supplemented-at-existing-tighter-stop"
                supplemental_stop["existing_covered_qty"] = covered_qty
                supplemental_stop["target_total_qty"] = quantity
                supplemental_stop["replaced_existing_stops"] = {
                    "symbol": symbol.upper(),
                    "candidates": len(existing_stops),
                    "cancelled": 0,
                    "cancelled_order_ids": [],
                    "failed": 0,
                    "failed_order_ids": [],
                }
                return supplemental_stop

        cancel_result = self.cancel_existing_protective_stops(symbol)
        stop_order = self.submit_protective_stop_sell(symbol, quantity, reference_price, safe_stop_pct)
        stop_order["replaced_existing_stops"] = cancel_result
        return stop_order

    def ai_adjusted_stop_loss_pct(self, base_stop_loss_pct: float, ai_confidence: float | None) -> float:
        """Adjust stop-loss width from AI confidence so weaker signals get tighter protection."""
        base = max(0.001, min(0.5, float(base_stop_loss_pct)))
        if ai_confidence is None:
            return base

        confidence = max(0.0, min(1.0, float(ai_confidence)))
        if confidence < 0.7:
            factor = 0.8
        elif confidence > 0.9:
            factor = 1.1
        else:
            factor = 1.0

        return max(0.001, min(0.5, base * factor))

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

    def get_position_snapshot(self, symbol: str) -> Dict[str, Any] | None:
        """Get broker position details needed for sell-side guardrails."""
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
            return {
                "symbol": symbol.upper(),
                "qty": float(position_data.get("qty") or 0.0),
                "avg_entry_price": float(position_data.get("avg_entry_price") or 0.0),
                "current_price": float(position_data.get("current_price") or 0.0),
                "unrealized_plpc": float(position_data.get("unrealized_plpc") or 0.0),
            }
        except requests.RequestException:
            return None

    def evaluate_sell_profit_guard(
        self,
        symbol: str,
        candidate_price: float | None,
        min_profit_pct: float,
        stop_loss_pct: float,
        last_buy_price: float | None = None,
        require_sell_above_last_buy: bool = False,
    ) -> Dict[str, Any]:
        """Decide whether a sell should be blocked for being below entry target or the last buy price."""
        snapshot = self.get_position_snapshot(symbol)
        quantity = float(snapshot.get("qty") or 0.0) if snapshot else 0.0
        avg_entry = float(snapshot.get("avg_entry_price") or 0.0) if snapshot else 0.0
        has_position = snapshot is not None and quantity > 0

        # Without a broker position at all, fall back to the last-buy-price floor if we have one.
        if not has_position and not (require_sell_above_last_buy and last_buy_price):
            return {"allowed": True, "reason": "no-open-position"}

        snapshot_price = float(snapshot.get("current_price") or 0.0) if snapshot else 0.0
        market_price = candidate_price or snapshot_price
        if market_price <= 0:
            return {"allowed": True, "reason": "candidate-price-unavailable"}

        min_take_profit_price = avg_entry * (1.0 + max(0.0, min_profit_pct)) if avg_entry > 0 else 0.0
        stop_loss_trigger_price = avg_entry * (1.0 - max(0.0, stop_loss_pct)) if avg_entry > 0 else 0.0

        # The effective floor is the stricter (higher) of the cost-basis target and the last buy price,
        # so a sell must clear whichever bar is higher.
        effective_floor_price = min_take_profit_price
        if require_sell_above_last_buy and last_buy_price is not None and last_buy_price > 0:
            last_buy_floor_price = last_buy_price * (1.0 + max(0.0, min_profit_pct))
            effective_floor_price = max(effective_floor_price, last_buy_floor_price)

        if effective_floor_price <= 0:
            return {"allowed": True, "reason": "no-reference-price-available"}

        guard_details = {
            "avg_entry_price": avg_entry,
            "candidate_price": market_price,
            "min_take_profit_price": min_take_profit_price,
            "stop_loss_trigger_price": stop_loss_trigger_price,
            "last_buy_price": last_buy_price,
            "effective_floor_price": effective_floor_price,
        }

        if market_price >= effective_floor_price:
            return {"allowed": True, "reason": "profit-target-satisfied", **guard_details}

        if stop_loss_pct > 0 and stop_loss_trigger_price > 0 and market_price <= stop_loss_trigger_price:
            return {"allowed": True, "reason": "stop-loss-override", **guard_details}

        reason = (
            "below-last-buy-price"
            if require_sell_above_last_buy and last_buy_price is not None and effective_floor_price > min_take_profit_price
            else "below-profit-floor"
        )
        return {"allowed": False, "reason": reason, **guard_details}

    def _count_consecutive_buy_orders(self, symbol: str) -> int:
        """
        Count consecutive buy orders for a symbol since the last sell.
        Returns the number of buy orders that should determine the allocation tier.
        """
        try:
            orders = trading_store.list_orders()
            symbol_orders = [o for o in orders if o.symbol.upper() == symbol.upper()]
            
            if not symbol_orders:
                return 0
            
            # Sort by creation time (newest first)
            symbol_orders.sort(key=lambda o: o.created_at, reverse=True)
            
            # Find the most recent SELL order
            last_sell_idx = None
            for i, order in enumerate(symbol_orders):
                if order.side.lower() == "sell":
                    last_sell_idx = i
                    break
            
            # Count BUY orders after the last SELL
            buy_count = 0
            for i, order in enumerate(symbol_orders):
                if last_sell_idx is not None and i >= last_sell_idx:
                    # Skip orders at or before the last sell
                    continue
                if order.side.lower() == "buy":
                    buy_count += 1
            
            return buy_count
        except Exception:
            return 0

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
        Dynamic position sizing based on consecutive buy orders since last sell:
        - 1st buy signal (no prior buys or after sell): 25% of account
        - 2nd buy signal (1 prior buy): 25% of account
        - 3rd+ buy signals (2+ prior buys): 7.5% of account
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
            
            # Get positions for current portfolio investment tracking
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
            
            # Count consecutive buy orders for this symbol since last sell (for tier determination)
            num_consecutive_buys = self._count_consecutive_buy_orders(symbol)
            
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
            
            # Determine allocation percentage based on consecutive buy count
            if num_consecutive_buys == 0:
                # First buy: configured percentage of account
                allocation_pct = first_buy_pct
            elif num_consecutive_buys == 1:
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

    # ---- Strict REST helpers: raise on failure so callers can reconcile instead of guessing ----

    def _strict_request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        body: Dict[str, Any] | None = None,
        data_api: bool = False,
    ) -> Any:
        base = settings.alpaca_data_base_url if data_api else settings.alpaca_base_url
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.request(
                method,
                f"{base}{path}",
                headers=headers,
                params=params,
                data=json.dumps(body) if body is not None else None,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise BrokerAmbiguous(f"{type(exc).__name__}: {exc}") from exc

        status = response.status_code
        if status == 404:
            raise BrokerNotFound(path)
        if status == 429 or status >= 500:
            raise BrokerAmbiguous(f"HTTP {status}")
        if status >= 400:
            raise BrokerRejected(status, response.text[:500])
        if status == 204 or not response.content:
            return None
        return response.json()

    def get_account_strict(self) -> Dict[str, Any]:
        return self._strict_request("GET", "/account")

    def get_clock(self) -> Dict[str, Any]:
        return self._strict_request("GET", "/clock")

    def get_position_strict(self, symbol: str) -> Dict[str, Any] | None:
        """Return the broker position, or None when the account is flat in the symbol."""
        try:
            return self._strict_request("GET", f"/positions/{symbol.upper()}")
        except BrokerNotFound:
            return None

    def list_orders_strict(self, symbol: str, status: str = "open", after: str | None = None) -> list[Dict[str, Any]]:
        """List orders with bracket/OCO legs flattened (and de-duplicated) into the result."""
        params: Dict[str, Any] = {
            "status": status, "symbols": symbol.upper(), "nested": "true", "direction": "desc", "limit": 100,
        }
        if after:
            params["after"] = after
        raw = self._strict_request("GET", "/orders", params=params)
        flat: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for order in raw if isinstance(raw, list) else []:
            for item in [order, *(order.get("legs") or [])]:
                if item.get("id") not in seen:
                    seen.add(item.get("id"))
                    flat.append(item)
        return flat

    def get_order_strict(self, order_id: str) -> Dict[str, Any]:
        return self._strict_request("GET", f"/orders/{order_id}", params={"nested": "true"})

    def get_order_by_client_id(self, client_order_id: str) -> Dict[str, Any] | None:
        try:
            return self._strict_request(
                "GET", "/orders:by_client_order_id", params={"client_order_id": client_order_id}
            )
        except BrokerNotFound:
            return None

    def submit_bracket_buy(
        self, symbol: str, qty: int, stop_price: float, target_price: float, client_order_id: str
    ) -> Dict[str, Any]:
        return self._strict_request(
            "POST",
            "/orders",
            body={
                "symbol": symbol.upper(),
                "qty": str(int(qty)),
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": f"{target_price:.2f}"},
                "stop_loss": {"stop_price": f"{stop_price:.2f}"},
                "client_order_id": client_order_id,
            },
        )

    def submit_oco_sell(
        self, symbol: str, qty: int, stop_price: float, target_price: float, client_order_id: str
    ) -> Dict[str, Any]:
        return self._strict_request(
            "POST",
            "/orders",
            body={
                "symbol": symbol.upper(),
                "qty": str(int(qty)),
                "side": "sell",
                "type": "limit",
                "time_in_force": "gtc",
                "order_class": "oco",
                "take_profit": {"limit_price": f"{target_price:.2f}"},
                "stop_loss": {"stop_price": f"{stop_price:.2f}"},
                "client_order_id": client_order_id,
            },
        )

    def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Dict[str, Any]:
        return self._strict_request(
            "POST",
            "/orders",
            body={
                "symbol": symbol.upper(),
                "qty": str(int(qty)),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            },
        )

    def cancel_order_strict(self, order_id: str) -> bool:
        """Request cancellation. False means the order was already gone or not cancelable (e.g. filled)."""
        try:
            self._strict_request("DELETE", f"/orders/{order_id}")
            return True
        except (BrokerNotFound, BrokerRejected):
            return False

    def close_position_strict(self, symbol: str) -> Dict[str, Any] | None:
        try:
            return self._strict_request("DELETE", f"/positions/{symbol.upper()}")
        except BrokerNotFound:
            return None

    def get_latest_quote(self, symbol: str) -> Dict[str, Any]:
        raw = self._strict_request("GET", f"/stocks/{symbol.upper()}/quotes/latest", data_api=True)
        return (raw or {}).get("quote") or {}


class BrokerError(Exception):
    pass


class BrokerAmbiguous(BrokerError):
    """Outcome unknown (timeout, connection loss, 5xx, 429): the request may or may not have taken effect."""


class BrokerRejected(BrokerError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class BrokerNotFound(BrokerError):
    pass


alpaca_paper_broker = AlpacaPaperBroker()
