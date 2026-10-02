from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
import hmac
import json
from datetime import datetime

from pydantic import ValidationError

from app.config import settings
from app.database import trading_store
from app.schemas import RiskSettings
from app.services.ai_analysis import aitrading_analysis_service
from app.services.alpaca_client import alpaca_paper_broker
from app.services.market_data import alpaca_market_data_service
from app.services.qqq_pullback import QQQPullbackWorkflow
from app.services.qqq_signals import STRATEGY_ID, UnsupportedEvent, error_summary, parse_signal
from app.services.qqq_state import QQQStateStore

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


@router.post("/tradingview/webhook")
async def tradingview_webhook(request: Request, x_webhook_secret: str | None = Header(default=None)):
    """Receive QQQ AI Webhook Signals (ENTRY_CANDIDATE / EXIT_SIGNAL)."""
    body_bytes = await request.body()
    log_webhook_activity("INFO", "Webhook received", {
        "content_type": request.headers.get("content-type", "").lower(),
        "body_length": len(body_bytes),
        "has_secret": bool(x_webhook_secret),
    })
    return await _handle_qqq_signal(body_bytes, x_webhook_secret)


@router.get("/alpaca/status")
def alpaca_status():
    return {
        "configured": bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key),
        "base_url": settings.alpaca_base_url,
        "mode": "paper",
    }




@router.get("/tradingview/webhook/status")
def qqq_strategy_status():
    workflow = get_qqq_workflow()
    return {
        "strategy": STRATEGY_ID,
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
