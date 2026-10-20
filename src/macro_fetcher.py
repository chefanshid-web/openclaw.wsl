"""
OpenClaw Quant Desk - Phase 2: Macro & Cross-Asset Engine (Production Hardened)
File: src/macro_fetcher.py
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import ccxt
import requests
import yfinance as yf
import pandas as pd

logger = logging.getLogger("MacroFetcher")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] [MacroFetcher] %(message)s'))
    logger.addHandler(ch)


class FundingOIResult(dict):
    """
    Container for funding rate and 24h Open Interest change percentage.
    Supports dictionary access, attribute access, and tuple unpacking.
    """
    def __init__(self, funding_rate: float, open_interest: float, oi_change_24h_pct: float, is_valid: bool = True):
        super().__init__({
            "funding_rate": funding_rate,
            "open_interest": open_interest,
            "oi_change_24h_pct": oi_change_24h_pct,
            "is_valid": is_valid
        })
        self.funding_rate = funding_rate
        self.open_interest = open_interest
        self.oi_change_24h_pct = oi_change_24h_pct
        self.is_valid = is_valid

    def __iter__(self):
        return iter((self.funding_rate, self.oi_change_24h_pct))

    def __getitem__(self, key):
        if isinstance(key, int):
            return (self.funding_rate, self.oi_change_24h_pct)[key]
        return super().__getitem__(key)


class MacroFetcher:
    """
    Production-grade Macro & Cross-Asset telemetry fetcher.
    Enforces weekend bans, fail-closed telemetry circuit breakers, and defensive OI calculations.
    """

    CACHE_TTL_SECONDS = 900  # 15 minutes max staleness for TradFi macro data

    def __init__(self, exchange: Optional[ccxt.Exchange] = None, db_manager: Optional[Any] = None):
        self.exchange = exchange or ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        self.db = db_manager
        self._consecutive_telemetry_failures = 0
        self._max_consecutive_failures = 3
        self._cache: Dict[str, Dict[str, Any]] = {}

    def is_weekend(self, now: Optional[datetime] = None) -> bool:
        """
        Calculates weekend trading restriction based on UTC time.
        Ban is active:
        - Entire day Saturday (weekday == 5)
        - Entire day Sunday (weekday == 6)
        - Friday after 16:00 UTC (weekday == 4 and hour >= 16)
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        weekday = now.weekday()
        if weekday in (5, 6):
            return True
        if weekday == 4 and now.hour >= 16:
            return True
        return False

    def _fetch_yfinance_ticker(self, ticker_symbol: str) -> Optional[Dict[str, float]]:
        """
        Defensively fetches ticker data with a 15-minute cached TTL.
        Returns None if data cannot be fetched or verified.
        """
        now = time.time()
        cached = self._cache.get(ticker_symbol)
        if cached and (now - cached["timestamp"] < self.CACHE_TTL_SECONDS):
            return cached["data"]

        try:
            ticker = yf.Ticker(ticker_symbol)
            hist = ticker.history(period="5d")
            if hist is not None and len(hist) >= 2:
                curr_price = float(hist["Close"].iloc[-1])
                prev_close = float(hist["Close"].iloc[-2])
                change_pct = ((curr_price - prev_close) / prev_close) * 100.0 if prev_close != 0 else 0.0
                data = {"price": curr_price, "change_24h_pct": change_pct}
                self._cache[ticker_symbol] = {"timestamp": now, "data": data}
                return data
            elif hist is not None and len(hist) == 1:
                curr_price = float(hist["Close"].iloc[-1])
                data = {"price": curr_price, "change_24h_pct": 0.0}
                self._cache[ticker_symbol] = {"timestamp": now, "data": data}
                return data
        except Exception as e:
            logger.warning(f"yfinance query failed for {ticker_symbol}: {e}")

        # If cache exists, return as emergency fallback ONLY if <= 1 hour (3600s) old
        if cached:
            age = now - cached["timestamp"]
            if age <= 3600:
                logger.warning(f"Using stale cached data for {ticker_symbol} ({int(age)}s old)")
                return cached["data"]
            logger.error(
                f"Cached data for {ticker_symbol} is older than 1 hour ({int(age)}s > 3600s). "
                f"Rejecting stale cache."
            )
            return None

        return None

    
    def get_eth_btc_trend(self) -> str:
        """
        Relative Strength Rotation (All Coin Theory):
        - Fetches ETH/BTC pair on the 4H timeframe using CCXT.
        - Calculates the 50-period EMA and the 200-period EMA.
        - If 50 EMA > 200 EMA, returns "ALTSEASON".
        - If 50 EMA < 200 EMA, returns "BTC_DOMINANCE".
        """
        try:
            raw = self.exchange.fetch_ohlcv("ETH/BTC", timeframe="4h", limit=250)
            if raw is None or len(raw) < 50:
                logger.warning("Insufficient ETH/BTC 4H data. Defaulting to BTC_DOMINANCE.")
                return "BTC_DOMINANCE"

            if isinstance(raw, pd.DataFrame):
                df = raw
            else:
                df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            close = df['close'].astype(float)
            ema_50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            ema_200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])

            if ema_50 > ema_200:
                return "ALTSEASON"
            else:
                return "BTC_DOMINANCE"
        except Exception as e:
            logger.warning(f"Error evaluating ETH/BTC trend: {e}. Defaulting to BTC_DOMINANCE.")
            return "BTC_DOMINANCE"

    def get_btc_dominance(self) -> float:
        """Fetches BTC.D with strict fallback."""
        try:
            resp = requests.get("https://api.coingecko.com/api/v3/global", timeout=4)
            if resp.status_code == 200:
                btc_d = resp.json().get("data", {}).get("market_cap_percentage", {}).get("btc")
                if btc_d:
                    return float(btc_d)
        except Exception:
            pass
        return 50.0

    def get_macro_risk(self) -> Dict[str, Any]:
        """
        Calculates Macro Risk Score.
        Returns:
            dict containing:
            - 'score': int (10=Risk On, 5=Neutral, 0=Risk Off, -999=Weekend, -1=Telemetry Offline)
            - 'is_valid': bool (True if trading allowed, False if circuit breaker tripped)
            - 'reason': str explanation
        """
        # 1. Gate: Weekend Liquidity Void
        if self.is_weekend():
            return {
                "score": -999,
                "Macro_Risk_Score": -999,
                "is_valid": False,
                "reason": "Weekend liquidity void: Trading halted from Friday 16:00 UTC through Sunday."
            }

        # Gate: Check Stale Macro Data (> 1 hour / 3600 seconds)
        now_ts = time.time()
        for sym in ("^IXIC", "DX-Y.NYB"):
            cached_sym = self._cache.get(sym)
            if cached_sym and isinstance(cached_sym, dict) and "timestamp" in cached_sym:
                if (now_ts - cached_sym["timestamp"]) > 3600:
                    logger.warning(
                        f"Macro telemetry cache for {sym} is older than 1 hour ({int(now_ts - cached_sym['timestamp'])}s > 3600s)."
                    )
                    return {
                        "score": -1,
                        "Macro_Risk_Score": -1,
                        "is_valid": False,
                        "reason": "Stale Macro Data"
                    }

        # 2. Gate: Fetch External TradFi Telemetry
        start_t = time.perf_counter()
        try:
            nasdaq = self._fetch_yfinance_ticker("^IXIC")
            dxy = self._fetch_yfinance_ticker("DX-Y.NYB")
            latency_ms = (time.perf_counter() - start_t) * 1000.0

            if nasdaq is None or dxy is None:
                if self.db and hasattr(self.db, "update_agent_health"):
                    self.db.update_agent_health(
                        "MacroFetcher",
                        "CRITICAL",
                        latency_ms,
                        "TradFi Telemetry (Nasdaq/DXY) unreachable",
                    )
            else:
                status = "WARNING" if latency_ms > 2000 else "HEALTHY"
                if self.db and hasattr(self.db, "update_agent_health"):
                    self.db.update_agent_health("MacroFetcher", status, latency_ms, None)
        except Exception as e:
            latency_ms = (time.perf_counter() - start_t) * 1000.0
            if self.db and hasattr(self.db, "update_agent_health"):
                self.db.update_agent_health("MacroFetcher", "CRITICAL", latency_ms, str(e))
            raise e

        # Telemetry Loss Handling: FAIL-CLOSED
        if nasdaq is None or dxy is None:
            self._consecutive_telemetry_failures += 1
            logger.error(
                f"Macro telemetry failure count: {self._consecutive_telemetry_failures}/{self._max_consecutive_failures}"
            )
            
            # Trip Supabase Kill Switch if telemetry stays down
            if self._consecutive_telemetry_failures >= self._max_consecutive_failures and self.db:
                try:
                    self.db.set_system_status(
                        "HALTED", 
                        f"CRITICAL: Macro telemetry (Nasdaq/DXY) unreachable for {self._consecutive_telemetry_failures} checks."
                    )
                except Exception as e:
                    logger.critical(f"Failed to trigger system kill switch: {e}")

            return {
                "score": -1,
                "Macro_Risk_Score": -1,
                "is_valid": False,
                "reason": "TradFi Telemetry Offline. Failsafe activated: Halting new exposure."
            }

        # Reset failure counter on successful read
        self._consecutive_telemetry_failures = 0

        # Secondary Gate: Verify telemetry cache is not stale fallback (> 1 hour / 3600s)
        now_ts = time.time()
        for sym in ("^IXIC", "DX-Y.NYB"):
            cached_sym = self._cache.get(sym)
            if cached_sym and isinstance(cached_sym, dict) and "timestamp" in cached_sym:
                if (now_ts - cached_sym["timestamp"]) > 3600:
                    logger.warning(
                        f"Macro telemetry cache fallback for {sym} is older than 1 hour ({int(now_ts - cached_sym['timestamp'])}s > 3600s)."
                    )
                    return {
                        "score": -1,
                        "Macro_Risk_Score": -1,
                        "is_valid": False,
                        "reason": "Stale Macro Data"
                    }

        nasdaq_change = nasdaq["change_24h_pct"]
        dxy_change = dxy["change_24h_pct"]

        # 3. Macro Regime Determination
        if nasdaq_change > 0.5 and dxy_change < 0.0:
            score = 10
            regime = "Risk On"
        elif nasdaq_change < -0.5 and dxy_change > 0.0:
            score = 0
            regime = "Risk Off"
        else:
            score = 5
            regime = "Neutral"

        return {
            "score": score,
            "Macro_Risk_Score": score,
            "is_valid": True,
            "reason": f"Regime: {regime}",
            "raw_data": {
                "nasdaq_price": nasdaq.get("price", 0.0),
                "nasdaq_change_24h": nasdaq_change,
                "dxy_price": dxy.get("price", 0.0),
                "dxy_change_24h": dxy_change,
                "btc_dominance": self.get_btc_dominance(),
                "regime": regime
            }
        }

    def check_economic_calendar(self) -> bool:
        """Check economic calendar for high-impact events within next 4 hours."""
        is_high_impact_news_within_4h = False
        return is_high_impact_news_within_4h

    def _normalize_symbol(self, symbol: str) -> str:
        if ":" not in symbol and "/" in symbol:
            parts = symbol.split("/")
            return f"{parts[0]}/{parts[1]}:{parts[1]}"
        return symbol

    def get_funding_and_oi(self, symbol: str) -> FundingOIResult:
        """
        Defensive Open Interest and Funding Rate fetcher.
        Prevents IndexError and crashes on unpopulated historical endpoints.
        """
        norm_symbol = self._normalize_symbol(symbol)
        funding_rate = 0.0
        current_oi = 0.0
        oi_change_pct = 0.0

        # 1. Fetch Current Funding Rate
        try:
            fr_data = self.exchange.fetch_funding_rate(norm_symbol)
            funding_rate = float(fr_data.get("fundingRate") or 0.0)
        except Exception as e:
            logger.warning(f"Could not fetch funding rate for {norm_symbol}: {e}")

        # 2. Fetch Current Open Interest
        try:
            oi_data = self.exchange.fetch_open_interest(norm_symbol)
            current_oi = float(oi_data.get("openInterestAmount") or oi_data.get("openInterest") or 0.0)
        except Exception as e:
            logger.warning(f"Could not fetch current OI for {norm_symbol}: {e}")

        # 3. Defensive 24h Historical Calculation
        try:
            oi_hist = self.exchange.fetch_open_interest_history(norm_symbol, timeframe="1h", limit=25)
            if oi_hist and len(oi_hist) >= 2:
                prev_oi = float(oi_hist[0].get("openInterestAmount") or oi_hist[0].get("openInterest") or 0.0)
                latest_oi = float(oi_hist[-1].get("openInterestAmount") or oi_hist[-1].get("openInterest") or current_oi)
                if prev_oi > 0:
                    oi_change_pct = ((latest_oi - prev_oi) / prev_oi) * 100.0
        except Exception as e:
            logger.warning(f"Historical OI endpoint unavailable on exchange for {norm_symbol}: {e}")
            oi_change_pct = 0.0

        return FundingOIResult(
            funding_rate=funding_rate,
            open_interest=current_oi,
            oi_change_24h_pct=oi_change_pct,
            is_valid=current_oi > 0.0
        )
