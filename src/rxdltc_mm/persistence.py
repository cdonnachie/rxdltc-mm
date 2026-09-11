"""SQLite persistence. Nothing secret is ever written here."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reference_prices (
    ts REAL NOT NULL, fair_rxd_per_ltc TEXT, sources INTEGER, disagreement_pct TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS balances (ts REAL NOT NULL, coin TEXT NOT NULL, spendable TEXT NOT NULL, unspendable TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    uuid TEXT PRIMARY KEY, side TEXT NOT NULL, price_rxd_per_ltc TEXT NOT NULL, amount TEXT NOT NULL,
    created_at REAL NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS swaps (
    uuid TEXT PRIMARY KEY, side TEXT, my_coin TEXT, other_coin TEXT, my_amount TEXT, other_amount TEXT,
    started_at REAL, finished INTEGER, success INTEGER, recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (ts REAL NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ref_ts ON reference_prices(ts);
CREATE INDEX IF NOT EXISTS idx_bal_ts ON balances(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

STAT_KEYS = ("swaps_total", "swaps_failed", "rxd_bought", "rxd_sold", "ltc_spent", "ltc_received", "two_sided_seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # kv ------------------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO kv(key, value, updated_at) VALUES (?, ?, ?)",
                               (key, json.dumps(value, default=str), time.time()))

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # stats ---------------------------------------------------------------
    def get_stats(self) -> dict[str, Decimal]:
        raw = self.get("stats", {})
        return {k: Decimal(str(raw.get(k, "0"))) for k in STAT_KEYS}

    def add_stats(self, **deltas: Decimal | int | float) -> dict[str, Decimal]:
        stats = self.get_stats()
        for k, v in deltas.items():
            stats[k] = stats.get(k, Decimal(0)) + Decimal(str(v))
        self.set("stats", {k: str(v) for k, v in stats.items()})
        return stats

    # records -------------------------------------------------------------
    def record_reference(self, ts: float, fair: Decimal | None, sources: int, disagreement: Decimal | None, detail: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO reference_prices(ts, fair_rxd_per_ltc, sources, disagreement_pct, detail) VALUES (?, ?, ?, ?, ?)",
                (ts, None if fair is None else str(fair), sources, None if disagreement is None else str(disagreement),
                 json.dumps(detail, default=str)),
            )

    def record_balance(self, ts: float, coin: str, spendable: Decimal, unspendable: Decimal) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO balances(ts, coin, spendable, unspendable) VALUES (?, ?, ?, ?)",
                               (ts, coin, str(spendable), str(unspendable)))

    def last_balance(self, coin: str) -> tuple[Decimal, Decimal] | None:
        with self._lock:
            row = self._conn.execute("SELECT spendable, unspendable FROM balances WHERE coin = ? ORDER BY ts DESC LIMIT 1", (coin,)).fetchone()
        return (Decimal(row[0]), Decimal(row[1])) if row else None

    def record_order(self, uuid: str, side: str, price: Decimal, amount: Decimal, created_at: float, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO orders(uuid, side, price_rxd_per_ltc, amount, created_at, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(uuid) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                (uuid, side, str(price), str(amount), created_at, status, time.time()),
            )

    def set_order_status(self, uuid: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE orders SET status = ?, updated_at = ? WHERE uuid = ?", (status, time.time(), uuid))

    def open_order_uuids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT uuid FROM orders WHERE status = 'open'").fetchall()
        return {r[0] for r in rows}

    def swap_known(self, uuid: str) -> tuple[bool, bool] | None:
        """``(finished, success)`` for a recorded swap, or None if unknown."""
        with self._lock:
            row = self._conn.execute("SELECT finished, success FROM swaps WHERE uuid = ?", (uuid,)).fetchone()
        return (bool(row[0]), bool(row[1])) if row else None

    def record_swap(self, uuid: str, side: str | None, my_coin: str, other_coin: str, my_amount: Decimal,
                    other_amount: Decimal, started_at: float, finished: bool, success: bool) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO swaps(uuid, side, my_coin, other_coin, my_amount, other_amount, started_at, finished, success, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(uuid) DO UPDATE SET finished = excluded.finished, "
                "success = excluded.success, my_amount = excluded.my_amount, other_amount = excluded.other_amount, recorded_at = excluded.recorded_at",
                (uuid, side, my_coin, other_coin, str(my_amount), str(other_amount), started_at, int(finished), int(success), time.time()),
            )

    def record_event(self, level: str, kind: str, message: str, ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events(ts, level, kind, message) VALUES (?, ?, ?, ?)",
                               (ts if ts is not None else time.time(), level, kind, message))

    def clear_events(self) -> int:
        with self._lock:
            n = self._conn.execute("SELECT count(*) FROM events").fetchone()[0]
            self._conn.execute("DELETE FROM events")
        return int(n)

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT ts, level, kind, message FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "level": r[1], "kind": r[2], "message": r[3]} for r in rows]

    def prune(self, older_than_seconds: float = 7 * 86400) -> None:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            for table in ("reference_prices", "balances", "events"):
                self._conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
