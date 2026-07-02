from fastapi import APIRouter, Header, HTTPException

from app.config import settings
from app.database import trading_store
from app.persistence import save_state
from app.schemas import OrderCreate, RiskSettings
from app.services.ai_analysis import aitrading_analysis_service
from app.services.alpaca_client import alpaca_paper_broker
from app.services.market_data import alpaca_market_data_service
from app.services.tradingview_alerts import tradingview_alert_service

router = APIRouter()


@router.get("/alpaca/status")
def alpaca_status():
    return {
        "configured": bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key),
        "base_url": settings.alpaca_base_url,
        "mode": "paper",
    }


@router.post("/tradingview/webhook")
def tradingview_webhook(payload: dict, x_webhook_secret: str | None = Header(default=None)):
    # Validate webhook secret
    webhook_secret = settings.tradingview_webhook_secret
    if webhook_secret:
        if not x_webhook_secret or x_webhook_secret != webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
    
    # Validate required fields
    ticker = str(payload.get("ticker") or "").strip().upper()
    action = str(payload.get("action") or "hold").strip().lower()
    price = payload.get("price")
    strategy = payload.get("strategy")
    
    if not ticker:
        raise HTTPException(status_code=400, detail="Missing required field: ticker")
    if action not in {"buy", "sell", "hold"}:
        raise HTTPException(status_code=400, detail="Invalid action. Must be: buy, sell, or hold")

    market_data = alpaca_market_data_service.get_stock_snapshot(ticker, timeframe="1Day", limit=5)
    market_summary = alpaca_market_data_service.build_analysis_summary(market_data)

    alert = tradingview_alert_service.receive_alert(
        ticker=ticker,
        action=action,
        price=float(price) if price is not None else None,
        strategy=strategy,
    )
    analysis = aitrading_analysis_service.analyze(
        symbol=ticker,
        timeframe="1D",
        notes=f"{action} alert from TradingView via webhook; strategy={strategy or 'n/a'}; {market_summary}",
        market_data=market_data,
    )

    order_payload = None
    confidence_threshold = float(analysis.get("calibrated_threshold", 0.7))
    if (
        ticker
        and action in {"buy", "sell"}
        and analysis.get("signal") in {"buy", "sell"}
        and analysis["signal"] == action
        and float(analysis.get("confidence", 0.0)) >= confidence_threshold
    ):
        side = analysis["signal"]
        # Execute broker order first to get actual quantity
        if side == "buy":
            broker_order = alpaca_paper_broker.submit_buy_with_balance_limit(ticker, account_balance=None, buy_pct=0.15)
        else:
            broker_order = alpaca_paper_broker.submit_sell_all(ticker)
        # Record order with actual quantity from broker
        actual_quantity = broker_order.get("quantity", 1)
        order = trading_store.create_order(OrderCreate(symbol=ticker, side=side, quantity=actual_quantity))
        order_payload = {"status": broker_order["status"], "broker": broker_order["broker"], "order": order.model_dump()}
        try:
            save_state()
        except Exception:
            pass  # Silently fail if state can't be saved

    return {
        "received": True,
        "payload": payload,
        "alert": alert,
        "market_data": market_data,
        "analysis": analysis,
        "order": order_payload,
    }


@router.get("/ai/status")
def ai_status():
    return {
        "configured": bool(settings.openai_api_key),
        "model": "OpenAI-compatible integration ready",
    }


@router.post("/risk-settings")
def save_risk_settings(settings_in: RiskSettings):
    trading_store.save_risk_settings(settings_in)
    return {"saved": True, "settings": settings_in.model_dump()}
