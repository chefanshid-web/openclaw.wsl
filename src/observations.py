"""Append-only store of live market observations.

Separate from src/logger.py's hypothesis tables: this records the raw STATE
(price, funding, OI, taker flow, long/short ratio) plus the derived features, so a
future hypothesis can be evaluated on data collected before anyone knew about it.

Same PIT rule applies: known_at_time >= event_time, enforced by trigger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from src.config import config


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObservationStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.db_path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c

    def _init(self) -> None:
        with self._conn() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                close REAL,
                volume REAL,
                funding REAL,
                z_funding REAL,
                open_interest REAL,
                oi_change_pct REAL,
                taker_ratio REAL,
                long_short_ratio REAL,
                vol_ratio REAL,
                sweep_bull INTEGER,
                sweep_bear INTEGER,
                metadata TEXT
            )
            """)
            c.execute("""
            CREATE INDEX IF NOT EXISTS idx_obs_symbol_time
            ON observations(symbol, event_time)
            """)
            c.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_pit_observations
            BEFORE INSERT ON observations
            FOR EACH ROW WHEN NEW.known_at_time < NEW.event_time
            BEGIN
                SELECT RAISE(ABORT, 'PIT violation: known_at_time < event_time');
            END
            """)

    def record(self, *, event_time: str, symbol: str, timeframe: str,
               close: float | None = None, volume: float | None = None,
               funding: float | None = None, z_funding: float | None = None,
               open_interest: float | None = None, oi_change_pct: float | None = None,
               taker_ratio: float | None = None, long_short_ratio: float | None = None,
               vol_ratio: float | None = None, sweep_bull: int = 0, sweep_bear: int = 0,
               metadata: dict | None = None) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO observations
                   (event_time, known_at_time, symbol, timeframe, close, volume, funding,
                    z_funding, open_interest, oi_change_pct, taker_ratio,
                    long_short_ratio, vol_ratio, sweep_bull, sweep_bear, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_time, utcnow(), symbol, timeframe, close, volume, funding,
                 z_funding, open_interest, oi_change_pct, taker_ratio,
                 long_short_ratio, vol_ratio, sweep_bull, sweep_bear,
                 json.dumps(metadata or {})),
            )

    def status(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT symbol, COUNT(*) n, MIN(event_time) first, MAX(event_time) last "
                "FROM observations GROUP BY symbol"
            ).fetchall()
            total = c.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        return {"total": total, "by_symbol": [dict(r) for r in rows]}

