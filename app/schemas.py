from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AssetBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)
    name: str = Field(..., min_length=1)
    sector: Optional[str] = None
    exchange: Optional[str] = None


class AssetCreate(AssetBase):
    pass


class AssetUpdate(BaseModel):
    name: Optional[str] = None
    sector: Optional[str] = None
    exchange: Optional[str] = None


class Asset(AssetBase):
    id: int


class SignalBase(BaseModel):
    symbol: str
    action: str
    confidence: float = Field(ge=0.0, le=1.0)
    strategy: Optional[str] = None


class SignalCreate(SignalBase):
    pass


class Signal(SignalBase):
    id: int
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AlertBase(BaseModel):
    ticker: str
    action: str
    price: Optional[float] = None
    strategy: Optional[str] = None


class AlertCreate(AlertBase):
    pass


class Alert(AlertBase):
    id: int
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OrderBase(BaseModel):
    symbol: str
    side: str
    quantity: int = Field(gt=0)
    order_type: str = "market"


class OrderCreate(OrderBase):
    pass


class Order(OrderBase):
    id: int
    status: str = "queued"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AIAnalysisRequest(BaseModel):
    symbol: str
    timeframe: str = "1D"
    notes: Optional[str] = None


class AIAnalysisResult(BaseModel):
    symbol: str
    timeframe: str
    notes: Optional[str] = None
    signal: str
    confidence: float
    model: str


class RiskSettings(BaseModel):
    max_position_size: int = Field(gt=0)
    daily_loss_limit: float = Field(gt=0)
    stop_loss_pct: float = Field(ge=0.0)
    take_profit_pct: float = Field(ge=0.0)
    cooldown_minutes: int = Field(ge=0)
