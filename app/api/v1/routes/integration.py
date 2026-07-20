from fastapi import APIRouter, Header, HTTPException, Request
import json
from datetime import datetime

from app.config import settings
from app.database import trading_store
from app.persistence import save_state
from app.schemas import OrderCreate, RiskSettings
from app.services.ai_analysis import aitrading_analysis_service
from app.services.alpaca_client import alpaca_paper_broker
from app.services.market_data import alpaca_market_data_service
from app.services.tradingview_alerts import tradingview_alert_service

# Webhook activity log file
WEBHOOK_LOG_FILE = "data/webhook_activity.log"

def log_webhook_activity(level: str, message: str, details: dict = None):
    """Log webhook activity for debugging"""
    timestamp = datetime.utcnow().isoformat()
    log_entry = {
        "timestamp": timestamp,
        "level": level,
        "message": message,
        "details": details or {}
    }
    try:
        with open(WEBHOOK_LOG_FILE, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass  # Silently fail if logging fails

router = APIRouter()


@router.get("/alpaca/status")
def alpaca_status():
    return {
        "configured": bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key),
        "base_url": settings.alpaca_base_url,
        "mode": "paper",
    }


@router.post("/tradingview/webhook")
async def tradingview_webhook(request: Request, x_webhook_secret: str | None = Header(default=None)):
    """Accept both JSON and plain text webhook formats from TradingView"""
    
    # Try to parse the request body
    body_bytes = await request.body()
    content_type = request.headers.get("content-type", "").lower()
    
    # Log raw request
    log_webhook_activity("INFO", "Webhook received", {
        "content_type": content_type,
        "body_length": len(body_bytes),
        "has_secret": bool(x_webhook_secret)
    })
    
    # Parse payload based on content type
    payload = {}
    try:
        if "application/json" in content_type:
            payload = json.loads(body_bytes.decode("utf-8"))
        else:
            # Parse plain text format: "SuperTrend Buy!" or similar
            body_text = body_bytes.decode("utf-8").strip()
            log_webhook_activity("INFO", "Parsing plain text message", {"message": body_text})
            
            # Try to extract action and ticker from the message
            # Expected format: "SuperTrend Buy!" or "SuperTrend Sell!"
            # We need to infer ticker (default to QQQ if not in message)
            message_lower = body_text.lower()
            
            action = "hold"
            if "buy" in message_lower:
                action = "buy"
            elif "sell" in message_lower:
                action = "sell"
            
            # Try to extract ticker from message or use QQQ as default
            ticker = "QQQ"
            for sym in ["BTC", "ETH", "QQQ", "SPY", "TSLA", "AAPL", "MSFT", "NVDA", "AMD"]:
                if sym.lower() in message_lower:
                    ticker = sym
                    break
            
            payload = {
                "ticker": ticker,
                "action": body_text,  # Store original message as action
                "strategy": "Supertrend"
            }
    except Exception as e:
        log_webhook_activity("ERROR", "Failed to parse request", {"error": str(e), "content_type": content_type})
        raise HTTPException(status_code=400, detail=f"Invalid request format: {str(e)}")
    
    # Validate webhook secret
    webhook_secret = settings.tradingview_webhook_secret
    if webhook_secret:
        if not x_webhook_secret or x_webhook_secret != webhook_secret:
            log_webhook_activity("WARN", "Webhook rejected - invalid secret")
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
    
    # Validate required fields
    ticker = str(payload.get("ticker") or "QQQ").strip().upper()
    action_raw = str(payload.get("action") or "hold").strip().lower()
    price = payload.get("price")
    strategy = payload.get("strategy")
    
    if not ticker:
        log_webhook_activity("WARN", "Webhook rejected - missing ticker")
        raise HTTPException(status_code=400, detail="Missing required field: ticker")
    
    # Extract action from TradingView message format (e.g., "SuperTrend Buy!" -> "buy")
    action = "hold"
    if "buy" in action_raw:
        action = "buy"
    elif "sell" in action_raw:
        action = "sell"
    
    if action not in {"buy", "sell", "hold"}:
        log_webhook_activity("WARN", "Webhook rejected - invalid action", {"action": action_raw})
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
    
    log_webhook_activity("INFO", "Analysis complete", {
        "ticker": ticker,
        "action": action,
        "signal": analysis.get("signal"),
        "confidence": analysis.get("confidence"),
        "threshold": analysis.get("calibrated_threshold")
    })

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
        
        # For BUY orders, determine allocation tier for logging
        allocation_info = {}
        if side == "buy":
            try:
                import requests as req
                headers = {
                    "APCA-API-KEY-ID": settings.alpaca_api_key_id or "",
                    "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key or "",
                }
                positions_resp = req.get(f"{settings.alpaca_base_url}/positions", headers=headers, timeout=5)
                positions = positions_resp.json() if positions_resp.ok and isinstance(positions_resp.json(), list) else []
                num_positions = len(positions)
                
                risk_settings = trading_store.get_risk_settings()
                if risk_settings:
                    if num_positions == 0:
                        allocation_info = {"tier": "1st_buy", "allocation_pct": risk_settings.first_buy_allocation_pct}
                    elif num_positions == 1:
                        allocation_info = {"tier": "2nd_buy", "allocation_pct": risk_settings.second_buy_allocation_pct}
                    else:
                        allocation_info = {"tier": "subsequent", "allocation_pct": risk_settings.subsequent_buy_allocation_pct}
            except Exception:
                allocation_info = {"tier": "unknown"}
        
        # Execute broker order first to get actual quantity
        if side == "buy":
            broker_order = alpaca_paper_broker.submit_buy_with_balance_limit(ticker, account_balance=None, buy_pct=0.15)
        else:
            broker_order = alpaca_paper_broker.submit_sell_all(ticker)
        
        # Record order with actual quantity from broker
        actual_quantity = broker_order.get("quantity", 1)
        order = trading_store.create_order(OrderCreate(symbol=ticker, side=side, quantity=actual_quantity))
        order_payload = {"status": broker_order["status"], "broker": broker_order["broker"], "order": order.model_dump()}
        
        log_details = {
            "ticker": ticker,
            "side": side,
            "quantity": actual_quantity,
            "status": broker_order.get("status")
        }
        # Add allocation info if it's a buy order
        if allocation_info:
            log_details.update(allocation_info)
        
        log_webhook_activity("INFO", "Order created", log_details)
        try:
            save_state()
        except Exception:
            pass  # Silently fail if state can't be saved
    else:
        # Log why order was not created
        reasons = []
        if not action or action not in {"buy", "sell"}:
            reasons.append(f"invalid_action: {action}")
        if analysis.get("signal") not in {"buy", "sell"}:
            reasons.append(f"invalid_signal: {analysis.get('signal')}")
        if analysis.get("signal") != action:
            reasons.append(f"signal_mismatch: signal={analysis.get('signal')} vs action={action}")
        if float(analysis.get("confidence", 0.0)) < confidence_threshold:
            reasons.append(f"low_confidence: {analysis.get('confidence')} < {confidence_threshold}")
        log_webhook_activity("INFO", "Order skipped", {
            "ticker": ticker,
            "action": action,
            "signal": analysis.get("signal"),
            "confidence": analysis.get("confidence"),
            "reasons": reasons
        })

    return {
        "received": True,
        "payload": payload,
        "alert": alert,
        "market_data": market_data,
        "analysis": analysis,
        "order": order_payload,
    }


@router.get("/tradingview/webhook/logs")
def get_webhook_logs(limit: int = 50):
    """Get recent webhook activity logs for debugging"""
    try:
        logs = []
        with open(WEBHOOK_LOG_FILE, "r") as f:
            all_lines = f.readlines()
            # Get last 'limit' lines
            for line in all_lines[-limit:]:
                try:
                    logs.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    pass
        return {"count": len(logs), "logs": logs}
    except FileNotFoundError:
        return {"count": 0, "logs": [], "note": "No webhook logs yet"}
    except Exception as e:
        return {"error": str(e), "logs": []}


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


@router.get("/allocation-settings")
def get_allocation_settings():
    """Get current position allocation tier settings"""
    settings = trading_store.get_risk_settings()
    if not settings:
        return {
            "error": "Risk settings not configured",
            "note": "Using defaults: 1st=25%, 2nd=25%, 3rd+=7.5%, max=75%"
        }
    return {
        "first_buy_allocation_pct": settings.first_buy_allocation_pct,
        "second_buy_allocation_pct": settings.second_buy_allocation_pct,
        "subsequent_buy_allocation_pct": settings.subsequent_buy_allocation_pct,
        "max_total_allocation_pct": settings.max_total_allocation_pct,
    }


@router.post("/allocation-settings")
def update_allocation_settings(
    first_buy: float = 25.0,
    second_buy: float = 25.0,
    subsequent_buy: float = 7.5,
    max_total: float = 75.0,
):
    """Update position allocation tier settings
    
    Args:
        first_buy: % of account for 1st buy (1-100)
        second_buy: % of account for 2nd buy (1-100)
        subsequent_buy: % of account for 3rd+ buys (1-50)
        max_total: Max % of account in total positions (1-100)
    """
    settings = trading_store.get_risk_settings()
    if not settings:
        return {"error": "Risk settings not configured"}
    
    # Validate inputs
    if not (1.0 <= first_buy <= 100.0):
        return {"error": "first_buy must be 1-100"}
    if not (1.0 <= second_buy <= 100.0):
        return {"error": "second_buy must be 1-100"}
    if not (1.0 <= subsequent_buy <= 50.0):
        return {"error": "subsequent_buy must be 1-50"}
    if not (1.0 <= max_total <= 100.0):
        return {"error": "max_total must be 1-100"}
    
    # Update settings
    settings.first_buy_allocation_pct = first_buy
    settings.second_buy_allocation_pct = second_buy
    settings.subsequent_buy_allocation_pct = subsequent_buy
    settings.max_total_allocation_pct = max_total
    
    trading_store.save_risk_settings(settings)
    return {
        "updated": True,
        "allocation_tiers": {
            "1st_buy": f"{first_buy}% of account",
            "2nd_buy": f"{second_buy}% of account",
            "3rd+_buys": f"{subsequent_buy}% of account",
            "max_total": f"{max_total}% of account"
        }
    }
