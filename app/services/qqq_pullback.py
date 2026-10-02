"""QQQ pullback (qqq_pullback_v1) webhook workflow: validate -> risk gates -> AI -> Alpaca bracket -> verified protection."""
import logging
import math
import re
import time
from datetime import datetime
from typing import Any, Callable

from app.services.alpaca_client import BrokerAmbiguous, BrokerError, BrokerNotFound, BrokerRejected
from app.services.qqq_signals import STRATEGY_ID, EntryCandidate, ExitSignal, Signal
from app.services.qqq_state import QQQStateStore

logger = logging.getLogger("qqq_pullback")

TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped"}
ACTIVE_ORDER_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "held", "partially_filled", "pending_replace"}


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def compute_quantity(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    max_position_value: float,
    buying_power: float,
    exposure_headroom: float | None = None,
    max_shares: int | None = None,
) -> dict[str, Any]:
    """Whole-share quantity bounded by risk budget, position value, buying power, exposure and share caps."""
    per_share_risk = entry_price - stop_price
    risk_budget = equity * risk_pct
    if per_share_risk <= 0 or entry_price <= 0 or risk_budget <= 0:
        return {"quantity": 0, "risk_budget": risk_budget, "per_share_risk": per_share_risk, "limit": "invalid-inputs"}
    caps = {
        "risk-budget": math.floor(risk_budget / per_share_risk),
        "max-position-value": math.floor(max_position_value / entry_price),
        "buying-power": math.floor(max(0.0, buying_power) / entry_price),
    }
    if exposure_headroom is not None:
        caps["exposure-headroom"] = math.floor(max(0.0, exposure_headroom) / entry_price)
    if max_shares is not None and max_shares > 0:
        caps["max-shares"] = int(max_shares)
    limit = min(caps, key=caps.get)
    return {
        "quantity": max(0, caps[limit]),
        "risk_budget": risk_budget,
        "per_share_risk": per_share_risk,
        "limit": limit,
        "caps": caps,
    }


class QQQPullbackWorkflow:
    def __init__(
        self,
        store: QQQStateStore,
        broker: Any,
        ai: Any,
        market_data: Any,
        cfg: Any,
        risk_settings_provider: Callable[[], Any] = lambda: None,
        log_hook: Callable[[str, str, dict], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.broker = broker
        self.ai = ai
        self.market_data = market_data
        self.cfg = cfg
        self.risk_settings_provider = risk_settings_provider
        self.log_hook = log_hook
        self.sleep = sleep
        self.now = now
        self.poll_interval = poll_interval

    # ------------------------------------------------------------------ entry point
    def handle(self, signal: Signal) -> dict[str, Any]:
        if not self.store.claim_event(signal.event_id, signal.event):
            self._log("INFO", "Duplicate event ignored", event_id=signal.event_id)
            return {"status": "duplicate", "event_id": signal.event_id}
        try:
            if isinstance(signal, EntryCandidate):
                result = self._handle_entry(signal)
            else:
                result = self._handle_exit(signal)
        except BrokerAmbiguous as exc:
            self.store.set_lockout(f"broker-state-unverifiable: {exc}")
            self._log("CRITICAL", "Broker state unverifiable; trading locked out", event_id=signal.event_id, error=str(exc))
            result = {"status": "failed", "reason": "broker-state-unverifiable"}
        except Exception as exc:  # unknown failure: block further entries until an operator reviews
            self.store.set_lockout(f"unexpected-error: {type(exc).__name__}")
            self._log("CRITICAL", "Unexpected workflow error; trading locked out", event_id=signal.event_id, error=repr(exc))
            result = {"status": "failed", "reason": "unexpected-error"}
        result["event_id"] = signal.event_id
        self.store.finish_event(signal.event_id, result.get("status", "unknown"), result)
        self.store.audit(signal.event_id, "result", result)
        self._log("INFO", "Event processed", event_id=signal.event_id, status=result.get("status"), reason=result.get("reason"))
        return result

    # ------------------------------------------------------------------ entry
    def _handle_entry(self, s: EntryCandidate) -> dict[str, Any]:
        cfg = self.cfg
        self.store.audit(s.event_id, "entry_signal", s.model_dump())

        stale = self._stale_reason(s.bar_time)
        if stale:
            return self._reject(stale)
        if not cfg.qqq_trading_enabled:
            return self._reject("trading-disabled")
        lockout = self.store.get_lockout()
        if lockout["locked"]:
            return self._reject("lockout-active", detail=lockout["reason"])

        account = self.broker.get_account_strict()
        if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
            return self._reject("account-not-tradable")
        equity, last_equity = _f(account.get("equity")), _f(account.get("last_equity"))
        if equity <= 0 or last_equity <= 0:
            raise BrokerAmbiguous("account equity unavailable")

        clock = self.broker.get_clock()
        if not clock.get("is_open"):
            return self._reject("market-closed")
        minutes_to_close = self._minutes_to_close(clock)
        if minutes_to_close is not None and minutes_to_close < cfg.qqq_no_entry_minutes_before_close:
            return self._reject("too-close-to-market-close")
        trade_date = str(clock.get("timestamp", ""))[:10]
        if len(trade_date) != 10:
            raise BrokerAmbiguous("clock timestamp unavailable")

        risk_settings = self.risk_settings_provider()
        daily_loss_pct = (last_equity - equity) / last_equity * 100.0
        loss_limit = cfg.qqq_daily_loss_limit_pct
        if risk_settings is not None:
            loss_limit = min(loss_limit, float(risk_settings.daily_loss_limit))
        if daily_loss_pct >= loss_limit:
            return self._reject("daily-loss-limit", daily_loss_pct=round(daily_loss_pct, 3), limit_pct=loss_limit)

        position = self.broker.get_position_strict(s.symbol)
        if position is not None and _f(position.get("qty")) != 0:
            return self._reject("existing-broker-position")
        open_orders = self.broker.list_orders_strict(s.symbol, "open")
        if any(str(o.get("status", "")).lower() in ACTIVE_ORDER_STATUSES for o in open_orders):
            return self._reject("open-order-exists")

        client_order_id = f"qqq-entry-{s.event_id}"[:128]
        pos_id, reason = self.store.reserve_position(
            s.symbol, STRATEGY_ID, s.event_id, client_order_id, trade_date, cfg.qqq_max_trades_per_day
        )
        if pos_id is None:
            return self._reject(reason or "reservation-failed")

        try:
            return self._execute_entry(
                s, pos_id, client_order_id, account, equity, daily_loss_pct, risk_settings
            )
        except BrokerAmbiguous:
            self.store.update_position(pos_id, status="ambiguous")
            raise
        except Exception:
            current = self.store.get_position(pos_id)
            if current and current["status"] == "reserved" and not current["order_submitted"]:
                self.store.update_position(pos_id, status="failed")
            raise

    def _execute_entry(self, s, pos_id, client_order_id, account, equity, daily_loss_pct, risk_settings) -> dict[str, Any]:
        cfg = self.cfg

        def release(reason: str, **extra: Any) -> dict[str, Any]:
            self.store.update_position(pos_id, status="failed")
            return self._reject(reason, **extra)

        priced = self._expected_entry_price(s.symbol)
        if priced is None:
            return release("no-current-price")
        expected_entry, price_source = priced
        drift_pct = abs(expected_entry - s.price) / s.price * 100.0
        if drift_pct > cfg.qqq_max_entry_slippage_pct:
            return release("entry-price-drifted", expected_entry=expected_entry, drift_pct=round(drift_pct, 3))
        if not s.stop_price < expected_entry < s.target_price:
            return release("levels-invalid-for-expected-entry", expected_entry=expected_entry)

        headroom = None
        max_shares = None
        if risk_settings is not None:
            headroom = equity * float(risk_settings.max_total_allocation_pct) / 100.0 - abs(_f(account.get("long_market_value")))
            max_shares = int(risk_settings.max_position_size)
        sizing = compute_quantity(
            equity, cfg.qqq_risk_pct, expected_entry, s.stop_price, cfg.qqq_max_position_value,
            _f(account.get("buying_power")), headroom, max_shares,
        )
        qty = sizing["quantity"]
        if qty < 1:
            return release("quantity-below-one-share", sizing=sizing)

        context = {
            "strategy": STRATEGY_ID,
            "signal": s.model_dump(),
            "expected_entry_price": expected_entry,
            "proposed_quantity": qty,
            "stop_price": s.stop_price,
            "target_price": s.target_price,
            "risk_budget": sizing["risk_budget"],
            "per_share_risk": sizing["per_share_risk"],
            "reward_to_risk": round((s.target_price - expected_entry) / sizing["per_share_risk"], 3),
            "account": {"equity": equity, "daily_pnl_pct": round(-daily_loss_pct, 3)},
            "position_state": "flat",
            "market": self._market_context(s.symbol),
            "rules": "long-only; exit signals only close longs; stop/target/size are fixed by code",
        }
        decision = self.ai.evaluate_entry_candidate(context)
        self.store.audit(s.event_id, "ai_decision", decision)
        min_conf = cfg.qqq_min_ai_confidence
        if risk_settings is not None:
            min_conf = max(min_conf, float(risk_settings.min_ai_confidence_buy))
        if decision.get("approve") is not True or _f(decision.get("confidence")) < min_conf:
            return release("ai-rejected", ai=decision)

        # Re-check lockout: another request may have tripped it while the AI was running.
        if self.store.get_lockout()["locked"]:
            return release("lockout-active")

        self.store.update_position(
            pos_id, qty=qty, stop_price=s.stop_price, target_price=s.target_price, order_submitted=1,
            detail={"requested_qty": qty, "expected_entry": expected_entry, "price_source": price_source, "sizing": sizing},
        )
        try:
            order = self.broker.submit_bracket_buy(s.symbol, qty, s.stop_price, s.target_price, client_order_id)
        except BrokerRejected as exc:
            self.store.audit(s.event_id, "entry_rejected_by_broker", {"error": str(exc)})
            self.store.update_position(pos_id, status="failed")
            return {"status": "rejected", "reason": "broker-rejected-entry", "detail": str(exc)}
        except BrokerAmbiguous as exc:
            order = self.broker.get_order_by_client_id(client_order_id)  # raises if still unverifiable
            if order is None:
                self.store.update_position(pos_id, status="failed")
                self.store.audit(s.event_id, "entry_not_placed", {"error": str(exc)})
                return {"status": "failed", "reason": "submission-failed-order-not-placed"}
        self.store.audit(s.event_id, "entry_submitted", {"order_id": order.get("id"), "client_order_id": client_order_id, "qty": qty})
        return self._complete_entry(pos_id, s.event_id, s.symbol, order, s.stop_price, s.target_price, qty)

    def _complete_entry(self, pos_id, event_id, symbol, order, stop, target, requested_qty) -> dict[str, Any]:
        order_id = order.get("id")
        self.store.update_position(pos_id, entry_order_id=order_id)
        order = self._await_terminal(order_id, order)
        if str(order.get("status", "")).lower() not in TERMINAL_ORDER_STATUSES:
            self.broker.cancel_order_strict(order_id)  # cancel the unfilled remainder
            order = self._await_terminal(order_id, order)
            if str(order.get("status", "")).lower() not in TERMINAL_ORDER_STATUSES:
                self.store.update_position(pos_id, status="ambiguous")
                self.store.set_lockout("entry-order-state-unverifiable")
                self._log("CRITICAL", "Entry order state unverifiable", event_id=event_id, order_id=order_id)
                return {"status": "ambiguous", "reason": "entry-order-state-unverifiable", "order_id": order_id}

        status = str(order.get("status", "")).lower()
        filled = int(_f(order.get("filled_qty")))
        if filled <= 0:
            self.store.update_position(pos_id, status="failed")
            return {"status": "rejected", "reason": f"entry-not-filled:{status}", "order_id": order_id}

        avg = _f(order.get("filled_avg_price"))
        self.store.update_position(pos_id, status="open", qty=filled, entry_price=avg)
        self.store.audit(event_id, "entry_filled", {"order_id": order_id, "filled_qty": filled, "avg_price": avg, "status": status})
        if not stop < avg < target:
            return self._failsafe(pos_id, event_id, symbol, "fill-price-invalidates-stop-target")

        protection = self._ensure_protection(pos_id, event_id, symbol, stop, target)
        if not protection["verified"]:
            return self._failsafe(pos_id, event_id, symbol, f"protection-unverified:{protection.get('reason')}")
        return {
            "status": "completed",
            "order_id": order_id,
            "requested_qty": requested_qty,
            "filled_qty": filled,
            "partial_fill": filled < requested_qty,
            "avg_fill_price": avg,
            "stop_price": stop,
            "target_price": target,
            "actual_risk": round((avg - stop) * filled, 2),
            "protection": protection,
        }

    # ------------------------------------------------------------------ protection / fail-safe
    def _protection_state(self, symbol: str) -> tuple[float, float, list[dict]]:
        orders = self.broker.list_orders_strict(symbol, "open")
        sells = [
            o for o in orders
            if str(o.get("side", "")).lower() == "sell" and str(o.get("status", "")).lower() in ACTIVE_ORDER_STATUSES
        ]
        stop_qty = sum(_f(o.get("qty")) for o in sells if str(o.get("type", "")).lower() in {"stop", "stop_limit"})
        target_qty = sum(_f(o.get("qty")) for o in sells if str(o.get("type", "")).lower() == "limit")
        return stop_qty, target_qty, sells

    def _cancel_active_sells(self, symbol: str) -> bool:
        """Cancel every active sell order and confirm none remain."""
        for _ in range(max(1, self._attempts(5.0))):
            _, _, sells = self._protection_state(symbol)
            if not sells:
                return True
            for order in sells:
                self.broker.cancel_order_strict(order["id"])
            self.sleep(self.poll_interval)
        return not self._protection_state(symbol)[2]

    def _ensure_protection(self, pos_id, event_id, symbol, stop, target) -> dict[str, Any]:
        """Verify protective stop + target cover the actual position; rebuild as a single OCO if they do not."""
        try:
            position = self.broker.get_position_strict(symbol)
            if position is None:
                self._settle_closed_from_history(pos_id, symbol, "closed-before-protection-check")
                return {"verified": True, "position_closed": True}
            qty = int(_f(position.get("qty")))
            if qty <= 0:
                return {"verified": False, "reason": "non-long-position"}
            stop_qty, target_qty, _ = self._protection_state(symbol)
            if stop_qty >= qty and target_qty >= qty:
                return {"verified": True, "rebuilt": False, "covered_qty": qty}

            self.store.audit(event_id, "protection_rebuild", {"position_qty": qty, "stop_qty": stop_qty, "target_qty": target_qty})
            if not self._cancel_active_sells(symbol):
                return {"verified": False, "reason": "could-not-clear-existing-orders"}
            position = self.broker.get_position_strict(symbol)  # a leg may have filled during cancellation
            if position is None:
                self._settle_closed_from_history(pos_id, symbol, "closed-during-protection-rebuild")
                return {"verified": True, "position_closed": True}
            qty = int(_f(position.get("qty")))
            cid = f"qqq-oco-{event_id}-{int(self.now())}"[:128]
            try:
                self.broker.submit_oco_sell(symbol, qty, stop, target, cid)
            except BrokerRejected as exc:
                return {"verified": False, "reason": f"oco-rejected:{exc}"}
            except BrokerAmbiguous:
                if self.broker.get_order_by_client_id(cid) is None:
                    return {"verified": False, "reason": "oco-submission-unconfirmed"}
            for _ in range(max(1, self._attempts(5.0))):
                stop_qty, target_qty, _ = self._protection_state(symbol)
                if stop_qty >= qty and target_qty >= qty:
                    return {"verified": True, "rebuilt": True, "covered_qty": qty}
                self.sleep(self.poll_interval)
            return {"verified": False, "reason": "protection-not-visible-after-rebuild"}
        except BrokerError as exc:
            return {"verified": False, "reason": f"broker-error:{type(exc).__name__}"}

    def _failsafe(self, pos_id, event_id, symbol, reason: str) -> dict[str, Any]:
        """Flatten the position, lock out new entries, and alert the operator."""
        self.store.set_lockout(f"failsafe: {reason}")
        self._log("CRITICAL", "FAIL-SAFE: flattening position; operator action required", event_id=event_id, reason=reason)
        self.store.audit(event_id, "failsafe", {"reason": reason})
        flat = False
        exit_order = None
        try:
            self._cancel_active_sells(symbol)
            exit_order = self.broker.close_position_strict(symbol)
            if exit_order and exit_order.get("id"):
                exit_order = self._await_terminal(exit_order["id"], exit_order)
            flat = self.broker.get_position_strict(symbol) is None
        except BrokerError as exc:
            self._log("CRITICAL", "FAIL-SAFE close failed; position may be unprotected", event_id=event_id, error=str(exc))
        if flat:
            self._record_exit(pos_id, exit_order, f"failsafe:{reason}")
        else:
            self.store.update_position(pos_id, status="ambiguous")
        return {"status": "failsafe", "reason": reason, "position_flattened": flat}

    # ------------------------------------------------------------------ exit
    def _handle_exit(self, s: ExitSignal) -> dict[str, Any]:
        self.store.audit(s.event_id, "exit_signal", s.model_dump())
        stale = self._stale_reason(s.bar_time)
        if stale:
            return self._reject(stale)

        position = self.broker.get_position_strict(s.symbol)
        row = self.store.get_active_position(s.symbol, STRATEGY_ID)
        broker_qty = _f(position.get("qty")) if position else 0.0

        if broker_qty <= 0:
            if row and row["status"] == "open":
                self._settle_closed_from_history(row["id"], s.symbol, "closed-before-exit-signal")
            reason = "short-position-not-eligible" if broker_qty < 0 else "no-open-position"
            return {"status": "ignored", "reason": reason}
        if row is None or row["status"] != "open":
            return {"status": "ignored", "reason": "position-not-attributable-to-strategy"}

        if not self._cancel_active_sells(s.symbol):
            self._ensure_protection(row["id"], s.event_id, s.symbol, row["stop_price"], row["target_price"])
            return {"status": "failed", "reason": "could-not-cancel-protective-orders"}

        # A protective order may have executed while we were cancelling.
        position = self.broker.get_position_strict(s.symbol)
        if position is None or _f(position.get("qty")) <= 0:
            self._settle_closed_from_history(row["id"], s.symbol, "closed-by-protective-order")
            return {"status": "completed", "reason": "already-closed-by-protective-order"}
        qty = int(min(_f(position.get("qty")), _f(row["qty"])))
        if qty <= 0:
            return {"status": "ignored", "reason": "no-eligible-quantity"}

        cid = f"qqq-exit-{s.event_id}"[:128]
        try:
            order = self.broker.submit_market_sell(s.symbol, qty, cid)
        except BrokerRejected as exc:
            self._ensure_protection(row["id"], s.event_id, s.symbol, row["stop_price"], row["target_price"])
            return {"status": "rejected", "reason": "broker-rejected-exit", "detail": str(exc)}
        except BrokerAmbiguous:
            order = self.broker.get_order_by_client_id(cid)
            if order is None:
                self._ensure_protection(row["id"], s.event_id, s.symbol, row["stop_price"], row["target_price"])
                return {"status": "failed", "reason": "exit-submission-failed-order-not-placed"}
        self.store.update_position(row["id"], exit_order_id=order.get("id"))
        self.store.audit(s.event_id, "exit_submitted", {"order_id": order.get("id"), "qty": qty})

        order = self._await_terminal(order.get("id"), order)
        if str(order.get("status", "")).lower() not in TERMINAL_ORDER_STATUSES:
            self.broker.cancel_order_strict(order.get("id"))
            order = self._await_terminal(order.get("id"), order)
            if str(order.get("status", "")).lower() not in TERMINAL_ORDER_STATUSES:
                self.store.update_position(row["id"], status="ambiguous")
                self.store.set_lockout("exit-order-state-unverifiable")
                return {"status": "ambiguous", "reason": "exit-order-state-unverifiable", "order_id": order.get("id")}

        filled = int(_f(order.get("filled_qty")))
        if filled <= 0:
            protection = self._ensure_protection(row["id"], s.event_id, s.symbol, row["stop_price"], row["target_price"])
            return {"status": "failed", "reason": f"exit-not-filled:{order.get('status')}", "protection": protection}

        self._record_exit(row["id"], order, "exit-signal")
        remaining = int(_f(row["qty"])) - filled
        result: dict[str, Any] = {
            "status": "completed",
            "order_id": order.get("id"),
            "filled_qty": filled,
            "avg_fill_price": _f(order.get("filled_avg_price")),
        }
        if remaining > 0:
            self.store.update_position(row["id"], status="open", qty=remaining)
            result["partial_fill"] = True
            result["protection"] = self._ensure_protection(row["id"], s.event_id, s.symbol, row["stop_price"], row["target_price"])
        return result

    def _record_exit(self, pos_id: int, order: dict[str, Any] | None, reason: str) -> None:
        row = self.store.get_position(pos_id) or {}
        filled = int(_f((order or {}).get("filled_qty"))) or int(_f(row.get("qty")))
        price = _f((order or {}).get("filled_avg_price")) or None
        entry = _f(row.get("entry_price"))
        pl = round((price - entry) * filled, 2) if price and entry else None
        if pl is not None and row.get("realized_pl") is not None:
            pl += row["realized_pl"]
        self.store.update_position(
            pos_id, status="closed", exit_order_id=(order or {}).get("id"), exit_price=price,
            exit_qty=filled, exit_reason=reason, realized_pl=pl,
        )
        self.store.audit(None, "position_closed", {"position_id": pos_id, "reason": reason, "exit_price": price, "realized_pl": pl})

    def _settle_closed_from_history(self, pos_id: int, symbol: str, reason: str) -> None:
        """Mark a row closed using the latest filled broker sell order since the position was opened."""
        row = self.store.get_position(pos_id)
        if not row or row["status"] == "closed":
            return
        exit_order = None
        try:
            for order in self.broker.list_orders_strict(symbol, "closed", after=row["created_at"]):
                if str(order.get("side", "")).lower() == "sell" and str(order.get("status", "")).lower() == "filled":
                    exit_order = order
                    break
        except BrokerError:
            pass
        self._record_exit(pos_id, exit_order, reason)

    # ------------------------------------------------------------------ restart recovery
    def reconcile(self) -> dict[str, Any]:
        """Align local strategy state with actual Alpaca positions and orders (run at startup)."""
        report: dict[str, Any] = {"checked": 0, "actions": []}
        try:
            for symbol in ("QQQ",):
                row = self.store.get_active_position(symbol, STRATEGY_ID)
                position = self.broker.get_position_strict(symbol)
                broker_qty = _f(position.get("qty")) if position else 0.0
                if row is None:
                    if broker_qty != 0:
                        report["actions"].append("orphan-broker-position-not-managed")
                        self._log("CRITICAL", "Broker holds a QQQ position with no strategy record", qty=broker_qty)
                    continue
                report["checked"] += 1
                event_id = row["event_id"]
                if row["status"] in {"reserved", "ambiguous"} and not row["entry_order_id"] and not row["order_submitted"]:
                    self.store.update_position(row["id"], status="failed")
                    report["actions"].append("released-unsubmitted-reservation")
                elif row["status"] in {"reserved", "ambiguous"}:
                    order = self.broker.get_order_by_client_id(row["entry_client_order_id"])
                    if order is None:
                        self.store.update_position(row["id"], status="failed")
                        report["actions"].append("entry-order-not-found-released")
                    else:
                        result = self._complete_entry(
                            row["id"], event_id, symbol, order, row["stop_price"], row["target_price"], int(_f(row["qty"]))
                        )
                        report["actions"].append(f"entry-recovered:{result['status']}")
                elif broker_qty <= 0:
                    self._settle_closed_from_history(row["id"], symbol, "closed-while-offline")
                    report["actions"].append("position-closed-while-offline")
                else:
                    protection = self._ensure_protection(row["id"], event_id, symbol, row["stop_price"], row["target_price"])
                    if not protection["verified"]:
                        self._failsafe(row["id"], event_id, symbol, f"recovery-protection-unverified:{protection.get('reason')}")
                    report["actions"].append(f"protection-checked:{protection['verified']}")
        except BrokerAmbiguous as exc:
            self.store.set_lockout(f"recovery-broker-unverifiable: {exc}")
            self._log("CRITICAL", "Recovery could not verify broker state; trading locked out", error=str(exc))
            report["actions"].append("lockout-broker-unverifiable")
        return report

    # ------------------------------------------------------------------ helpers
    def _attempts(self, timeout: float) -> int:
        return int(timeout / self.poll_interval) if self.poll_interval > 0 else int(timeout)

    def _await_terminal(self, order_id: str, order: dict[str, Any]) -> dict[str, Any]:
        for _ in range(max(1, self._attempts(self.cfg.qqq_fill_timeout_seconds))):
            if str(order.get("status", "")).lower() in TERMINAL_ORDER_STATUSES:
                return order
            self.sleep(self.poll_interval)
            try:
                order = self.broker.get_order_strict(order_id)
            except BrokerAmbiguous:
                continue
        return order

    def _stale_reason(self, bar_time_ms: int) -> str | None:
        age_ms = self.now() * 1000.0 - bar_time_ms
        if age_ms > self.cfg.qqq_max_alert_age_seconds * 1000:
            return "stale-alert"
        if age_ms < -60_000:
            return "alert-timestamp-in-future"
        return None

    def _minutes_to_close(self, clock: dict[str, Any]) -> float | None:
        try:
            # Alpaca timestamps may carry nanosecond fractions that fromisoformat rejects.
            now_ts = datetime.fromisoformat(re.sub(r"\.\d+", "", str(clock["timestamp"])))
            close_ts = datetime.fromisoformat(re.sub(r"\.\d+", "", str(clock["next_close"])))
            return (close_ts - now_ts).total_seconds() / 60.0
        except (KeyError, ValueError, TypeError):
            return None

    def _expected_entry_price(self, symbol: str) -> tuple[float, str] | None:
        try:
            quote = self.broker.get_latest_quote(symbol)
            ask, bid = _f(quote.get("ap")), _f(quote.get("bp"))
            if ask > 0 and bid > 0 and ask >= bid:
                return ask, "alpaca-ask"
        except BrokerError:
            pass
        try:
            snapshot = self.market_data.get_stock_snapshot(symbol)
            price = _f((snapshot or {}).get("latest_price"))
            if price > 0:
                return price, "market-data"
        except Exception:
            pass
        return None

    def _market_context(self, symbol: str) -> dict[str, Any] | str:
        try:
            snapshot = self.market_data.get_stock_snapshot(symbol)
            return {k: snapshot.get(k) for k in ("latest_price", "change_pct", "source", "quote")}
        except Exception:
            return "unavailable"

    def _reject(self, reason: str, **extra: Any) -> dict[str, Any]:
        return {"status": "rejected", "reason": reason, **extra}

    def _log(self, level: str, message: str, **details: Any) -> None:
        logger.log(getattr(logging, level, logging.INFO), "%s %s", message, details)
        if self.log_hook:
            try:
                self.log_hook(level, message, details)
            except Exception:
                pass
