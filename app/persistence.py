import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.database import trading_store

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "trading_state.json"


def ensure_data_dir() -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value


def save_state() -> None:
    ensure_data_dir()
    payload = {
        "signals": [_serialize_value(item.model_dump()) for item in trading_store.list_signals()],
        "alerts": [_serialize_value(item.model_dump()) for item in trading_store.list_alerts()],
        "orders": [_serialize_value(item.model_dump()) for item in trading_store.list_orders()],
        "risk_settings": _serialize_value(trading_store.get_risk_settings().model_dump()) if trading_store.get_risk_settings() else None,
    }
    DATA_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_state() -> None:
    if not DATA_FILE.exists():
        return
    payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for item in payload.get("signals", []):
        trading_store.create_signal(type("SignalCreate", (), item)())
    for item in payload.get("alerts", []):
        trading_store.create_alert(type("AlertCreate", (), item)())
    for item in payload.get("orders", []):
        trading_store.create_order(type("OrderCreate", (), item)())
    if payload.get("risk_settings"):
        trading_store.save_risk_settings(type("RiskSettings", (), payload["risk_settings"])())
