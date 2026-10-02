from fastapi.testclient import TestClient
import pytest

from main import app
from app.api.v1.routes import integration as integration_module

client = TestClient(app)



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


