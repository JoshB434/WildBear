from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
import hmac
import json
from datetime import datetime

from pydantic import ValidationError

from app.config import settings
from app.database import trading_store
from app.persistence import save_state
from app.schemas import OrderCreate, RiskSettings
from app.services.ai_analysis import aitrading_analysis_service
from app.services.alpaca_client import alpaca_paper_broker
from app.services.market_data import alpaca_market_data_service
from app.services.qqq_pullback import QQQPullbackWorkflow
from app.services.qqq_signals import UnsupportedEvent, error_summary, parse_signal
from app.services.qqq_state import QQQStateStore
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

_qqq_workflow: QQQPullbackWorkflow | None = None


def get_qqq_workflow() -> QQQPullbackWorkflow:
    global _qqq_workflow
    if _qqq_workflow is None:
        _qqq_workflow = QQQPullbackWorkflow(
            store=QQQStateStore(settings.qqq_state_db_path),
            broker=alpaca_paper_broker,
            ai=aitrading_analysis_service,
            market_data=alpaca_market_data_service,
            cfg=settings,
            risk_settings_provider=trading_store.get_risk_settings,
            log_hook=lambda level, message, details: log_webhook_activity(level, message, details),
        )
    return _qqq_workflow


def _secret_ok(header_secret: str | None, body_secret: str | None = None) -> bool:
    expected = settings.tradingview_webhook_secret
    if not expected:
        return True
    return any(c and hmac.compare_digest(c, expected) for c in (header_secret, body_secret))


async def _handle_qqq_signal(body_bytes: bytes, header_secret: str | None) -> dict:
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log_webhook_activity("WARN", "QQQ signal rejected - malformed JSON")
        raise HTTPException(status_code=400, detail="Malformed JSON payload")

    body_secret = payload.get("passphrase") if isinstance(payload, dict) else None
    if not _secret_ok(header_secret, body_secret if isinstance(body_secret, str) else None):
        log_webhook_activity("WARN", "Webhook rejected - invalid secret")
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        signal = parse_signal(payload)
    except UnsupportedEvent as exc:
        log_webhook_activity("WARN", "QQQ signal rejected - unsupported event", {"error": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc))
    except ValidationError as exc:
        errors = error_summary(exc)
        log_webhook_activity("WARN", "QQQ signal rejected - validation failed", {"errors": errors})
        raise HTTPException(status_code=422, detail=errors)

    return await run_in_threadpool(get_qqq_workflow().handle, signal)


def _get_side_confidence_threshold(side: str, analysis: dict, risk_settings: RiskSettings | None) -> float:
    calibrated_threshold = float(analysis.get("calibrated_threshold", 0.0) or 0.0)
    if risk_settings is None:
        configured_threshold = 0.72 if side == "buy" else 0.78
    else:
        configured_threshold = (
            float(risk_settings.min_ai_confidence_buy)
            if side == "buy"
            else float(risk_settings.min_ai_confidence_sell)
        )
    return max(calibrated_threshold, configured_threshold)


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
    
    ticker = "unknown"
    try:
        # Try to parse the request body
        body_bytes = await request.body()
        content_type = request.headers.get("content-type", "").lower()
        
        # Log raw request
        log_webhook_activity("INFO", "Webhook received", {
            "content_type": content_type,
            "body_length": len(body_bytes),
            "has_secret": bool(x_webhook_secret)
        })

        try:
            parsed_body = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed_body = None
        if not settings.legacy_webhook_enabled or (isinstance(parsed_body, dict) and "event" in parsed_body):
            return await _handle_qqq_signal(body_bytes, x_webhook_secret)
    
        # Parse payload based on content type
        payload = {}
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
        risk_settings = trading_store.get_risk_settings()
        confidence_threshold = float(analysis.get("calibrated_threshold", 0.7))
        analysis_signal = str(analysis.get("signal") or "").lower()
        side = analysis_signal if analysis_signal in {"buy", "sell"} else ""
        confidence = float(analysis.get("confidence", 0.0) or 0.0)
        if side in {"buy", "sell"}:
            confidence_threshold = _get_side_confidence_threshold(side, analysis, risk_settings)

        if (
            ticker
            and action in {"buy", "sell"}
            and side in {"buy", "sell"}
            and side == action
            and confidence >= confidence_threshold
        ):
            side = action
            
            # For BUY orders, determine allocation tier for logging
            allocation_info = {}
            if side == "buy":
                try:
                    # Count consecutive buy orders since last sell to determine tier
                    num_consecutive_buys = alpaca_paper_broker._count_consecutive_buy_orders(ticker)
                    
                    risk_settings = trading_store.get_risk_settings()
                    if risk_settings:
                        if num_consecutive_buys == 0:
                            allocation_info = {"tier": "1st_buy", "allocation_pct": risk_settings.first_buy_allocation_pct}
                        elif num_consecutive_buys == 1:
                            allocation_info = {"tier": "2nd_buy", "allocation_pct": risk_settings.second_buy_allocation_pct}
                        else:
                            allocation_info = {"tier": "subsequent", "allocation_pct": risk_settings.subsequent_buy_allocation_pct}
                except Exception:
                    allocation_info = {"tier": "unknown"}
            
            # Execute broker order first to get actual quantity
            if side == "buy":
                broker_order = alpaca_paper_broker.submit_buy_with_balance_limit(ticker, account_balance=None, buy_pct=0.15)
            else:
                if risk_settings is None:
                    risk_settings = RiskSettings(
                        max_position_size=50,
                        daily_loss_limit=5.0,
                        stop_loss_pct=0.05,
                        take_profit_pct=0.1,
                        cooldown_minutes=30,
                    )

                if risk_settings.block_loss_sells:
                    candidate_sell_price = (
                        float(market_data.get("latest_price") or 0.0)
                        if isinstance(market_data, dict)
                        else 0.0
                    )
                    if candidate_sell_price <= 0:
                        candidate_sell_price = float(price) if price is not None else None

                    # Fall back to broker fill history if local order history has no recorded price
                    # (e.g. after an app restart with no persisted price).
                    last_buy_price = trading_store.get_last_buy_price(ticker)
                    if last_buy_price is None:
                        last_buy_price = alpaca_paper_broker.get_last_filled_buy_price(ticker)

                    sell_guard = alpaca_paper_broker.evaluate_sell_profit_guard(
                        ticker,
                        candidate_sell_price,
                        float(risk_settings.min_profit_pct_for_sell),
                        float(risk_settings.stop_loss_pct),
                        last_buy_price=last_buy_price,
                        require_sell_above_last_buy=risk_settings.require_sell_above_last_buy,
                    )
                    if not sell_guard.get("allowed", True):
                        log_webhook_activity("INFO", "Sell order blocked by profit guard", {
                            "ticker": ticker,
                            "side": "sell",
                            "reason": sell_guard.get("reason"),
                            "avg_entry_price": sell_guard.get("avg_entry_price"),
                            "candidate_price": sell_guard.get("candidate_price"),
                            "min_take_profit_price": sell_guard.get("min_take_profit_price"),
                            "stop_loss_trigger_price": sell_guard.get("stop_loss_trigger_price"),
                        })
                        return {
                            "received": True,
                            "payload": payload,
                            "alert": alert,
                            "market_data": market_data,
                            "analysis": analysis,
                            "order": {
                                "status": "blocked",
                                "broker": "alpaca-paper",
                                "reason": sell_guard.get("reason"),
                                "guard": sell_guard,
                            },
                        }

                broker_order = alpaca_paper_broker.submit_sell_all(ticker)
            
            # If broker blocks the trade (for example, no open position to sell), return block details.
            if broker_order and broker_order.get("status") == "blocked":
                order_payload = {
                    "status": "blocked",
                    "broker": broker_order.get("broker", "alpaca-paper"),
                    "reason": broker_order.get("reason", "blocked-by-broker"),
                }
                log_webhook_activity("INFO", "Order blocked by broker", {
                    "ticker": ticker,
                    "side": side,
                    "reason": broker_order.get("reason", "blocked-by-broker"),
                })
                return {
                    "received": True,
                    "payload": payload,
                    "alert": alert,
                    "market_data": market_data,
                    "analysis": analysis,
                    "order": order_payload,
                }

            # Record order with actual quantity from broker
            actual_quantity = int(broker_order.get("quantity", 1)) if broker_order else 1
            order_reference_price = None
            if isinstance(market_data, dict):
                order_reference_price = float(market_data.get("latest_price") or 0.0) or None
            if not order_reference_price and price is not None:
                order_reference_price = float(price)
            order = trading_store.create_order(
                OrderCreate(symbol=ticker, side=side, quantity=actual_quantity, price=order_reference_price)
            )
            order_payload = {
                "status": broker_order.get("status") if broker_order else "unknown",
                "broker": broker_order.get("broker") if broker_order else "alpaca-paper",
                "order": order.model_dump()
            }

            if side == "buy" and broker_order and broker_order.get("status") != "blocked":
                if risk_settings is None:
                    risk_settings = RiskSettings(
                        max_position_size=50,
                        daily_loss_limit=5.0,
                        stop_loss_pct=0.05,
                        take_profit_pct=0.1,
                        cooldown_minutes=30,
                    )

                reference_buy_price = 0.0
                if isinstance(market_data, dict):
                    reference_buy_price = float(market_data.get("latest_price") or 0.0)
                if reference_buy_price <= 0 and price is not None:
                    reference_buy_price = float(price)

                if reference_buy_price > 0:
                    adjusted_stop_pct = alpaca_paper_broker.ai_adjusted_stop_loss_pct(
                        float(risk_settings.stop_loss_pct),
                        confidence,
                    )
                    protective_stop = alpaca_paper_broker.upsert_consolidated_protective_stop(
                        ticker,
                        reference_buy_price,
                        adjusted_stop_pct,
                        fallback_quantity=actual_quantity,
                    )
                    order_payload["protective_stop"] = protective_stop
                    replaced = protective_stop.get("replaced_existing_stops") or {}
                    log_webhook_activity("INFO", "Protective stop submitted for buy", {
                        "ticker": ticker,
                        "quantity": actual_quantity,
                        "reference_buy_price": reference_buy_price,
                        "base_stop_loss_pct": float(risk_settings.stop_loss_pct),
                        "ai_confidence": confidence,
                        "adjusted_stop_loss_pct": adjusted_stop_pct,
                        "stop_status": protective_stop.get("status"),
                        "stop_price": protective_stop.get("stop_price"),
                        "stops_replaced": replaced.get("cancelled", 0),
                    })
            
            log_details = {
                "ticker": ticker,
                "side": side,
                "quantity": actual_quantity,
                "status": broker_order.get("status") if broker_order else "unknown"
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
            if confidence < confidence_threshold:
                reasons.append(f"low_confidence: {confidence} < {confidence_threshold}")
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
    
    except HTTPException:
        # Re-raise HTTPExceptions (validation errors, etc.)
        raise
    except Exception as e:
        log_webhook_activity("ERROR", f"Webhook processing failed: {str(e)}", {
            "error_type": type(e).__name__,
            "ticker": ticker
        })
        raise HTTPException(status_code=500, detail=f"Webhook processing failed: {str(e)}")


@router.get("/tradingview/webhook/status")
def qqq_strategy_status():
    workflow = get_qqq_workflow()
    return {
        "strategy": "qqq_pullback_v1",
        "entries_enabled": settings.qqq_trading_enabled,
        "lockout": workflow.store.get_lockout(),
        "mode": "paper" if "paper" in (settings.alpaca_base_url or "") else "live",
    }


@router.post("/tradingview/webhook/reconcile")
async def qqq_strategy_reconcile(x_webhook_secret: str | None = Header(default=None)):
    if not _secret_ok(x_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    return await run_in_threadpool(get_qqq_workflow().reconcile)


@router.post("/tradingview/webhook/lockout/clear")
def qqq_clear_lockout(x_webhook_secret: str | None = Header(default=None)):
    if not settings.tradingview_webhook_secret or not _secret_ok(x_webhook_secret):
        raise HTTPException(status_code=401, detail="Webhook secret required to clear lockout")
    get_qqq_workflow().store.clear_lockout()
    return {"lockout": get_qqq_workflow().store.get_lockout()}


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
