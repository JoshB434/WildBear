"""Strict schemas for the QQQ AI Webhook Signals TradingView indicator."""
import re
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

STRATEGY_ID = "qqq_pullback_v2"
SUPPORTED_SYMBOL = "QQQ"
SUPPORTED_TIMEFRAME = "5"

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")


class _BaseSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    event_id: str
    symbol: Literal["QQQ"]
    timeframe: Literal["5"]
    bar_time: int = Field(gt=0, description="Bar timestamp in epoch milliseconds")
    price: float = Field(gt=0)
    strategy: Literal["qqq_pullback_v2"]
    # Optional shared secret for senders that cannot set HTTP headers; never stored or logged.
    passphrase: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _check_event_id(self):
        if not _EVENT_ID_RE.match(self.event_id):
            raise ValueError("event_id must be 1-100 chars of letters, digits, _ . : -")
        return self


class EntryCandidate(_BaseSignal):
    event: Literal["ENTRY_CANDIDATE"]
    side: Literal["buy"]
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    atr: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_levels(self):
        if not self.stop_price < self.price:
            raise ValueError("stop_price must be below price for a long entry")
        if not self.target_price > self.price:
            raise ValueError("target_price must be above price for a long entry")
        return self


class ExitSignal(_BaseSignal):
    event: Literal["EXIT_SIGNAL"]
    side: Literal["close_long"]


Signal = Union[EntryCandidate, ExitSignal]
_EVENT_MODELS = {"ENTRY_CANDIDATE": EntryCandidate, "EXIT_SIGNAL": ExitSignal}


class UnsupportedEvent(ValueError):
    pass


def parse_signal(payload: object) -> Signal:
    """Validate a decoded JSON payload. Raises UnsupportedEvent or pydantic ValidationError."""
    if not isinstance(payload, dict):
        raise UnsupportedEvent("payload must be a JSON object")
    event = payload.get("event")
    model = _EVENT_MODELS.get(event) if isinstance(event, str) else None
    if model is None:
        raise UnsupportedEvent(f"unsupported event type: {payload.get('event')!r}")
    return model.model_validate(payload)


def error_summary(exc: ValidationError) -> list[dict]:
    return [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
