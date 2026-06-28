from datetime import datetime, timezone
from pathlib import Path
import json


class TradeJournal:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or "data/trades.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict] = []
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    def log_trade(self, symbol: str, side: str, quantity: int, status: str) -> dict:
        entry = {
            "symbol": symbol.upper(),
            "side": side.lower(),
            "quantity": quantity,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._entries.append(entry)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")
        return entry

    def list_entries(self) -> list[dict]:
        return list(self._entries)
