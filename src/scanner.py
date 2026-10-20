"""
OpenClaw Quant Lab - Phase 1: Weeks 5-7 Paper Trading Harness
Market Condition Scanner (src/scanner.py)

Responsibilities:
1. Fetch 4H OHLCV for target universe (BTC/USDT:USDT, ETH/USDT:USDT, SOL/USDT:USDT).
2. Calculate Funding Rate Z-score / Percentile and Open Interest Delta.
3. Check pre-registered edge conditions from edge.md:
   - Funding Rate >= 95th percentile
   - Open Interest Delta > +2.5%
4. When conditions are met, log signal to Supabase signals table via SupabaseManager.log_signal().
"""

import time
import logging
from datetime import datetime, timezone, time as dt_time
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd

from src.config import config, TIMEFRAME
from src.data_fetcher import DataFetcher
from src.db_manager import SupabaseManager

logger = logging.getLogger("Scanner")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [Scanner] %(message)s"))
    logger.addHandler(ch)




def is_valid_session(now_dt: Optional[datetime] = None) -> bool:
    """
    Session Awareness (The Time Gate):
    Returns True ONLY if current UTC time is between 07:00 and 16:00 (London/NY overlap).
    Blocks Asian session trading.
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    else:
        now_dt = now_dt.astimezone(timezone.utc)

    current_t = now_dt.time()
    return dt_time(7, 0, 0) <= current_t <= dt_time(16, 0, 0)



def check_gap_risk(
    symbol_or_self: Any,
    symbol: Optional[str] = None,
    fetcher: Optional[Any] = None,
    data_fetcher: Optional[Any] = None,
    df: Optional[pd.DataFrame] = None,
    atr: Optional[float] = None,
) -> bool:
    """
    Evaluates Gap Risk on the 1H timeframe:
    - Fetches the last 2 candles on the 1H timeframe.
    - Calculates the gap size: abs(current_open - prev_close) / prev_close.
    - Fetches the 14-period ATR.
    - Calculates the normalized gap ratio: gap_size / (atr / prev_close).
    - If the ratio is > 1.0 (meaning the gap is larger than 1 full ATR), returns True (High Gap Risk).
    - Otherwise, returns False.
    """
    if isinstance(symbol_or_self, str):
        target_symbol = symbol_or_self
        client_fetcher = data_fetcher or fetcher
    else:
        target_symbol = symbol if symbol is not None else getattr(symbol_or_self, "default_symbol", "BTC/USDT:USDT")
        client_fetcher = data_fetcher or fetcher or getattr(symbol_or_self, "fetcher", None)

    if client_fetcher is None:
        try:
            client_fetcher = DataFetcher()
        except Exception as e:
            logger.warning(f"DataFetcher initialization failed in check_gap_risk: {e}")
            client_fetcher = None

    if df is None and client_fetcher is not None:
        try:
            df = client_fetcher.fetch_ohlcv(target_symbol, timeframe="1h", limit=50, use_cache=False)
        except TypeError:
            try:
                df = client_fetcher.fetch_ohlcv(target_symbol, timeframe="1h", limit=2)
            except Exception:
                df = client_fetcher.fetch_ohlcv(target_symbol)
        except Exception as e:
            logger.error(f"Error fetching OHLCV for {target_symbol} in check_gap_risk: {e}")
            return False

    if df is None or len(df) < 2:
        logger.warning(f"Insufficient OHLCV data for {target_symbol} to calculate gap risk")
        return False

    prev_close = float(df["close"].iloc[-2])
    current_open = float(df["open"].iloc[-1])

    if prev_close <= 0:
        return False

    gap_size = abs(current_open - prev_close) / prev_close

    atr_val = atr
    if atr_val is None:
        if "atr" in df.columns and not df["atr"].empty:
            atr_val = float(df["atr"].iloc[-1])
        elif hasattr(df, "attrs") and "atr" in df.attrs:
            atr_val = float(df.attrs["atr"])
        elif client_fetcher is not None and hasattr(client_fetcher, "fetch_atr") and callable(getattr(client_fetcher, "fetch_atr")):
            try:
                res = client_fetcher.fetch_atr(target_symbol, timeframe="1h", period=14)
                # Ensure not a MagicMock dummy object
                if isinstance(res, (int, float)) and type(res).__name__ != "MagicMock":
                    atr_val = float(res)
            except Exception:
                atr_val = None

    if atr_val is None:
        if "high" in df.columns and "low" in df.columns:
            atr_val = MarketScanner.calculate_atr(df, period=14)
        else:
            atr_val = 1.0

    atr_norm = atr_val / prev_close if prev_close > 0 else 0.0
    if atr_norm <= 1e-9:
        return bool(gap_size > 0.0)

    gap_ratio = gap_size / atr_norm
    return bool(gap_ratio > 1.0)

def calculate_volatility_filter(
    df: pd.DataFrame,
    atr_period: int = 14,
    avg_period: int = 24
) -> Tuple[float, float, bool]:
    """
    Volatility Expansion Filter:
    - Calculates 14-period ATR.
    - Calculates 24-period average of the ATR.
    - Returns (current_atr, avg_atr, is_expanded), where is_expanded is True if current_atr > 1.2 * avg_atr.
    """
    if df is None or len(df) < 2:
        return 0.0, 0.0, False

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    tr1 = high[1:] - low[1:]
    tr2 = np.abs(high[1:] - close[:-1])
    tr3 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))

    tr_series = pd.Series(tr)
    atr_series = tr_series.rolling(window=atr_period).mean().dropna()

    if len(atr_series) == 0:
        val = float(high[-1] - low[-1])
        return val, val, False

    current_atr = float(atr_series.iloc[-1])

    # 24-period average of the 14-period ATR
    if len(atr_series) >= avg_period:
        avg_atr = float(atr_series.tail(avg_period).mean())
    else:
        avg_atr = float(atr_series.mean())

    if avg_atr <= 1e-9:
        is_expanded = current_atr > 0
    else:
        is_expanded = current_atr > 1.2 * avg_atr

    return current_atr, avg_atr, is_expanded


def check_whale_footprint(oi_delta: float, taker_buy_ratio: float) -> Optional[Dict[str, Any]]:
    """
    Evaluates order flow imbalance for institutional whale footprint.
    Condition: abs(oi_delta) > 0.025 and (taker_buy_ratio > 0.65 or taker_buy_ratio < 0.35).
    Returns signal dict with signal_type="WHALE_FLOW_DETECTED" if condition is met, else None.
    """
    normalized_delta = oi_delta / 100.0 if abs(oi_delta) > 1.0 else oi_delta
    if abs(normalized_delta) > 0.025 and (taker_buy_ratio > 0.65 or taker_buy_ratio < 0.35):
        direction = "LONG" if taker_buy_ratio > 0.65 else "SHORT"
        flow_type = "AGGRESSIVE_BUYING" if taker_buy_ratio > 0.65 else "AGGRESSIVE_SELLING"
        score = round(taker_buy_ratio if direction == "LONG" else (1.0 - taker_buy_ratio), 4)
        return {
            "signal_type": "WHALE_FLOW_DETECTED",
            "score": score,
            "direction": direction,
            "flow_type": flow_type,
            "oi_delta": normalized_delta,
            "taker_buy_ratio": taker_buy_ratio,
            "metadata": {
                "flow_type": flow_type,
                "direction": direction,
                "oi_delta": normalized_delta,
                "taker_buy_ratio": taker_buy_ratio,
            }
        }
    return None


def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Calculates 14-period Wilder's Average Directional Index (ADX) from OHLCV dataframe."""
    if df is None or len(df) < period + 2:
        return 22.0  # Fallback to neutral/choppy if insufficient rows

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    tr1 = high[1:] - low[1:]
    tr2 = np.abs(high[1:] - close[:-1])
    tr3 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))

    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean()
    plus_dm_smooth = pd.Series(plus_dm).ewm(alpha=1.0 / period, adjust=False).mean()
    minus_dm_smooth = pd.Series(minus_dm).ewm(alpha=1.0 / period, adjust=False).mean()

    plus_di = 100.0 * (plus_dm_smooth / tr_smooth.replace(0, 1e-9))
    minus_di = 100.0 * (minus_dm_smooth / tr_smooth.replace(0, 1e-9))

    denom = (plus_di + minus_di).replace(0, 1e-9)
    dx = 100.0 * (np.abs(plus_di - minus_di) / denom)
    adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return float(adx_series.iloc[-1])


def apply_regime_gate(signal: Any, adx_value: float) -> Optional[Any]:
    """
    Regime Detection Gate based on 14-period ADX:
    - If ADX < 20: Only allow "MEAN_REVERSION" signals.
    - If ADX > 25: Only allow "CASCADE" or "BREAKOUT" signals.
    - If ADX is 20-25: Reject all signals (returns None).
    """
    if signal is None:
        return None

    if 20.0 <= adx_value <= 25.0:
        return None

    sig_type = ""
    if isinstance(signal, dict):
        sig_type = str(signal.get("signal_type", ""))
    elif isinstance(signal, str):
        sig_type = signal
    else:
        sig_type = getattr(signal, "signal_type", str(signal))

    sig_type_upper = sig_type.upper()

    if adx_value < 20.0:
        if "MEAN_REVERSION" in sig_type_upper:
            return signal
        return None

    if adx_value > 25.0:
        if "CASCADE" in sig_type_upper or "BREAKOUT" in sig_type_upper:
            return signal
        return None

    return None


class MarketScanner:
    """
    Deterministic 1h scanner with 4h macro trend awareness evaluating liquidity cascade triggers from edge.md.
    """
    DEFAULT_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    TIMEFRAME = "1h"

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        db_manager: Optional[SupabaseManager] = None,
        timeframe: Optional[str] = None,
    ):
        self.symbols = symbols or self.DEFAULT_SYMBOLS
        self.TIMEFRAME = timeframe or getattr(config, "TIMEFRAME", "1h")
        self.fetcher = DataFetcher()
        self.db: Optional[SupabaseManager] = db_manager

    def _ensure_db(self) -> Optional[SupabaseManager]:
        """Lazy-initialize or return SupabaseManager."""
        if self.db is not None:
            return self.db
        try:
            self.db = SupabaseManager()
            return self.db
        except Exception as e:
            logger.warning(f"Supabase connection unavailable: {e}. Running in dry-run mode.")
            return None

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
        """Calculates Average True Range (ATR) over specified period."""
        if len(df) < period + 1:
            return float(df["high"].iloc[-1] - df["low"].iloc[-1])

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values

        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1])
            )
        )
        atr_series = pd.Series(tr).rolling(window=period).mean()
        return float(atr_series.iloc[-1])

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> float:
        """Calculates 14-period Wilder's Average Directional Index (ADX)."""
        if len(df) < period + 2:
            return 22.0  # Fallback to neutral/choppy if insufficient rows

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values

        tr1 = high[1:] - low[1:]
        tr2 = np.abs(high[1:] - close[:-1])
        tr3 = np.abs(low[1:] - close[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))

        up_move = high[1:] - high[:-1]
        down_move = low[:-1] - low[1:]

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        # Wilder smoothing (alpha = 1.0 / period)
        tr_smooth = pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean()
        plus_dm_smooth = pd.Series(plus_dm).ewm(alpha=1.0 / period, adjust=False).mean()
        minus_dm_smooth = pd.Series(minus_dm).ewm(alpha=1.0 / period, adjust=False).mean()

        plus_di = 100.0 * (plus_dm_smooth / tr_smooth.replace(0, 1e-9))
        minus_di = 100.0 * (minus_dm_smooth / tr_smooth.replace(0, 1e-9))

        denom = (plus_di + minus_di).replace(0, 1e-9)
        dx = 100.0 * (np.abs(plus_di - minus_di) / denom)
        adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean()
        return float(adx_series.iloc[-1])


    def check_gap_risk(
        self,
        symbol: str,
        fetcher: Optional[Any] = None,
        data_fetcher: Optional[Any] = None,
        df: Optional[pd.DataFrame] = None,
        atr: Optional[float] = None,
    ) -> bool:
        """Evaluates gap risk for target symbol using scanner instance fetcher."""
        client_fetcher = data_fetcher or fetcher or getattr(self, "fetcher", None)
        return check_gap_risk(symbol, fetcher=client_fetcher, df=df, atr=atr)

    def scan_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Scans a single symbol against the edge.md liquidation cascade criteria.
        Returns signal payload if condition is met, else None.
        """
        # Task 1: Session Awareness (The Time Gate)
        if not self.is_valid_session():
            logger.info(f"[{symbol}] Outside London/NY session overlap (07:00-16:00 UTC). Aborting scan.")
            return None

        logger.info(f"Scanning {symbol} ({self.TIMEFRAME})...")

        # 1. Fetch 1H OHLCV
        try:
            df = self.fetcher.fetch_ohlcv(symbol, timeframe=self.TIMEFRAME, limit=50, use_cache=False)
            if df.empty or len(df) < 15:
                logger.warning(f"Insufficient OHLCV data for {symbol}")
                return None
        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return None

        latest_close = float(df["close"].iloc[-1])
        latest_timestamp = int(df["timestamp"].iloc[-1])
        event_time = datetime.fromtimestamp(latest_timestamp / 1000.0, tz=timezone.utc)

        # Task 2: Volatility Expansion Filter (Current 14-ATR > 1.2 * 24-period Avg ATR)
        current_atr, avg_atr, is_vol_expanded = self.calculate_volatility_filter(df, atr_period=14, avg_period=24)
        atr = current_atr
        logger.info(
            f"[{symbol}] Current ATR (14): {current_atr:.2f} | 24-bar Avg ATR: {avg_atr:.2f} | "
            f"Expansion: {is_vol_expanded} (Ratio: {current_atr / (avg_atr if avg_atr > 1e-9 else 1.0):.2f})"
        )

        if not is_vol_expanded:
            logger.info(
                f"[{symbol}] Volatility filter rejected: Current ATR {current_atr:.2f} <= 1.2 * Avg ATR ({1.2 * avg_atr:.2f}). Market lacks momentum."
            )
            return None

        # 2. Fetch Funding Rate History
        try:
            fr_hist = self.fetcher.fetch_funding_rate_history(symbol, limit=100)
            if not fr_hist:
                logger.warning(f"No funding rate history for {symbol}")
                return None
        except Exception as e:
            logger.error(f"Error fetching funding rate history for {symbol}: {e}")
            return None

        fr_rates = np.array([float(r.get("fundingRate", 0.0)) for r in fr_hist])
        latest_fr = float(fr_rates[-1])

        # Compute Funding Z-score & Percentile Rank
        fr_mean = float(np.mean(fr_rates))
        fr_std = float(np.std(fr_rates))
        fr_zscore = (latest_fr - fr_mean) / (fr_std if fr_std > 1e-9 else 1.0)
        # Percentile rank (0 to 100)
        fr_percentile = float(np.mean(fr_rates <= latest_fr) * 100.0)

        # 3. Fetch Open Interest History & Delta
        try:
            oi_hist = self.fetcher.fetch_open_interest_history(symbol, timeframe=self.TIMEFRAME, limit=5)
            if not oi_hist or len(oi_hist) < 2:
                logger.warning(f"Insufficient open interest history for {symbol}")
                return None
        except Exception as e:
            logger.error(f"Error fetching open interest history for {symbol}: {e}")
            return None

        curr_oi = float(oi_hist[-1].get("openInterestAmount", 0.0))
        prev_oi = float(oi_hist[-2].get("openInterestAmount", 0.0))
        if prev_oi <= 0:
            logger.warning(f"Invalid previous OI ({prev_oi}) for {symbol}")
            return None

        oi_delta = (curr_oi - prev_oi) / prev_oi
        oi_delta_pct = oi_delta * 100.0

        logger.info(
            f"[{symbol}] Close: {latest_close:.2f} | ATR: {atr:.2f} | "
            f"Funding: {latest_fr:.6f} (Z: {fr_zscore:.2f}, Pct: {fr_percentile:.1f}%) | "
            f"OI Delta: {oi_delta_pct:+.2f}%"
        )

        # 4. Task 2: Regime Detection (14-period ADX Gate)
        adx = self.calculate_adx(df, period=14)
        if adx < 20.0:
            regime = "Ranging"
        elif adx > 25.0:
            regime = "Trending"
        else:
            regime = "Choppy"

        # 5. Task 1: Fetch Taker Buy Ratio and evaluate Whale Footprint
        try:
            taker_buy_ratio = getattr(self.fetcher, "fetch_taker_buy_ratio", lambda s: 0.5)(symbol)
        except Exception:
            taker_buy_ratio = 0.5

        logger.info(
            f"[{symbol}] ADX: {adx:.2f} ({regime}) | Taker Buy Ratio: {taker_buy_ratio:.4f}"
        )

        # Gap Risk Filter: Check for high gap risk before generating ANY signal
        if self.check_gap_risk(symbol, df=df, atr=atr):
            logger.warning(f"High Gap Risk detected on {symbol}. Skipping signal.")
            return None

        db = self._ensure_db()
        whale_signal = None

        # Task 1 Check: Whale Footprint (check_whale_footprint)
        whale_check = check_whale_footprint(oi_delta, taker_buy_ratio)
        if whale_check is not None:
            whale_dir = whale_check["direction"]
            flow_type = whale_check["flow_type"]
            is_whale = True
            if True:
                whale_score = round(taker_buy_ratio if whale_dir == "LONG" else (1.0 - taker_buy_ratio), 4)
                whale_meta = {
                    "timeframe": self.TIMEFRAME,
                    "close": latest_close,
                    "atr": atr,
                    "direction": whale_dir,
                    "oi_delta_pct": round(oi_delta_pct, 4),
                    "taker_buy_ratio": round(taker_buy_ratio, 4),
                    "flow_type": flow_type,
                    "current_oi": curr_oi,
                    "prev_oi": prev_oi,
                    "adx": round(adx, 2),
                    "regime": regime,
                    "strategy": "whale_flow_order_imbalance",
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                }
                logger.warning(
                    f"[WHALE FOOTPRINT] {symbol} => OI Delta: {oi_delta_pct:+.2f}% | "
                    f"Taker Buy Ratio: {taker_buy_ratio:.4f} ({flow_type}) | Direction: {whale_dir}"
                )
                whale_db_record = None
                if db:
                    try:
                        whale_db_record = db.log_signal(
                            symbol=symbol,
                            event_time=event_time,
                            signal_type="WHALE_FLOW_DETECTED",
                            score=whale_score,
                            metadata=whale_meta,
                            known_at_time=datetime.now(timezone.utc),
                        )
                        logger.info(f"WHALE_FLOW_DETECTED logged to Supabase: {whale_db_record}")
                    except Exception as e:
                        logger.error(f"Failed to log WHALE_FLOW_DETECTED signal: {e}")

                whale_signal = {
                    "symbol": symbol,
                    "event_time": event_time.isoformat(),
                    "signal_type": "WHALE_FLOW_DETECTED",
                    "score": whale_score,
                    "metadata": whale_meta,
                    "db_record": whale_db_record,
                }

        # Task 2: Regime Gating
        # If ADX is 20-25 (Choppy), reject all signals
        if 20.0 <= adx <= 25.0:
            logger.info(f"[{symbol}] Choppy regime (ADX {adx:.1f} in [20, 25]). Rejecting all signals.")
            return None

        # Extreme funding condition check
        funding_extreme = (fr_percentile >= 95.0) or (fr_percentile <= 5.0)

        # ADX < 20 (Ranging): ONLY generate Mean Reversion signals (fade the funding extremes)
        if adx < 20.0:
            if funding_extreme:
                direction = "SHORT" if latest_fr > 0 else "LONG"
                score = round(fr_percentile / 100.0, 4) if latest_fr > 0 else round((100.0 - fr_percentile) / 100.0, 4)
                metadata = {
                    "timeframe": self.TIMEFRAME,
                    "close": latest_close,
                    "atr": atr,
                    "direction": direction,
                    "funding_rate": latest_fr,
                    "funding_zscore": round(fr_zscore, 4),
                    "funding_percentile": round(fr_percentile, 2),
                    "oi_delta_pct": round(oi_delta_pct, 4),
                    "current_oi": curr_oi,
                    "prev_oi": prev_oi,
                    "adx": round(adx, 2),
                    "regime": regime,
                    "strategy": "mean_reversion_funding_fade",
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                }
                logger.warning(
                    f"[MEAN REVERSION TRIGGER] {symbol} => ADX {adx:.1f} (Ranging) | "
                    f"Funding Pct: {fr_percentile:.1f}% | Fade Direction: {direction}"
                )
                db_record = None
                if db:
                    try:
                        db_record = db.log_signal(
                            symbol=symbol,
                            event_time=event_time,
                            signal_type="MEAN_REVERSION",
                            score=score,
                            metadata=metadata,
                            known_at_time=datetime.now(timezone.utc),
                        )
                    except Exception as e:
                        logger.error(f"Failed to log MEAN_REVERSION signal: {e}")

                return {
                    "symbol": symbol,
                    "event_time": event_time.isoformat(),
                    "signal_type": "MEAN_REVERSION",
                    "score": score,
                    "metadata": metadata,
                    "db_record": db_record,
                }

        # ADX > 25 (Trending): ONLY generate Cascade/Continuation signals (ride the trend)
        elif adx > 25.0:
            if funding_extreme and oi_delta_pct > 2.5:
                direction = "SHORT" if latest_fr > 0 else "LONG"
                score = round(fr_percentile / 100.0, 4) if latest_fr > 0 else round((100.0 - fr_percentile) / 100.0, 4)
                metadata = {
                    "timeframe": self.TIMEFRAME,
                    "close": latest_close,
                    "atr": atr,
                    "direction": direction,
                    "funding_rate": latest_fr,
                    "funding_zscore": round(fr_zscore, 4),
                    "funding_percentile": round(fr_percentile, 2),
                    "oi_delta_pct": round(oi_delta_pct, 4),
                    "current_oi": curr_oi,
                    "prev_oi": prev_oi,
                    "adx": round(adx, 2),
                    "regime": regime,
                    "strategy": "cascade_continuation",
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                }
                logger.warning(
                    f"[CASCADE CONTINUATION TRIGGER] {symbol} => ADX {adx:.1f} (Trending) | "
                    f"OI Delta: {oi_delta_pct:+.2f}% | Direction: {direction}"
                )
                db_record = None
                if db:
                    try:
                        db_record = db.log_signal(
                            symbol=symbol,
                            event_time=event_time,
                            signal_type="CASCADE_CONTINUATION",
                            score=score,
                            metadata=metadata,
                            known_at_time=datetime.now(timezone.utc),
                        )
                    except Exception as e:
                        logger.error(f"Failed to log CASCADE_CONTINUATION signal: {e}")

                return {
                    "symbol": symbol,
                    "event_time": event_time.isoformat(),
                    "signal_type": "CASCADE_CONTINUATION",
                    "score": score,
                    "metadata": metadata,
                    "db_record": db_record,
                }

        # If whale footprint occurred and market is not choppy, return whale signal
        if whale_signal:
            return whale_signal

        return None

    def scan_all(self) -> List[Dict[str, Any]]:
        """Scans all configured symbols and returns any triggered signals."""
        start_t = time.perf_counter()
        db = self._ensure_db()
        try:
            # Task 1: Session Awareness (The Time Gate)
            if not self.is_valid_session():
                logger.warning("[SESSION GATE] Current UTC time is outside London/NY overlap (07:00-16:00 UTC). Aborting scan.")
                latency = (time.perf_counter() - start_t) * 1000.0
                if db and hasattr(db, "update_agent_health"):
                    db.update_agent_health("Scanner", "HEALTHY", latency, None)
                return []

            logger.info(f"=== Starting Market Scan across {len(self.symbols)} symbols ===")
            triggered = []
            for sym in self.symbols:
                if self.check_gap_risk(sym):
                    logger.warning(f"High Gap Risk detected on {sym}. Skipping signal.")
                    continue
                sig = self.scan_symbol(sym)
                if sig:
                    triggered.append(sig)
                time.sleep(0.5)  # Rate limiting courtesy

            latency = (time.perf_counter() - start_t) * 1000.0
            logger.info(f"Scan complete. {len(triggered)} signal(s) triggered. Latency: {latency:.2f}ms")
            if db and hasattr(db, "update_agent_health"):
                db.update_agent_health("Scanner", "HEALTHY", latency, None)
            return triggered
        except Exception as e:
            latency = (time.perf_counter() - start_t) * 1000.0
            logger.error(f"Exception in Scanner loop: {e}")
            if db and hasattr(db, "update_agent_health"):
                db.update_agent_health("Scanner", "CRITICAL", latency, str(e))
            raise e


if __name__ == "__main__":
    scanner = MarketScanner()
    results = scanner.scan_all()
    print(f"Triggered signals: {len(results)}")

MarketScanner.check_whale_footprint = staticmethod(check_whale_footprint)
MarketScanner.calculate_adx = staticmethod(calculate_adx)
MarketScanner.apply_regime_gate = staticmethod(apply_regime_gate)

MarketScanner.is_valid_session = staticmethod(is_valid_session)
MarketScanner.calculate_volatility_filter = staticmethod(calculate_volatility_filter)

MarketScanner.check_gap_risk = check_gap_risk
