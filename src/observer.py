"""Live observer: collects uncontaminated forward data. Never trades.

Every cycle, per symbol:
  - 15m and 1h klines (price, volume)
  - funding rate history  -> rolling z-score (7-day window at 8h funding = 21 points)
  - open interest + 5m change   (Binance only serves ~30 days of this)
  - taker buy/sell ratio, global long/short account ratio
  - derived: vol ratio, sweep flags from the project's own detector

Everything is written to `observations` with a PIT trigger, so nothing can be
back-dated later. The point is the DATA, not the hypotheses: all four hypothesised
features are recorded as raw state, so any future hypothesis can be evaluated on it.

Robust by construction: one symbol or endpoint failing never stops the loop, and the
process is wrapped by a supervisor that restarts it (see start-observer.cmd).

Usage:
  venv/bin/python -m src.observer --once        # single cycle, for verification
  venv/bin/python -m src.observer               # loop every 5 minutes
  venv/bin/python -m src.observer --status      # what has been collected
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.data_fetcher import DataFetcher
from src.features.sweep import LiquiditySweepDetector
from src.observations import ObservationStore

FAPI = "https://fapi.binance.com"
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL_MIN = 5
Z_WINDOW_POINTS = 21          # 7 days of 8h funding
TF_MS = 15 * 60 * 1000        # 15m bar, in milliseconds


def _get(url: str, timeout: int = 20):
    """Public endpoint, no credentials. Returns None instead of raising."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "observer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def fetch_klines(client: DataFetcher, symbol: str, tf: str, limit: int = 120) -> pd.DataFrame | None:
    try:
        df = client.fetch_ohlcv(f"{symbol[:-4]}/USDT:USDT", tf, limit=limit, use_cache=False)
        return df if df is not None and len(df) else None
    except Exception:
        return None


def closed_bars(df: pd.DataFrame, tf_ms: int, now_ms: float | None = None) -> pd.DataFrame:
    """Keep only candles whose full interval has elapsed."""
    if df.empty:
        return df
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    return df[df.timestamp + tf_ms <= now_ms]


def compute_features(symbol: str) -> dict:
    out: dict = {"symbol": symbol, "metadata": {}}

    # ---- price / volume -------------------------------------------------
    df15 = _get(f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval=15m&limit=120")
    if not df15:
        return {}
    bars = pd.DataFrame([{"timestamp": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                          "low": float(k[3]), "close": float(k[4]),
                          "volume": float(k[5])} for k in df15])
    # Drop the still-forming candle. Its close moves until the bar ends, so logging it
    # writes a value that was never actually observable at that timestamp. This is the
    # lookahead bug that silently inflates backtests.
    bars = closed_bars(bars, TF_MS)
    if bars.empty:
        return {}
    last = bars.iloc[-1]
    out["close"] = float(last.close)
    out["volume"] = float(last.volume)
    out["vol_ratio"] = (float(last.volume / bars.volume.tail(24).mean())
                        if bars.volume.tail(24).mean() > 0 else None)
    # event_time = when the candle CLOSED (the event), not when it opened
    out["event_time"] = datetime.fromtimestamp((last.timestamp + TF_MS) / 1000,
                                               tz=timezone.utc).isoformat()

    # ---- sweep detector (the project's own) -----------------------------
    try:
        sweeps = LiquiditySweepDetector().detect(bars)
        last_ts = int(bars.timestamp.iloc[-1])
        out["sweep_bull"] = int(any(s["timestamp"] == last_ts and s["direction"] == "LONG" for s in sweeps))
        out["sweep_bear"] = int(any(s["timestamp"] == last_ts and s["direction"] == "SHORT" for s in sweeps))
    except Exception:
        out["sweep_bull"] = out["sweep_bear"] = 0

    # ---- funding + z ----------------------------------------------------
    fr = _get(f"{FAPI}/fapi/v1/fundingRate?symbol={symbol}&limit=500")
    if fr:
        rates = pd.Series([float(x["fundingRate"]) for x in fr])
        out["funding"] = float(rates.iloc[-1])
        win = rates.tail(Z_WINDOW_POINTS)
        if len(win) >= 10 and win.std() > 0:
            out["z_funding"] = float((win.iloc[-1] - win.mean()) / win.std())

    # ---- open interest (ephemeral: ~30 days of history only) -------------
    oi = _get(f"{FAPI}/futures/data/openInterestHist?symbol={symbol}&period=5m&limit=3")
    if oi and len(oi) >= 2:
        out["open_interest"] = float(oi[-1]["sumOpenInterest"])
        prev = float(oi[-2]["sumOpenInterest"])
        if prev > 0:
            out["oi_change_pct"] = (out["open_interest"] - prev) / prev

    # ---- taker flow + account long/short --------------------------------
    taker = _get(f"{FAPI}/futures/data/takerlongshortRatio?symbol={symbol}&period=5m&limit=1")
    if taker:
        out["taker_ratio"] = float(taker[-1]["buySellRatio"])
    ls = _get(f"{FAPI}/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=5m&limit=1")
    if ls:
        out["long_short_ratio"] = float(ls[-1]["longShortRatio"])

    return out


def one_cycle(symbols: list[str]) -> int:
    store = ObservationStore()
    logged = 0
    for sym in symbols:
        try:
            f = compute_features(sym)
            if not f:
                print(f"  {sym}: no data (endpoint unavailable)")
                continue
            store.record(
                event_time=f.get("event_time") or datetime.now(timezone.utc).isoformat(),
                symbol=sym, timeframe="15m", close=f.get("close"), volume=f.get("volume"),
                funding=f.get("funding"), z_funding=f.get("z_funding"),
                open_interest=f.get("open_interest"), oi_change_pct=f.get("oi_change_pct"),
                taker_ratio=f.get("taker_ratio"), long_short_ratio=f.get("long_short_ratio"),
                vol_ratio=f.get("vol_ratio"),
                sweep_bull=f.get("sweep_bull", 0), sweep_bear=f.get("sweep_bear", 0),
                metadata={"cycle_utc": datetime.now(timezone.utc).isoformat()},
            )
            logged += 1
            print(f"  {sym}: close {f.get('close')}  funding {f.get('funding')}  "
                  f"z {f.get('z_funding')}  oi% {f.get('oi_change_pct')}  "
                  f"taker {f.get('taker_ratio')}")
        except Exception as e:
            print(f"  {sym}: FAILED {type(e).__name__}: {e}")
            traceback.print_exc(limit=1)
    return logged


def sleep_to_next(interval_min: int) -> None:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(minutes=interval_min)).replace(
        minute=(now.minute // interval_min) * interval_min + interval_min, second=0, microsecond=0)
    time.sleep(max(5, (nxt - now).total_seconds()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--status", action="store_true", help="print collected counts")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--interval", type=int, default=INTERVAL_MIN)
    args = ap.parse_args()

    if args.status:
        st = ObservationStore().status()
        print(json.dumps(st, indent=2, default=str))
        return

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.once:
        print(f"single cycle: {symbols}")
        n = one_cycle(symbols)
        print(f"logged {n} rows")
        return

    print(f"observer starting | symbols={symbols} | every {args.interval} min | NO TRADING")
    consecutive_fail = 0
    while True:
        try:
            started = time.time()
            n = one_cycle(symbols)
            consecutive_fail = 0 if n else consecutive_fail + 1
            print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] cycle done, "
                  f"{n} rows, {time.time()-started:.1f}s", flush=True)
        except Exception as e:
            consecutive_fail += 1
            print(f"cycle error ({consecutive_fail}): {type(e).__name__}: {e}", flush=True)
        # back off if the exchange is unhappy, but never exit
        if consecutive_fail >= 3:
            time.sleep(min(300, 30 * consecutive_fail))
        sleep_to_next(args.interval)


if __name__ == "__main__":
    sys.exit(main())

