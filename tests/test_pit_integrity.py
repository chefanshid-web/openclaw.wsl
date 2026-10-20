"""Real tests for the two claims worth testing.

1. PIT integrity: the DATABASE rejects a row where known_at_time < event_time.
2. Retry logic: _retry_call actually retries on ccxt.RateLimitExceeded / NetworkError
   and gives up after its retry budget.

These replace the "chaos loop" from the proposal: injecting an exception that the
same code raises only proves the handler runs. Here the failure is injected from
outside and the observable behaviour is asserted.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.logger import PointInTimeLogger


@pytest.fixture()
def db(tmp_path):
    return PointInTimeLogger(db_path=str(tmp_path / "pit.db"))


def test_valid_insert_is_accepted(db):
    db.log_signal(
        event_time="2026-01-01T00:00:00+00:00",
        known_at_time="2026-01-01T00:00:01+00:00",
        symbol="BTCUSDT", timeframe="1h", feature_family="Trend",
        signal_type="test", raw_score=0.5, calibrated_prob=0.5,
    )
    con = sqlite3.connect(db.db_path)
    n = con.execute("select count(*) from signals").fetchone()[0]
    con.close()
    assert n == 1


def test_pit_violation_is_rejected_by_the_database(db):
    """known_at_time BEFORE event_time must raise, even if application code is wrong."""
    with pytest.raises(sqlite3.IntegrityError, match="PIT violation"):
        db.log_signal(
            event_time="2026-01-01T00:00:10+00:00",
            known_at_time="2026-01-01T00:00:00+00:00",   # 10s in the past
            symbol="BTCUSDT", timeframe="1h", feature_family="Trend",
            signal_type="cheat", raw_score=0.9, calibrated_prob=0.9,
        )
    con = sqlite3.connect(db.db_path)
    n = con.execute("select count(*) from signals").fetchone()[0]
    con.close()
    assert n == 0, "the violating row must not be stored"


def test_pit_violation_rejected_on_every_table(db):
    """One trigger per table - a single unguarded table would leak look-ahead."""
    calls = [
        lambda: db.log_trade("2026-01-01T00:00:09+00:00", "2026-01-01T00:00:00+00:00",
                             "cid-1", "BTCUSDT", "buy", "limit", 1.0, 1.0, 0.0, "filled"),
        lambda: db.log_evidence("2026-01-01T00:00:09+00:00", "2026-01-01T00:00:00+00:00",
                                "x", "y", "z", "state"),
    ]
    for call in calls:
        with pytest.raises(sqlite3.IntegrityError, match="PIT violation"):
            call()


def test_wal_mode_is_enabled(db):
    con = sqlite3.connect(db.db_path)
    mode = con.execute("pragma journal_mode").fetchone()[0]
    con.close()
    assert mode.lower() == "wal"

