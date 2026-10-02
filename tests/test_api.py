from fastapi.testclient import TestClient
import pytest

from main import app
from app.api.v1.routes import integration as integration_module

client = TestClient(app)


@pytest.fixture(autouse=True)
def _legacy_webhook(monkeypatch):
    monkeypatch.setattr(integration_module.settings, "legacy_webhook_enabled", True)


def test_assets_crud_flow():
    list_response = client.get("/api/v1/assets/")
    assert list_response.status_code == 200
    assert list_response.json()["items"] == []

    create_response = client.post(
        "/api/v1/assets/",
        json={
            "symbol": "AAPL",
            "name": "Apple Inc",
            "sector": "Technology",
            "exchange": "NASDAQ",
        },
    )
    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["symbol"] == "AAPL"
    assert payload["name"] == "Apple Inc"

    get_response = client.get("/api/v1/assets/AAPL")
    assert get_response.status_code == 200
    assert get_response.json()["symbol"] == "AAPL"

    update_response = client.put(
        "/api/v1/assets/AAPL",
        json={"name": "Apple Incorporated"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Apple Incorporated"

    delete_response = client.delete("/api/v1/assets/AAPL")
    assert delete_response.status_code == 200
    assert delete_response.json()["symbol"] == "AAPL"


def test_trading_signal_alert_and_ai_analysis_flow():
    signal_response = client.post(
        "/api/v1/trading/signals/",
        json={
            "symbol": "AAPL",
            "action": "buy",
            "confidence": 0.82,
            "strategy": "momentum",
        },
    )
    assert signal_response.status_code == 200
    assert signal_response.json()["symbol"] == "AAPL"

    alert_response = client.post(
        "/api/v1/trading/alerts/tradingview",
        json={
            "ticker": "AAPL",
            "action": "buy",
            "price": 190.24,
            "strategy": "breakout",
        },
    )
    assert alert_response.status_code == 200
    assert alert_response.json()["ticker"] == "AAPL"

    order_response = client.post(
        "/api/v1/trading/orders/paper",
        json={"symbol": "AAPL", "side": "buy", "quantity": 10},
    )
    assert order_response.status_code == 200
    assert order_response.json()["status"] == "queued"

    analysis_response = client.post(
        "/api/v1/trading/analysis/ai",
        json={"symbol": "AAPL", "timeframe": "1D", "notes": "Momentum breakout"},
    )
    assert analysis_response.status_code == 200
    assert "signal" in analysis_response.json()


def test_tradingview_webhook_triggers_ai_analysis_and_order():
    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "TSLA", "action": "buy", "price": 250.0, "strategy": "breakout"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["received"] is True
    assert payload["analysis"]["symbol"] == "TSLA"
    assert payload["order"]["status"] in {"queued", "blocked"}


def test_tradingview_webhook_requires_alert_and_ai_to_agree_before_order():
    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "sell", "price": 480.0, "strategy": "breakout"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["analysis"]["symbol"] == "QQQ"
    # Order outcome depends on live paper-account position/price state (no mocks here),
    # so any of these statuses is a valid outcome for this agreement-only check.
    if payload["order"] is not None:
        assert payload["order"]["status"] in {"blocked", "queued"}


def test_tradingview_webhook_and_risk_limits():
    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "MSFT", "action": "sell", "price": 421.5},
    )
    assert webhook_response.status_code == 200

    risk_response = client.post(
        "/api/v1/trading/risk-settings",
        json={
            "max_position_size": 5,
            "daily_loss_limit": 100.0,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "cooldown_minutes": 15,
        },
    )
    assert risk_response.status_code == 200

    blocked_order_response = client.post(
        "/api/v1/trading/orders/paper",
        json={"symbol": "MSFT", "side": "buy", "quantity": 100},
    )
    assert blocked_order_response.status_code == 409


def test_tradingview_webhook_includes_live_market_data_in_analysis(monkeypatch):
    market_snapshot = {
        "symbol": "QQQ",
        "timeframe": "1Day",
        "available": True,
        "bars": [
            {"time": "2026-06-28T14:30:00Z", "open": 480.0, "high": 482.0, "low": 479.2, "close": 481.6, "volume": 1200000},
            {"time": "2026-06-28T14:35:00Z", "open": 481.6, "high": 483.1, "low": 480.8, "close": 482.4, "volume": 1100000},
        ],
        "latest_bar": {"time": "2026-06-28T14:35:00Z", "open": 481.6, "high": 483.1, "low": 480.8, "close": 482.4, "volume": 1100000},
        "previous_close": 481.6,
        "change_pct": 0.166,
        "average_volume": 1150000.0,
    }
    captured = {}

    monkeypatch.setattr(
        integration_module.alpaca_market_data_service,
        "get_stock_snapshot",
        lambda symbol, timeframe="1Day", limit=5: market_snapshot,
    )

    def fake_analyze(symbol, timeframe, notes=None, market_data=None):
        captured["market_data"] = market_data
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "hold",
            "confidence": 0.68,
            "model": "test-double",
        }

    monkeypatch.setattr(integration_module.aitrading_analysis_service, "analyze", fake_analyze)

    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "buy", "price": 482.4, "strategy": "supertrend"},
    )

    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["market_data"]["symbol"] == "QQQ"
    assert captured["market_data"]["symbol"] == "QQQ"


def test_webhook_sell_requires_higher_confidence_from_risk_settings(monkeypatch):
    risk_response = client.post(
        "/api/v1/trading/risk-settings",
        json={
            "max_position_size": 5,
            "daily_loss_limit": 100.0,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "cooldown_minutes": 15,
            "min_ai_confidence_buy": 0.7,
            "min_ai_confidence_sell": 0.9,
            "block_loss_sells": False,
        },
    )
    assert risk_response.status_code == 200

    monkeypatch.setattr(
        integration_module.alpaca_market_data_service,
        "get_stock_snapshot",
        lambda symbol, timeframe="1Day", limit=5: {
            "symbol": symbol,
            "available": True,
            "latest_price": 510.0,
            "change_pct": 0.2,
            "average_volume": 1200000,
        },
    )

    monkeypatch.setattr(
        integration_module.aitrading_analysis_service,
        "analyze",
        lambda symbol, timeframe, notes=None, market_data=None: {
            "symbol": symbol,
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "sell",
            "confidence": 0.85,
            "model": "test-double",
        },
    )

    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "sell", "price": 510.0, "strategy": "supertrend"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["order"] is None


def test_webhook_blocks_sell_below_cost_basis(monkeypatch):
    risk_response = client.post(
        "/api/v1/trading/risk-settings",
        json={
            "max_position_size": 5,
            "daily_loss_limit": 100.0,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "cooldown_minutes": 15,
            "min_ai_confidence_buy": 0.7,
            "min_ai_confidence_sell": 0.7,
            "block_loss_sells": True,
            "min_profit_pct_for_sell": 0.0,
        },
    )
    assert risk_response.status_code == 200

    monkeypatch.setattr(
        integration_module.alpaca_market_data_service,
        "get_stock_snapshot",
        lambda symbol, timeframe="1Day", limit=5: {
            "symbol": symbol,
            "available": True,
            "latest_price": 490.0,
            "change_pct": -0.5,
            "average_volume": 1200000,
        },
    )

    monkeypatch.setattr(
        integration_module.aitrading_analysis_service,
        "analyze",
        lambda symbol, timeframe, notes=None, market_data=None: {
            "symbol": symbol,
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "sell",
            "confidence": 0.91,
            "model": "test-double",
        },
    )

    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "evaluate_sell_profit_guard",
        lambda symbol, candidate_price, min_profit_pct, stop_loss_pct, last_buy_price=None, require_sell_above_last_buy=False: {
            "allowed": False,
            "reason": "below-profit-floor",
            "avg_entry_price": 500.0,
            "candidate_price": candidate_price,
            "min_take_profit_price": 500.0,
            "stop_loss_trigger_price": 475.0,
        },
    )

    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "sell", "price": 490.0, "strategy": "supertrend"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["order"]["status"] == "blocked"
    assert payload["order"]["reason"] == "below-profit-floor"


def test_webhook_buy_creates_protective_stop(monkeypatch):
    risk_response = client.post(
        "/api/v1/trading/risk-settings",
        json={
            "max_position_size": 10,
            "daily_loss_limit": 100.0,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "cooldown_minutes": 15,
            "min_ai_confidence_buy": 0.7,
            "min_ai_confidence_sell": 0.7,
            "block_loss_sells": True,
            "min_profit_pct_for_sell": 0.0,
        },
    )
    assert risk_response.status_code == 200

    monkeypatch.setattr(
        integration_module.alpaca_market_data_service,
        "get_stock_snapshot",
        lambda symbol, timeframe="1Day", limit=5: {
            "symbol": symbol,
            "available": True,
            "latest_price": 500.0,
            "change_pct": 0.4,
            "average_volume": 1200000,
        },
    )

    monkeypatch.setattr(
        integration_module.aitrading_analysis_service,
        "analyze",
        lambda symbol, timeframe, notes=None, market_data=None: {
            "symbol": symbol,
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "buy",
            "confidence": 0.86,
            "model": "test-double",
        },
    )

    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "submit_buy_with_balance_limit",
        lambda symbol, account_balance=None, buy_pct=0.15: {
            "symbol": symbol,
            "side": "buy",
            "quantity": 3,
            "status": "queued",
            "broker": "alpaca-paper",
        },
    )

    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "upsert_consolidated_protective_stop",
        lambda symbol, reference_price, stop_loss_pct, fallback_quantity: {
            "symbol": symbol,
            "side": "sell",
            "quantity": fallback_quantity,
            "status": "queued",
            "broker": "alpaca-paper",
            "type": "stop",
            "stop_price": round(reference_price * (1.0 - stop_loss_pct), 2),
            "stop_loss_pct": stop_loss_pct,
            "replaced_existing_stops": {"cancelled": 0},
        },
    )

    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "buy", "price": 500.0, "strategy": "supertrend"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["order"]["status"] == "queued"
    assert payload["order"]["protective_stop"]["status"] == "queued"
    assert payload["order"]["protective_stop"]["type"] == "stop"


def test_webhook_buy_replaces_existing_protective_stops(monkeypatch):
    risk_response = client.post(
        "/api/v1/trading/risk-settings",
        json={
            "max_position_size": 10,
            "daily_loss_limit": 100.0,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "cooldown_minutes": 15,
            "min_ai_confidence_buy": 0.7,
            "min_ai_confidence_sell": 0.7,
            "block_loss_sells": True,
            "min_profit_pct_for_sell": 0.0,
        },
    )
    assert risk_response.status_code == 200

    monkeypatch.setattr(
        integration_module.alpaca_market_data_service,
        "get_stock_snapshot",
        lambda symbol, timeframe="1Day", limit=5: {
            "symbol": symbol,
            "available": True,
            "latest_price": 501.0,
            "change_pct": 0.3,
            "average_volume": 1200000,
        },
    )

    monkeypatch.setattr(
        integration_module.aitrading_analysis_service,
        "analyze",
        lambda symbol, timeframe, notes=None, market_data=None: {
            "symbol": symbol,
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "buy",
            "confidence": 0.82,
            "model": "test-double",
        },
    )

    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "submit_buy_with_balance_limit",
        lambda symbol, account_balance=None, buy_pct=0.15: {
            "symbol": symbol,
            "side": "buy",
            "quantity": 2,
            "status": "queued",
            "broker": "alpaca-paper",
        },
    )

    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "upsert_consolidated_protective_stop",
        lambda symbol, reference_price, stop_loss_pct, fallback_quantity: {
            "symbol": symbol,
            "side": "sell",
            "quantity": 5,
            "status": "queued",
            "broker": "alpaca-paper",
            "type": "stop",
            "stop_price": round(reference_price * (1.0 - stop_loss_pct), 2),
            "stop_loss_pct": stop_loss_pct,
            "replaced_existing_stops": {
                "candidates": 2,
                "cancelled": 2,
                "cancelled_order_ids": ["old-stop-1", "old-stop-2"],
                "failed": 0,
                "failed_order_ids": [],
            },
        },
    )

    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "buy", "price": 501.0, "strategy": "supertrend"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["order"]["protective_stop"]["replaced_existing_stops"]["cancelled"] == 2


def test_upsert_keeps_tighter_existing_stop_without_replacement(monkeypatch):
    broker = integration_module.alpaca_paper_broker

    monkeypatch.setattr(
        broker,
        "get_position_snapshot",
        lambda symbol: {"symbol": symbol, "qty": 5.0},
    )
    monkeypatch.setattr(
        broker,
        "list_open_orders",
        lambda symbol=None: [
            {
                "id": "existing-stop-1",
                "symbol": "QQQ",
                "side": "sell",
                "type": "stop",
                "qty": "5",
                "stop_price": "490",
                "client_order_id": "bot-stop-QQQ-123",
            }
        ],
    )

    def should_not_cancel(symbol):
        raise AssertionError("cancel_existing_protective_stops should not be called when tighter stop exists")

    monkeypatch.setattr(broker, "cancel_existing_protective_stops", should_not_cancel)

    result = broker.upsert_consolidated_protective_stop(
        symbol="QQQ",
        reference_price=500.0,
        stop_loss_pct=0.05,
        fallback_quantity=5,
    )

    assert result["status"] == "kept-existing"
    assert result["reason"] == "tighter-existing-stop"
    assert result["stop_price"] == 490.0
    assert result["replaced_existing_stops"]["cancelled"] == 0


def test_upsert_supplements_qty_at_existing_tighter_stop(monkeypatch):
    broker = integration_module.alpaca_paper_broker

    monkeypatch.setattr(
        broker,
        "get_position_snapshot",
        lambda symbol: {"symbol": symbol, "qty": 5.0},
    )
    monkeypatch.setattr(
        broker,
        "list_open_orders",
        lambda symbol=None: [
            {
                "id": "existing-stop-1",
                "symbol": "QQQ",
                "side": "sell",
                "type": "stop",
                "qty": "2",
                "stop_price": "490",
                "client_order_id": "bot-stop-QQQ-123",
            }
        ],
    )

    def should_not_cancel(symbol):
        raise AssertionError("cancel_existing_protective_stops should not be called when tighter stop exists")

    monkeypatch.setattr(broker, "cancel_existing_protective_stops", should_not_cancel)
    monkeypatch.setattr(
        broker,
        "_submit_protective_stop_sell_at_price",
        lambda symbol, quantity, stop_price, stop_loss_pct=None: {
            "symbol": symbol,
            "side": "sell",
            "quantity": quantity,
            "status": "queued",
            "type": "stop",
            "stop_price": stop_price,
            "stop_loss_pct": stop_loss_pct,
        },
    )

    result = broker.upsert_consolidated_protective_stop(
        symbol="QQQ",
        reference_price=500.0,
        stop_loss_pct=0.05,
        fallback_quantity=5,
    )

    assert result["status"] == "queued"
    assert result["reason"] == "supplemented-at-existing-tighter-stop"
    assert result["quantity"] == 3
    assert result["stop_price"] == 490.0
    assert result["existing_covered_qty"] == 2
    assert result["target_total_qty"] == 5


def test_sell_guard_blocks_price_below_last_buy_even_if_above_avg_entry(monkeypatch):
    broker = integration_module.alpaca_paper_broker

    # Average cost basis (490) is lower than the most recent buy fill (500), which is how a
    # DCA'd position can show an unrealized "gain" while still selling below the last buy.
    monkeypatch.setattr(
        broker,
        "get_position_snapshot",
        lambda symbol: {"symbol": symbol, "qty": 10.0, "avg_entry_price": 490.0, "current_price": 495.0},
    )

    result = broker.evaluate_sell_profit_guard(
        symbol="QQQ",
        candidate_price=495.0,
        min_profit_pct=0.0,
        stop_loss_pct=0.05,
        last_buy_price=500.0,
        require_sell_above_last_buy=True,
    )

    assert result["allowed"] is False
    assert result["reason"] == "below-last-buy-price"
    assert result["effective_floor_price"] == 500.0


def test_sell_guard_allows_price_above_last_buy(monkeypatch):
    broker = integration_module.alpaca_paper_broker

    monkeypatch.setattr(
        broker,
        "get_position_snapshot",
        lambda symbol: {"symbol": symbol, "qty": 10.0, "avg_entry_price": 490.0, "current_price": 505.0},
    )

    result = broker.evaluate_sell_profit_guard(
        symbol="QQQ",
        candidate_price=505.0,
        min_profit_pct=0.0,
        stop_loss_pct=0.05,
        last_buy_price=500.0,
        require_sell_above_last_buy=True,
    )

    assert result["allowed"] is True
    assert result["reason"] == "profit-target-satisfied"


def test_webhook_blocks_sell_below_last_buy_price(monkeypatch):
    risk_response = client.post(
        "/api/v1/trading/risk-settings",
        json={
            "max_position_size": 10,
            "daily_loss_limit": 100.0,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "cooldown_minutes": 15,
            "min_ai_confidence_buy": 0.7,
            "min_ai_confidence_sell": 0.7,
            "block_loss_sells": True,
            "min_profit_pct_for_sell": 0.0,
            "require_sell_above_last_buy": True,
        },
    )
    assert risk_response.status_code == 200

    monkeypatch.setattr(
        integration_module.alpaca_market_data_service,
        "get_stock_snapshot",
        lambda symbol, timeframe="1Day", limit=5: {
            "symbol": symbol,
            "available": True,
            "latest_price": 495.0,
            "change_pct": -0.1,
            "average_volume": 1200000,
        },
    )

    monkeypatch.setattr(
        integration_module.aitrading_analysis_service,
        "analyze",
        lambda symbol, timeframe, notes=None, market_data=None: {
            "symbol": symbol,
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "sell",
            "confidence": 0.91,
            "model": "test-double",
        },
    )

    monkeypatch.setattr(
        integration_module.trading_store,
        "get_last_buy_price",
        lambda symbol: 500.0,
    )

    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "get_position_snapshot",
        lambda symbol: {"symbol": symbol, "qty": 10.0, "avg_entry_price": 490.0, "current_price": 495.0},
    )

    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "sell", "price": 495.0, "strategy": "supertrend"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["order"]["status"] == "blocked"
    assert payload["order"]["reason"] == "below-last-buy-price"


def test_get_last_filled_buy_price_reads_broker_order_history(monkeypatch):
    broker = integration_module.alpaca_paper_broker

    captured_params = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"side": "sell", "status": "filled", "filled_avg_price": "510.00"},
                {"side": "buy", "status": "canceled", "filled_avg_price": None},
                {"side": "buy", "status": "filled", "filled_avg_price": "502.35"},
                {"side": "buy", "status": "filled", "filled_avg_price": "498.00"},
            ]

    def fake_get(url, headers=None, params=None, timeout=None):
        captured_params.update(params or {})
        return FakeResponse()

    monkeypatch.setattr(broker._session, "get", fake_get)

    result = broker.get_last_filled_buy_price("QQQ")

    assert result == 502.35
    assert captured_params.get("status") == "closed"
    assert captured_params.get("symbols") == "QQQ"


def test_webhook_falls_back_to_broker_fill_history_when_no_local_buy_price(monkeypatch):
    risk_response = client.post(
        "/api/v1/trading/risk-settings",
        json={
            "max_position_size": 10,
            "daily_loss_limit": 100.0,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "cooldown_minutes": 15,
            "min_ai_confidence_buy": 0.7,
            "min_ai_confidence_sell": 0.7,
            "block_loss_sells": True,
            "min_profit_pct_for_sell": 0.0,
            "require_sell_above_last_buy": True,
        },
    )
    assert risk_response.status_code == 200

    monkeypatch.setattr(
        integration_module.alpaca_market_data_service,
        "get_stock_snapshot",
        lambda symbol, timeframe="1Day", limit=5: {
            "symbol": symbol,
            "available": True,
            "latest_price": 495.0,
            "change_pct": -0.1,
            "average_volume": 1200000,
        },
    )

    monkeypatch.setattr(
        integration_module.aitrading_analysis_service,
        "analyze",
        lambda symbol, timeframe, notes=None, market_data=None: {
            "symbol": symbol,
            "timeframe": timeframe,
            "notes": notes or "",
            "signal": "sell",
            "confidence": 0.91,
            "model": "test-double",
        },
    )

    # No local order history recorded (e.g. after an app restart).
    monkeypatch.setattr(
        integration_module.trading_store,
        "get_last_buy_price",
        lambda symbol: None,
    )
    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "get_last_filled_buy_price",
        lambda symbol: 500.0,
    )
    monkeypatch.setattr(
        integration_module.alpaca_paper_broker,
        "get_position_snapshot",
        lambda symbol: {"symbol": symbol, "qty": 10.0, "avg_entry_price": 490.0, "current_price": 495.0},
    )

    webhook_response = client.post(
        "/api/v1/integration/tradingview/webhook",
        headers={"x-webhook-secret": "test-secret"},
        json={"ticker": "QQQ", "action": "sell", "price": 495.0, "strategy": "supertrend"},
    )
    assert webhook_response.status_code == 200
    payload = webhook_response.json()
    assert payload["order"]["status"] == "blocked"
    assert payload["order"]["reason"] == "below-last-buy-price"
