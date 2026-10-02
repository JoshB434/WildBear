import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    alpaca_api_key_id: str | None = "PKD7FPV7WNLAFFANMC4FLHXPGD"
    alpaca_api_secret_key: str | None = "oyD2KuEC2J9WYoRgRs7xRQuToxJ4vmCjExmZsnf7HGp"
    alpaca_base_url: str | None = "https://paper-api.alpaca.markets/v2"
    alpaca_data_base_url: str | None = "https://data.alpaca.markets/v2"
    tradingview_webhook_secret: str | None = os.getenv("TRADINGVIEW_WEBHOOK_SECRET")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")

    # QQQ pullback strategy (qqq_pullback_v2). Entries stay disabled until explicitly enabled.
    qqq_trading_enabled: bool = _env_bool("QQQ_TRADING_ENABLED", False)
    qqq_risk_pct: float = _env_float("QQQ_RISK_PCT", 0.005)  # fraction of equity risked per trade
    qqq_max_position_value: float = _env_float("QQQ_MAX_POSITION_VALUE", 20000.0)
    qqq_max_trades_per_day: int = _env_int("QQQ_MAX_TRADES_PER_DAY", 3)
    qqq_daily_loss_limit_pct: float = _env_float("QQQ_DAILY_LOSS_LIMIT_PCT", 1.0)  # percent of prior-close equity
    qqq_max_alert_age_seconds: int = _env_int("QQQ_MAX_ALERT_AGE_SECONDS", 600)
    qqq_max_entry_slippage_pct: float = _env_float("QQQ_MAX_ENTRY_SLIPPAGE_PCT", 0.3)
    qqq_no_entry_minutes_before_close: int = _env_int("QQQ_NO_ENTRY_MINUTES_BEFORE_CLOSE", 15)
    qqq_window_start: str = os.getenv("QQQ_WINDOW_START", "10:00")  # America/New_York
    qqq_window_end: str = os.getenv("QQQ_WINDOW_END", "15:00")  # America/New_York
    qqq_window_grace_minutes: int = _env_int("QQQ_WINDOW_GRACE_MINUTES", 5)  # delivery delay allowed past the end
    qqq_monitor_interval_seconds: int = _env_int("QQQ_MONITOR_INTERVAL_SECONDS", 60)
    qqq_fill_timeout_seconds: float = _env_float("QQQ_FILL_TIMEOUT_SECONDS", 20.0)
    qqq_state_db_path: str = os.getenv("QQQ_STATE_DB_PATH", "data/qqq_strategy.db")


settings = Settings()
