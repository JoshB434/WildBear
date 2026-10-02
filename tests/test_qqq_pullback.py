import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from app.api.v1.routes import integration as integration_module
from app.config import settings
from app.schemas import RiskSettings
from app.services.alpaca_client import BrokerAmbiguous, BrokerRejected
from app.services.qqq_pullback import QQQPullbackWorkflow, compute_quantity
from app.services.qqq_signals import UnsupportedEvent, parse_signal
from app.services.qqq_state import QQQStateStore

NOW = 1_790_964_600.0  # fixed "current" epoch seconds for deterministic staleness checks


class FakeBroker:
    """Minimal in-memory Alpaca stand-in; never touches the network."""

    ACTIVE = {"new", "accepted", "held", "partially_filled", "pending_new"}

    def __init__(self):
        self.lock = threading.Lock()
        self.account = {
            "status": "ACTIVE", "equity": "100000", "last_equity": "100000",
            "buying_power": "400000", "long_market_value": "0",
        }
        self.clock = {"is_open": True, "timestamp": "2026-10-02T11:00:00.123456789-04:00", "next_close": "2026-10-02T16:00:00-04:00"}
        self.quote = {"ap": 600.05, "bp": 600.0}
        self.fill_price = 600.05
        self.exit_price = 599.25
        self.position_qty = 0
        self.orders: dict[str, dict] = {}
        self.by_client: dict[str, str] = {}
        self.mode = "fill"
        self.oco_reject = False
        self.lookup_fails = False
        self.calls: list[tuple] = []
        self._seq = 0

    def _id(self):
        self._seq += 1
        return f"ord-{self._seq}"

    def _add(self, **fields):
        order = {"id": self._id(), "status": "new", "filled_qty": "0", **fields}
        self.orders[order["id"]] = order
        if order.get("client_order_id"):
            self.by_client[order["client_order_id"]] = order["id"]
        return order

    def _legs(self, qty, stop, target):
        self._add(side="sell", type="stop", qty=str(qty), stop_price=str(stop))
        self._add(side="sell", type="limit", qty=str(qty), limit_price=str(target))

    # --- reads
    def get_account_strict(self):
        return dict(self.account)

    def get_clock(self):
        return dict(self.clock)

    def get_position_strict(self, symbol):
        return {"qty": str(self.position_qty)} if self.position_qty else None

    def list_orders_strict(self, symbol, status="open", after=None):
        if status == "open":
            return [dict(o) for o in self.orders.values() if o["status"] in self.ACTIVE]
        return [dict(o) for o in self.orders.values() if o["status"] not in self.ACTIVE][::-1]

    def get_order_strict(self, order_id):
        return dict(self.orders[order_id])

    def get_order_by_client_id(self, client_order_id):
        if self.lookup_fails:
            raise BrokerAmbiguous("lookup timeout")
        oid = self.by_client.get(client_order_id)
        return dict(self.orders[oid]) if oid else None

    def get_latest_quote(self, symbol):
        return dict(self.quote)

    # --- writes
    def submit_bracket_buy(self, symbol, qty, stop, target, client_order_id):
        with self.lock:
            self.calls.append(("bracket_buy", qty, stop, target))
            if self.mode == "reject":
                raise BrokerRejected(403, "insufficient buying power")
            if self.mode == "timeout_unplaced":
                raise BrokerAmbiguous("timeout")
            partial = self.mode == "partial"
            filled = qty // 2 if partial else qty
            order = self._add(
                side="buy", type="market", qty=str(qty), client_order_id=client_order_id,
                status="partially_filled" if partial else "filled",
                filled_qty=str(filled), filled_avg_price=str(self.fill_price),
            )
            self.position_qty += filled
            if self.mode in {"fill", "timeout_placed", "timeout_unverifiable"}:
                self._legs(filled, stop, target)
            if self.mode == "timeout_placed":
                raise BrokerAmbiguous("timeout")
            if self.mode == "timeout_unverifiable":
                self.lookup_fails = True
                raise BrokerAmbiguous("timeout")
            return dict(order)

    def submit_oco_sell(self, symbol, qty, stop, target, client_order_id):
        self.calls.append(("oco", qty, stop, target))
        if self.oco_reject:
            raise BrokerRejected(422, "cannot place oco")
        self._legs(qty, stop, target)

    def submit_market_sell(self, symbol, qty, client_order_id):
        self.calls.append(("market_sell", qty))
        held = sum(int(o["qty"]) for o in self.orders.values() if o["status"] in self.ACTIVE and o["side"] == "sell")
        if qty + held > self.position_qty:
            raise BrokerRejected(403, "insufficient qty available")
        self.position_qty -= qty
        return dict(self._add(
            side="sell", type="market", qty=str(qty), client_order_id=client_order_id, status="filled",
            filled_qty=str(qty), filled_avg_price=str(self.exit_price),
        ))

    def cancel_order_strict(self, order_id):
        order = self.orders.get(order_id)
        if order and order["status"] in self.ACTIVE:
            order["status"] = "canceled"
            return True
        return False

    def close_position_strict(self, symbol):
        self.calls.append(("close_position",))
        qty, self.position_qty = self.position_qty, 0
        return dict(self._add(
            side="sell", type="market", qty=str(qty), status="filled", filled_qty=str(qty),
            filled_avg_price=str(self.exit_price),
        ))


class FakeAI:
    def __init__(self, approve=True, confidence=0.9):
        self.approve, self.confidence, self.calls = approve, confidence, 0

    def evaluate_entry_candidate(self, context):
        self.calls += 1
        return {"approve": self.approve, "confidence": self.confidence, "rationale": "test", "model": "fake"}


class FakeMarketData:
    def get_stock_snapshot(self, symbol, **kwargs):
        return {"latest_price": 600.0, "change_pct": 0.1, "source": "fake", "quote": {}}


def make_cfg(**overrides):
    base = dict(
        qqq_trading_enabled=True, qqq_risk_pct=0.005, qqq_max_position_value=1_000_000.0,
        qqq_max_trades_per_day=3, qqq_daily_loss_limit_pct=1.0, qqq_max_alert_age_seconds=600,
        qqq_max_entry_slippage_pct=0.3, qqq_no_entry_minutes_before_close=15, qqq_min_ai_confidence=0.72,
        qqq_fill_timeout_seconds=3.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def entry_payload(event_id="QQQ_ENTRY_1", bar_time=None, **overrides):
    payload = {
        "event": "ENTRY_CANDIDATE", "event_id": event_id, "symbol": "QQQ", "side": "buy", "timeframe": "5",
        "bar_time": int((NOW - 300) * 1000) if bar_time is None else bar_time, "price": 600.0, "stop_price": 598.5,
        "target_price": 602.25, "atr": 1.0, "strategy": "qqq_pullback_v1",
    }
    payload.update(overrides)
    return payload


def exit_payload(event_id="QQQ_EXIT_1", **overrides):
    payload = {
        "event": "EXIT_SIGNAL", "event_id": event_id, "symbol": "QQQ", "side": "close_long", "timeframe": "5",
        "bar_time": int((NOW - 100) * 1000), "price": 599.25, "strategy": "qqq_pullback_v1",
    }
    payload.update(overrides)
    return payload


def build(tmp_path, ai=None, cfg=None, risk_settings=None, broker=None):
    broker = broker or FakeBroker()
    store = QQQStateStore(tmp_path / "state.db")
    workflow = QQQPullbackWorkflow(
        store=store, broker=broker, ai=ai or FakeAI(), market_data=FakeMarketData(),
        cfg=cfg or make_cfg(), risk_settings_provider=lambda: risk_settings,
        sleep=lambda _s: None, now=lambda: NOW, poll_interval=0,
    )
    return workflow, broker, store


def run(workflow, payload):
    return workflow.handle(parse_signal(payload))


# ---------------------------------------------------------------- schema
def test_valid_entry_and_exit_payloads_parse():
    assert parse_signal(entry_payload()).event == "ENTRY_CANDIDATE"
    assert parse_signal(exit_payload()).side == "close_long"


@pytest.mark.parametrize(
    "payload",
    [
        entry_payload(symbol="SPY"),
        entry_payload(strategy="supertrend"),
        entry_payload(timeframe="15"),
        entry_payload(side="sell"),
        entry_payload(price=-1),
        entry_payload(stop_price=601.0),
        entry_payload(target_price=599.0),
        entry_payload(atr=0),
        entry_payload(event_id="bad id!"),
        entry_payload(bar_time=0),
        exit_payload(side="sell"),
        exit_payload(price="abc"),
        {k: v for k, v in entry_payload().items() if k != "stop_price"},
        {**entry_payload(), "unexpected": 1},
    ],
)
def test_invalid_payloads_rejected(payload):
    with pytest.raises(ValidationError):
        parse_signal(payload)


def test_unsupported_event_rejected():
    with pytest.raises(UnsupportedEvent):
        parse_signal({"event": "SUPERTREND_BUY"})
    with pytest.raises(UnsupportedEvent):
        parse_signal(["not", "an", "object"])


# ---------------------------------------------------------------- sizing
def test_position_sizing_respects_risk_budget():
    sizing = compute_quantity(100_000, 0.005, 600.0, 598.5, 1_000_000, 400_000)
    assert sizing["quantity"] == 333 and sizing["limit"] == "risk-budget"
    assert sizing["quantity"] * 1.5 <= 500
    assert compute_quantity(100_000, 0.005, 600.0, 598.5, 12_000, 400_000)["quantity"] == 20
    assert compute_quantity(100_000, 0.005, 600.0, 598.5, 1_000_000, 1_000)["quantity"] == 1
    assert compute_quantity(100_000, 0.005, 600.0, 600.0, 1_000_000, 400_000)["quantity"] == 0


def test_workflow_uses_sized_quantity_and_actual_fills(tmp_path):
    workflow, broker, store = build(tmp_path)
    result = run(workflow, entry_payload())
    assert result["status"] == "completed"
    assert result["filled_qty"] == 322  # risk budget 500 / (600.05 ask - 598.5 stop)
    assert broker.calls[0] == ("bracket_buy", 322, 598.5, 602.25)
    assert result["actual_risk"] == round((600.05 - 598.5) * 322, 2)
    row = store.get_active_position("QQQ", "qqq_pullback_v1")
    assert row["status"] == "open" and row["entry_price"] == 600.05 and row["qty"] == 322


# ---------------------------------------------------------------- entry flow
def test_duplicate_event_id_is_ignored(tmp_path):
    workflow, broker, _ = build(tmp_path)
    assert run(workflow, entry_payload())["status"] == "completed"
    assert run(workflow, entry_payload())["status"] == "duplicate"
    assert len([c for c in broker.calls if c[0] == "bracket_buy"]) == 1


def test_stale_alert_rejected(tmp_path):
    workflow, broker, _ = build(tmp_path)
    result = run(workflow, entry_payload(bar_time=int((NOW - 3600) * 1000)))
    assert result["reason"] == "stale-alert" and not broker.calls


def test_ai_rejection_blocks_order(tmp_path):
    ai = FakeAI(approve=False)
    workflow, broker, store = build(tmp_path, ai=ai)
    result = run(workflow, entry_payload())
    assert result["reason"] == "ai-rejected" and ai.calls == 1 and not broker.calls
    assert store.get_active_position("QQQ", "qqq_pullback_v1") is None


def test_ai_low_confidence_blocks_order(tmp_path):
    workflow, broker, _ = build(tmp_path, ai=FakeAI(approve=True, confidence=0.5))
    assert run(workflow, entry_payload())["reason"] == "ai-rejected" and not broker.calls


def test_ai_approval_places_order_with_protection(tmp_path):
    workflow, broker, _ = build(tmp_path)
    result = run(workflow, entry_payload())
    assert result["status"] == "completed" and result["protection"]["verified"]


def test_trading_disabled_by_default_flag(tmp_path):
    workflow, broker, _ = build(tmp_path, cfg=make_cfg(qqq_trading_enabled=False))
    assert run(workflow, entry_payload())["reason"] == "trading-disabled" and not broker.calls


def test_existing_position_prevents_entry(tmp_path):
    workflow, broker, _ = build(tmp_path)
    broker.position_qty = 10
    assert run(workflow, entry_payload())["reason"] == "existing-broker-position"
    assert not broker.calls


def test_second_entry_blocked_while_strategy_position_open(tmp_path):
    workflow, broker, _ = build(tmp_path)
    run(workflow, entry_payload("E1"))
    assert run(workflow, entry_payload("E2"))["reason"] == "existing-broker-position"


def test_open_order_prevents_entry(tmp_path):
    workflow, broker, _ = build(tmp_path)
    broker._add(side="buy", type="limit", qty="5", status="new")
    assert run(workflow, entry_payload())["reason"] == "open-order-exists"


def test_daily_loss_limit_blocks_entry(tmp_path):
    workflow, broker, _ = build(tmp_path)
    broker.account["equity"] = "98000"
    result = run(workflow, entry_payload())
    assert result["reason"] == "daily-loss-limit" and not broker.calls


def test_risk_settings_daily_loss_limit_is_respected(tmp_path):
    risk = RiskSettings(max_position_size=500, daily_loss_limit=0.1, stop_loss_pct=0.05, take_profit_pct=0.1, cooldown_minutes=0)
    workflow, broker, _ = build(tmp_path, risk_settings=risk)
    broker.account["equity"] = "99800"
    assert run(workflow, entry_payload())["reason"] == "daily-loss-limit"


def test_max_trades_per_day(tmp_path):
    workflow, broker, _ = build(tmp_path, cfg=make_cfg(qqq_max_trades_per_day=1))
    run(workflow, entry_payload("E1"))
    run(workflow, exit_payload("X1"))
    assert run(workflow, entry_payload("E2"))["reason"] == "max-trades-per-day-reached"


def test_price_drift_rejects_entry(tmp_path):
    workflow, broker, _ = build(tmp_path)
    broker.quote = {"ap": 610.0, "bp": 609.9}
    assert run(workflow, entry_payload())["reason"] == "entry-price-drifted"


def test_broker_rejection_of_entry(tmp_path):
    workflow, broker, store = build(tmp_path)
    broker.mode = "reject"
    result = run(workflow, entry_payload())
    assert result["reason"] == "broker-rejected-entry"
    assert store.get_active_position("QQQ", "qqq_pullback_v1") is None
    assert not store.get_lockout()["locked"]


def test_partial_fill_protects_actual_filled_quantity(tmp_path):
    workflow, broker, store = build(tmp_path)
    broker.mode = "partial"
    result = run(workflow, entry_payload())
    assert result["status"] == "completed" and result["partial_fill"] is True
    assert result["filled_qty"] == 161
    assert ("oco", 161, 598.5, 602.25) in broker.calls
    assert store.get_active_position("QQQ", "qqq_pullback_v1")["qty"] == 161


def test_protection_failure_triggers_failsafe_and_lockout(tmp_path):
    workflow, broker, store = build(tmp_path)
    broker.mode = "unprotected"
    broker.oco_reject = True
    result = run(workflow, entry_payload())
    assert result["status"] == "failsafe" and result["position_flattened"] is True
    assert broker.position_qty == 0
    assert store.get_lockout()["locked"]
    assert run(workflow, entry_payload("E2"))["reason"] == "lockout-active"


def test_timeout_with_order_placed_reconciles_and_continues(tmp_path):
    workflow, broker, _ = build(tmp_path)
    broker.mode = "timeout_placed"
    result = run(workflow, entry_payload())
    assert result["status"] == "completed"
    assert len([c for c in broker.calls if c[0] == "bracket_buy"]) == 1  # no blind retry


def test_timeout_with_order_not_placed_is_not_retried(tmp_path):
    workflow, broker, store = build(tmp_path)
    broker.mode = "timeout_unplaced"
    result = run(workflow, entry_payload())
    assert result["reason"] == "submission-failed-order-not-placed"
    assert len(broker.calls) == 1
    assert store.get_active_position("QQQ", "qqq_pullback_v1") is None


def test_timeout_with_unverifiable_state_locks_out(tmp_path):
    workflow, broker, store = build(tmp_path)
    broker.mode = "timeout_unverifiable"
    result = run(workflow, entry_payload())
    assert result["status"] == "failed"
    assert store.get_lockout()["locked"]
    assert store.get_active_position("QQQ", "qqq_pullback_v1")["status"] == "ambiguous"


def test_concurrent_duplicate_and_distinct_events_place_one_order(tmp_path):
    workflow, broker, _ = build(tmp_path)
    results: list[dict] = []

    def fire(event_id):
        results.append(run(workflow, entry_payload(event_id)))

    threads = [threading.Thread(target=fire, args=("SAME",)) for _ in range(4)]
    threads += [threading.Thread(target=fire, args=(f"OTHER_{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([c for c in broker.calls if c[0] == "bracket_buy"]) == 1
    assert sum(1 for r in results if r["status"] == "completed") == 1
    assert sum(1 for r in results if r["status"] == "duplicate") == 3


# ---------------------------------------------------------------- exit flow
def test_exit_without_position_is_ignored(tmp_path):
    workflow, broker, _ = build(tmp_path)
    result = run(workflow, exit_payload())
    assert result == {"status": "ignored", "reason": "no-open-position", "event_id": "QQQ_EXIT_1"}
    assert not broker.calls


def test_exit_not_attributable_to_strategy_is_ignored(tmp_path):
    workflow, broker, _ = build(tmp_path)
    broker.position_qty = 50
    assert run(workflow, exit_payload())["reason"] == "position-not-attributable-to-strategy"
    assert not broker.calls


def test_exit_closes_long_without_going_short(tmp_path):
    workflow, broker, store = build(tmp_path)
    run(workflow, entry_payload())
    result = run(workflow, exit_payload())
    assert result["status"] == "completed" and result["filled_qty"] == 322
    assert broker.position_qty == 0
    assert store.get_active_position("QQQ", "qqq_pullback_v1") is None
    assert not any(o["status"] in broker.ACTIVE for o in broker.orders.values())
    closed = store.get_position(1)
    assert closed["status"] == "closed" and closed["realized_pl"] == round((599.25 - 600.05) * 322, 2)
    # A repeated exit signal must not sell again.
    assert run(workflow, exit_payload("X2"))["reason"] == "no-open-position"
    assert broker.position_qty == 0


def test_stale_exit_rejected(tmp_path):
    workflow, broker, _ = build(tmp_path)
    run(workflow, entry_payload())
    result = run(workflow, exit_payload(bar_time=int((NOW - 7200) * 1000)))
    assert result["reason"] == "stale-alert" and broker.position_qty == 322


def test_exit_still_allowed_during_lockout(tmp_path):
    workflow, broker, store = build(tmp_path)
    run(workflow, entry_payload())
    store.set_lockout("test")
    assert run(workflow, exit_payload())["status"] == "completed"


# ---------------------------------------------------------------- recovery
def test_restart_recovery_rebuilds_protection_for_filled_entry(tmp_path):
    workflow, broker, store = build(tmp_path)
    store.claim_event("E_RECOVER", "ENTRY_CANDIDATE")
    pos_id, _ = store.reserve_position("QQQ", "qqq_pullback_v1", "E_RECOVER", "qqq-entry-E_RECOVER", "2026-10-02", 3)
    store.update_position(pos_id, qty=100, stop_price=598.5, target_price=602.25, order_submitted=1)
    broker._add(side="buy", type="market", qty="100", client_order_id="qqq-entry-E_RECOVER",
                status="filled", filled_qty="100", filled_avg_price="600.05")
    broker.position_qty = 100  # filled while the app was down, no protective orders

    fresh, _, _ = build(tmp_path, broker=broker)
    report = fresh.reconcile()
    assert report["actions"] == ["entry-recovered:completed"]
    assert ("oco", 100, 598.5, 602.25) in broker.calls
    assert store.get_active_position("QQQ", "qqq_pullback_v1")["status"] == "open"


def test_restart_recovery_closes_row_when_position_is_gone(tmp_path):
    workflow, broker, store = build(tmp_path)
    run(workflow, entry_payload())
    broker.position_qty = 0
    for order in broker.orders.values():
        if order["side"] == "sell" and order["status"] in broker.ACTIVE:
            order["status"] = "canceled"
    broker._add(side="sell", type="stop", qty="333", status="filled", filled_qty="333", filled_avg_price="598.5")
    report = workflow.reconcile()
    assert report["actions"] == ["position-closed-while-offline"]
    assert store.get_active_position("QQQ", "qqq_pullback_v1") is None


def test_restart_recovery_flags_orphan_broker_position(tmp_path):
    workflow, broker, _ = build(tmp_path)
    broker.position_qty = 10
    assert workflow.reconcile()["actions"] == ["orphan-broker-position-not-managed"]


# ---------------------------------------------------------------- HTTP layer
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "legacy_webhook_enabled", False)
    monkeypatch.setattr(settings, "tradingview_webhook_secret", None)
    workflow, broker, store = build(tmp_path)
    monkeypatch.setattr(integration_module, "_qqq_workflow", workflow)
    return TestClient(app), workflow, broker


URL = "/api/v1/integration/tradingview/webhook"


def test_api_valid_entry_and_exit(api, monkeypatch):
    client, workflow, broker = api
    monkeypatch.setattr(workflow, "now", lambda: time.time())
    fresh = int(time.time() * 1000) - 60_000
    entry = client.post(URL, json=entry_payload("API_E1", bar_time=fresh))
    assert entry.status_code == 200 and entry.json()["status"] == "completed"
    exit_ = client.post(URL, json=exit_payload("API_X1", bar_time=fresh + 1000))
    assert exit_.status_code == 200 and exit_.json()["status"] == "completed"
    assert broker.position_qty == 0


def test_api_malformed_json(api):
    client, _, _ = api
    response = client.post(URL, content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_api_supertrend_plain_text_no_longer_accepted(api):
    client, _, broker = api
    response = client.post(URL, content="SuperTrend Buy!", headers={"content-type": "text/plain"})
    assert response.status_code == 400 and not broker.calls


@pytest.mark.parametrize(
    "payload",
    [
        {k: v for k, v in entry_payload().items() if k != "price"},
        entry_payload(symbol="SPY"),
        entry_payload(strategy="other"),
        entry_payload(stop_price=700.0),
    ],
)
def test_api_validation_errors_return_422(api, payload):
    client, _, broker = api
    assert client.post(URL, json=payload).status_code == 422
    assert not broker.calls


def test_api_unsupported_event_returns_400(api):
    client, _, _ = api
    assert client.post(URL, json={"event": "SUPERTREND_BUY"}).status_code == 400


def test_api_webhook_secret_via_header_or_passphrase(api, monkeypatch):
    client, _, broker = api
    monkeypatch.setattr(settings, "tradingview_webhook_secret", "s3cret")
    assert client.post(URL, json=entry_payload()).status_code == 401
    assert client.post(URL, json=entry_payload(passphrase="wrong")).status_code == 401
    ok = client.post(URL, json=entry_payload("API_E2", bar_time=int(time.time() * 1000)), headers={"x-webhook-secret": "s3cret"})
    assert ok.status_code == 200
    assert "s3cret" not in ok.text
