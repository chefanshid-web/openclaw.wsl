"""The one thing in the "production-ready" drop that was a real bug in our code:
using the still-forming candle.

A 15m candle at timestamp T is only final once T + 15min has passed. Before that its
close is a moving target. Logging it writes a number that was never observable at
that event_time - the exact lookahead that inflates backtests.
"""

from __future__ import annotations

import pandas as pd

from src.observer import TF_MS, closed_bars


def _bars(starts: list[int]) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": starts,
        "open": [1.0] * len(starts),
        "high": [1.0] * len(starts),
        "low": [1.0] * len(starts),
        "close": [1.0] * len(starts),
        "volume": [1.0] * len(starts),
    })


def test_in_progress_candle_is_dropped():
    base = 1_700_000_000_000 - 1_700_000_000_000 % TF_MS
    now = base + TF_MS + 60_000          # 1 minute into the bar at base+TF_MS
    df = _bars([base, base + TF_MS])     # first closed, second still forming
    out = closed_bars(df, TF_MS, now_ms=now)
    assert list(out.timestamp) == [base], "only the fully elapsed candle survives"


def test_all_completed_candles_are_kept():
    base = 1_700_000_000_000 - 1_700_000_000_000 % TF_MS
    now = base + 3 * TF_MS + 1           # all three have closed
    df = _bars([base, base + TF_MS, base + 2 * TF_MS])
    out = closed_bars(df, TF_MS, now_ms=now)
    assert len(out) == 3


def test_exactly_at_close_boundary_is_included():
    base = 1_700_000_000_000 - 1_700_000_000_000 % TF_MS
    df = _bars([base])
    assert len(closed_bars(df, TF_MS, now_ms=base + TF_MS)) == 1


def test_one_millisecond_before_close_is_excluded():
    base = 1_700_000_000_000 - 1_700_000_000_000 % TF_MS
    df = _bars([base])
    assert len(closed_bars(df, TF_MS, now_ms=base + TF_MS - 1)) == 0


def test_empty_frame_is_returned_unchanged():
    out = closed_bars(pd.DataFrame({"timestamp": [], "close": []}), TF_MS, now_ms=1)
    assert out.empty

