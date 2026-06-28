from typing import Dict, Optional

from app.schemas import (
    Alert,
    AlertCreate,
    Asset,
    AssetCreate,
    AssetUpdate,
    Order,
    OrderCreate,
    RiskSettings,
    Signal,
    SignalCreate,
)


class InMemoryAssetStore:
    def __init__(self) -> None:
        self._assets: Dict[str, Asset] = {}
        self._next_id = 1

    def list_assets(self) -> list[Asset]:
        return list(self._assets.values())

    def get_asset(self, symbol: str) -> Optional[Asset]:
        return self._assets.get(symbol.upper())

    def create_asset(self, asset_in: AssetCreate) -> Asset:
        symbol = asset_in.symbol.upper()
        asset = Asset(
            id=self._next_id,
            symbol=symbol,
            name=asset_in.name,
            sector=asset_in.sector,
            exchange=asset_in.exchange,
        )
        self._assets[symbol] = asset
        self._next_id += 1
        return asset

    def update_asset(self, symbol: str, asset_in: AssetUpdate) -> Optional[Asset]:
        existing = self._assets.get(symbol.upper())
        if not existing:
            return None

        updated_data = existing.model_dump()
        if asset_in.name is not None:
            updated_data["name"] = asset_in.name
        if asset_in.sector is not None:
            updated_data["sector"] = asset_in.sector
        if asset_in.exchange is not None:
            updated_data["exchange"] = asset_in.exchange

        updated_asset = Asset(**updated_data)
        self._assets[symbol.upper()] = updated_asset
        return updated_asset

    def delete_asset(self, symbol: str) -> Optional[Asset]:
        return self._assets.pop(symbol.upper(), None)


class InMemoryTradingStore:
    def __init__(self) -> None:
        self._signals: Dict[int, Signal] = {}
        self._alerts: Dict[int, Alert] = {}
        self._orders: Dict[int, Order] = {}
        self._risk_settings: Optional[RiskSettings] = None
        self._next_signal_id = 1
        self._next_alert_id = 1
        self._next_order_id = 1

    def create_signal(self, signal_in: SignalCreate) -> Signal:
        signal = Signal(
            id=self._next_signal_id,
            symbol=signal_in.symbol.upper(),
            action=signal_in.action.lower(),
            confidence=signal_in.confidence,
            strategy=signal_in.strategy,
        )
        self._signals[self._next_signal_id] = signal
        self._next_signal_id += 1
        return signal

    def create_alert(self, alert_in: AlertCreate) -> Alert:
        alert = Alert(
            id=self._next_alert_id,
            ticker=alert_in.ticker.upper(),
            action=alert_in.action.lower(),
            price=alert_in.price,
            strategy=alert_in.strategy,
        )
        self._alerts[self._next_alert_id] = alert
        self._next_alert_id += 1
        return alert

    def create_order(self, order_in: OrderCreate) -> Order:
        order = Order(
            id=self._next_order_id,
            symbol=order_in.symbol.upper(),
            side=order_in.side.lower(),
            quantity=order_in.quantity,
            order_type=order_in.order_type,
        )
        self._orders[self._next_order_id] = order
        self._next_order_id += 1
        return order

    def list_signals(self) -> list[Signal]:
        return list(self._signals.values())

    def list_alerts(self) -> list[Alert]:
        return list(self._alerts.values())

    def list_orders(self) -> list[Order]:
        return list(self._orders.values())

    def save_risk_settings(self, settings_in: RiskSettings) -> RiskSettings:
        self._risk_settings = settings_in
        return settings_in

    def get_risk_settings(self) -> Optional[RiskSettings]:
        return self._risk_settings


asset_store = InMemoryAssetStore()
trading_store = InMemoryTradingStore()
