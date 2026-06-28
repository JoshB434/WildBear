from fastapi.testclient import TestClient

from main import app

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
