from fastapi import APIRouter

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.backtest import BacktestEngine
from app.database import trading_store
from app.journal import TradeJournal
from app.persistence import save_state
from app.scheduler import scheduler
from app.schemas import AIAnalysisRequest, AIAnalysisResult, AlertCreate, OrderCreate, RiskSettings, SignalCreate
from app.services.ai_analysis import aitrading_analysis_service
from app.services.alpaca_client import alpaca_paper_broker
from app.services.tradingview_alerts import tradingview_alert_service

router = APIRouter()
journal = TradeJournal()
backtest_engine = BacktestEngine()


@router.post("/signals/", response_model=dict)
def create_signal(signal_in: SignalCreate):
    signal = trading_store.create_signal(signal_in)
    payload = signal.model_dump()
    return {"symbol": payload["symbol"], "action": payload["action"], "confidence": payload["confidence"], "strategy": payload["strategy"], "signal": payload, "source": "internal-signal"}


@router.post("/alerts/{provider}", response_model=dict)
def receive_alert(provider: str, alert_in: AlertCreate):
    alert = trading_store.create_alert(alert_in)
    result = tradingview_alert_service.receive_alert(
        ticker=alert_in.ticker,
        action=alert_in.action,
        price=alert_in.price,
        strategy=alert_in.strategy,
    )
    payload = alert.model_dump()
    return {"provider": provider, "ticker": payload["ticker"], "action": payload["action"], "price": payload["price"], "strategy": payload["strategy"], "alert": payload, "received": result}


@router.post("/orders/{mode}", response_model=dict)
def create_order(mode: str, order_in: OrderCreate):
    if mode != "paper":
        return {"status": "unsupported-mode", "mode": mode}
    order = trading_store.create_order(order_in)
    broker_order = alpaca_paper_broker.submit_order(order_in.symbol, order_in.side, order_in.quantity)
    if broker_order["status"] == "blocked":
        journal.log_trade(order_in.symbol, order_in.side, order_in.quantity, "blocked")
        save_state()
        raise HTTPException(status_code=409, detail=broker_order["reason"])
    journal.log_trade(order_in.symbol, order_in.side, order_in.quantity, broker_order["status"])
    save_state()
    return {"status": broker_order["status"], "order": order.model_dump(), "broker": broker_order}


@router.post("/analysis/ai", response_model=AIAnalysisResult)
def ai_analysis(payload: AIAnalysisRequest):
    return aitrading_analysis_service.analyze(symbol=payload.symbol, timeframe=payload.timeframe, notes=payload.notes)


@router.get("/history")
def trading_history():
    return {
        "signals": [item.model_dump() for item in trading_store.list_signals()],
        "alerts": [item.model_dump() for item in trading_store.list_alerts()],
        "orders": [item.model_dump() for item in trading_store.list_orders()],
    }


@router.post("/risk-settings")
def save_risk_settings(settings_in: RiskSettings):
    trading_store.save_risk_settings(settings_in)
    save_state()
    return {"saved": True, "settings": settings_in.model_dump()}


@router.post("/schedule")
def schedule_job(minutes: int = 5):
    run_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    scheduler.add_job(run_at, lambda: save_state(), f"persist-state-{minutes}")
    return {"scheduled": True, "run_at": run_at.isoformat()}


@router.get("/backtest")
def run_backtest():
    signals = [signal.model_dump() for signal in trading_store.list_signals()]
    results = backtest_engine.run(signals)
    return {"results": [result.__dict__ for result in results]}


@router.get("/journal")
def list_journal():
    return {"entries": journal.list_entries()}
