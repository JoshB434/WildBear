"""Durable SQLite state for the QQQ pullback strategy: idempotency, position reservations, lockout, audit."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ACTIVE_STATUSES = ("reserved", "open", "ambiguous")
_COUNTED_STATUSES = ("reserved", "open", "ambiguous", "closed")
_POSITION_FIELDS = {
    "status", "entry_order_id", "qty", "entry_price", "stop_price", "target_price",
    "order_submitted", "exit_order_id", "exit_price", "exit_qty", "exit_reason",
    "realized_pl", "detail",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS strategy_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    event_id TEXT NOT NULL,
    entry_client_order_id TEXT NOT NULL,
    entry_order_id TEXT,
    trade_date TEXT NOT NULL,
    qty REAL,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    order_submitted INTEGER NOT NULL DEFAULT 0,
    exit_order_id TEXT,
    exit_price REAL,
    exit_qty REAL,
    exit_reason TEXT,
    realized_pl REAL,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_position
    ON strategy_positions(symbol, strategy) WHERE status IN ('reserved', 'open', 'ambiguous');
CREATE TABLE IF NOT EXISTS lockout (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    locked INTEGER NOT NULL,
    reason TEXT,
    since TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_id TEXT,
    kind TEXT NOT NULL,
    data TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class QQQStateStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=15)
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # --- idempotency ---
    def claim_event(self, event_id: str, event_type: str) -> bool:
        """Atomically claim an event_id. Returns False if it was already seen."""
        now = _now()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(event_id, event_type, status, received_at, updated_at) "
                "VALUES (?, ?, 'processing', ?, ?)",
                (event_id, event_type, now, now),
            )
            return cur.rowcount == 1

    def finish_event(self, event_id: str, status: str, detail: dict[str, Any] | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE events SET status = ?, detail = ?, updated_at = ? WHERE event_id = ?",
                (status, json.dumps(detail or {}, default=str), _now(), event_id),
            )

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
            return dict(row) if row else None

    # --- positions ---
    def reserve_position(
        self,
        symbol: str,
        strategy: str,
        event_id: str,
        client_order_id: str,
        trade_date: str,
        max_trades_per_day: int,
    ) -> tuple[int | None, str | None]:
        """Atomically reserve the single strategy slot. Returns (position_id, rejection_reason)."""
        now = _now()
        with self._tx() as conn:
            placeholders = ",".join("?" * len(_COUNTED_STATUSES))
            count = conn.execute(
                f"SELECT COUNT(*) FROM strategy_positions WHERE trade_date = ? AND strategy = ? "
                f"AND status IN ({placeholders})",
                (trade_date, strategy, *_COUNTED_STATUSES),
            ).fetchone()[0]
            if count >= max_trades_per_day:
                return None, "max-trades-per-day-reached"
            try:
                cur = conn.execute(
                    "INSERT INTO strategy_positions(symbol, strategy, status, event_id, entry_client_order_id, "
                    "trade_date, created_at, updated_at) VALUES (?, ?, 'reserved', ?, ?, ?, ?, ?)",
                    (symbol, strategy, event_id, client_order_id, trade_date, now, now),
                )
            except sqlite3.IntegrityError:
                return None, "active-strategy-position-exists"
            return int(cur.lastrowid), None

    def update_position(self, position_id: int, **fields: Any) -> None:
        unknown = set(fields) - _POSITION_FIELDS
        if unknown:
            raise ValueError(f"unknown position fields: {sorted(unknown)}")
        if "detail" in fields and not isinstance(fields["detail"], str):
            fields["detail"] = json.dumps(fields["detail"], default=str)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE strategy_positions SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), position_id),
            )

    def get_position(self, position_id: int) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM strategy_positions WHERE id = ?", (position_id,)).fetchone()
            return dict(row) if row else None

    def get_active_position(self, symbol: str, strategy: str) -> dict[str, Any] | None:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        with self._tx() as conn:
            row = conn.execute(
                f"SELECT * FROM strategy_positions WHERE symbol = ? AND strategy = ? "
                f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (symbol, strategy, *ACTIVE_STATUSES),
            ).fetchone()
            return dict(row) if row else None

    def count_trades(self, strategy: str, trade_date: str) -> int:
        placeholders = ",".join("?" * len(_COUNTED_STATUSES))
        with self._tx() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM strategy_positions WHERE trade_date = ? AND strategy = ? "
                f"AND status IN ({placeholders})",
                (trade_date, strategy, *_COUNTED_STATUSES),
            ).fetchone()[0]

    # --- lockout ---
    def set_lockout(self, reason: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO lockout(id, locked, reason, since) VALUES (1, 1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET locked = 1, reason = excluded.reason, since = excluded.since",
                (reason, _now()),
            )

    def clear_lockout(self) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE lockout SET locked = 0 WHERE id = 1")

    def get_lockout(self) -> dict[str, Any]:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM lockout WHERE id = 1").fetchone()
        if not row or not row["locked"]:
            return {"locked": False, "reason": None, "since": None}
        return {"locked": True, "reason": row["reason"], "since": row["since"]}

    # --- audit ---
    def audit(self, event_id: str | None, kind: str, data: dict[str, Any] | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO audit(ts, event_id, kind, data) VALUES (?, ?, ?, ?)",
                (_now(), event_id, kind, json.dumps(data or {}, default=str)),
            )

    def list_audit(self, event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._tx() as conn:
            if event_id:
                rows = conn.execute(
                    "SELECT * FROM audit WHERE event_id = ? ORDER BY id DESC LIMIT ?", (event_id, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
