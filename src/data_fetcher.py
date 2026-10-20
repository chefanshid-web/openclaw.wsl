import requests
import os
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import ccxt
import numpy as np
import pandas as pd
from src.config import config

os.makedirs('logs', exist_ok=True)
os.makedirs(config.cache_dir, exist_ok=True)

fetcher_logger = logging.getLogger('Fetcher')
if not fetcher_logger.handlers:
    fetcher_logger.setLevel(logging.INFO)
    fh = logging.FileHandler('logs/fetcher.log')
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    fetcher_logger.addHandler(fh)


class HealthMonitor:
    """
    Monitors exchange API connectivity, rate limits, and latency for Binance & BingX.
    """
    def __init__(self, clients: Dict[str, Any]):
        self.clients = clients
        self.stats = {
            name: {"status": "UNKNOWN", "latency_ms": None, "last_check": None, "errors": 0}
            for name in clients
        }

    def ping(self, exchange_name: str) -> Dict[str, Any]:
        client = self.clients.get(exchange_name)
        if not client:
            return {"status": "ERROR", "error": f"Unknown exchange {exchange_name}"}
        start = time.time()
        try:
            client.fetch_time()
            latency = (time.time() - start) * 1000
            self.stats[exchange_name] = {
                "status": "HEALTHY",
                "latency_ms": round(latency, 2),
                "last_check": datetime.now(timezone.utc).isoformat(),
                "errors": 0,
            }
        except Exception as e:
            self.stats[exchange_name]["status"] = "UNHEALTHY"
            self.stats[exchange_name]["errors"] += 1
            self.stats[exchange_name]["last_error"] = str(e)
            self.stats[exchange_name]["last_check"] = datetime.now(timezone.utc).isoformat()
        return self.stats[exchange_name]

    def check_all(self) -> Dict[str, Any]:
        for name in self.clients:
            self.ping(name)
        return self.stats


class DataFetcher:
    def __init__(self, exchange_id: Optional[str] = None, enable_rate_limit: bool = True):
        self.exchange_id = exchange_id or config.exchange

        # Primary Binance CCXT client
        binance_params = {
            'enableRateLimit': enable_rate_limit,
            'options': {'defaultType': 'future'},
        }
        if config.api_key and config.api_secret:
            binance_params['apiKey'] = config.api_key
            binance_params['secret'] = config.api_secret
        self.binance = ccxt.binance(binance_params)

        # Secondary BingX CCXT client
        bingx_params = {
            'enableRateLimit': enable_rate_limit,
            'options': {'defaultType': 'swap'},
        }
        self.bingx = ccxt.bingx(bingx_params)

        # Active client assignment
        exchange_class = getattr(ccxt, self.exchange_id)
        if self.exchange_id == 'binance':
            self.client = self.binance
        elif self.exchange_id == 'bingx':
            self.client = self.bingx
        else:
            self.client = exchange_class(binance_params)

        # Health monitor integration
        self.health_monitor = HealthMonitor({
            'binance': self.binance,
            'bingx': self.bingx,
        })

    def _get_func_name(self, func) -> str:
        return getattr(func, '__name__', getattr(func, '__qualname__', str(func)))

    def _retry_call(self, func, *args, **kwargs) -> Any:
        delays = [1, 4, 16]
        last_exception = None
        func_name = self._get_func_name(func)
        for attempt, delay in enumerate(delays, start=1):
            try:
                fetcher_logger.info(f"Calling {func_name} (attempt {attempt})")
                return func(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.RateLimitExceeded) as e:
                last_exception = e
                fetcher_logger.warning(f"Network/RateLimit error on attempt {attempt}: {e}. Retrying in {delay}s...")
                time.sleep(delay)
            except Exception as e:
                fetcher_logger.error(f"Fatal error on {func_name}: {e}")
                raise
        fetcher_logger.error(f"Exceeded max retries (3) for {func_name}")
        raise last_exception

    def get_cache_path(self, symbol: str, timeframe: str) -> str:
        safe_sym = symbol.replace('/', '_').replace(':', '_')
        return os.path.join(config.cache_dir, f"{safe_sym}_{timeframe}.parquet")

    @staticmethod
    def drop_unclosed_candles(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """
        Drops in-progress / unclosed candles to eliminate lookahead bias.
        A candle starting at timestamp T is closed only after T + tf_ms <= now.
        """
        if df.empty or 'timestamp' not in df.columns:
            return df
        tf_map = {
            '1m': 60 * 1000,
            '3m': 3 * 60 * 1000,
            '5m': 5 * 60 * 1000,
            '15m': 15 * 60 * 1000,
            '30m': 30 * 60 * 1000,
            '1h': 60 * 60 * 1000,
            '2h': 2 * 60 * 60 * 1000,
            '4h': 4 * 60 * 60 * 1000,
            '6h': 6 * 60 * 60 * 1000,
            '8h': 8 * 60 * 60 * 1000,
            '12h': 12 * 60 * 60 * 1000,
            '1d': 24 * 60 * 60 * 1000,
        }
        tf_ms = tf_map.get(timeframe, 15 * 60 * 1000)
        now_ms = time.time() * 1000
        return df[df['timestamp'] + tf_ms <= now_ms].copy()

    def fetch_ohlcv(self, symbol: str = config.default_symbol, timeframe: str = '15m',
                    limit: int = 100, use_cache: bool = True, drop_unclosed: bool = True) -> pd.DataFrame:
        cache_path = self.get_cache_path(symbol, timeframe)
        
        if use_cache and os.path.exists(cache_path):
            try:
                df = pd.read_parquet(cache_path)
                if not df.empty:
                    fetcher_logger.info(f"Cache hit for {symbol} {timeframe} ({len(df)} rows)")
                    return df
            except Exception as e:
                fetcher_logger.warning(f"Failed to read cache {cache_path}: {e}")

        raw = self._retry_call(self.client.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        
        if drop_unclosed:
            df = self.drop_unclosed_candles(df, timeframe)

        try:
            df.to_parquet(cache_path, index=False)
            fetcher_logger.info(f"Cached {len(df)} rows to {cache_path}")
        except Exception as e:
            fetcher_logger.warning(f"Failed to write cache {cache_path}: {e}")
            
        return df

    def fetch_atr(self, symbol: str = config.default_symbol, timeframe: str = '1h', period: int = 14) -> float:
        """
        Fetches OHLCV candles and calculates the 14-period Average True Range (ATR).
        """
        try:
            df = self.fetch_ohlcv(symbol, timeframe=timeframe, limit=max(period + 15, 50), use_cache=True)
            if df is None or len(df) < 2:
                return 0.0
            high = df['high'].values
            low = df['low'].values
            close = df['close'].values
            tr = np.maximum(
                high[1:] - low[1:],
                np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]))
            )
            if len(tr) < period:
                return float(high[-1] - low[-1])
            return float(pd.Series(tr).rolling(window=period).mean().iloc[-1])
        except Exception as e:
            fetcher_logger.warning(f"Failed to fetch/calculate ATR for {symbol}: {e}")
            return 0.0

    def fetch_order_book(self, symbol: str = config.default_symbol, limit: int = 20) -> Dict[str, Any]:
        ob = self._retry_call(self.client.fetch_order_book, symbol, limit=limit)
        fetcher_logger.info(f"Fetched order book for {symbol} (bids: {len(ob.get('bids', []))}, asks: {len(ob.get('asks', []))})")
        return ob

    def fetch_funding_rate(self, symbol: str = config.default_symbol) -> Dict[str, Any]:
        try:
            res = self._retry_call(self.client.fetch_funding_rate, symbol)
            fetcher_logger.info(f"Fetched funding rate for {symbol}: {res.get('fundingRate')}")
            return res
        except Exception as e:
            fetcher_logger.warning(f"Funding rate not available on {self.exchange_id} for {symbol}: {e}")
            return {'symbol': symbol, 'fundingRate': 0.0, 'timestamp': int(time.time() * 1000)}

    def fetch_funding_rate_history(self, symbol: str = config.default_symbol, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            res = self._retry_call(self.client.fetch_funding_rate_history, symbol, limit=limit)
            fetcher_logger.info(f"Fetched {len(res)} funding rate records for {symbol}")
            return res
        except Exception as e:
            fetcher_logger.warning(f"Funding rate history not available for {symbol}: {e}")
            return []

    def fetch_open_interest(self, symbol: str = config.default_symbol) -> Dict[str, Any]:
        try:
            res = self._retry_call(self.client.fetch_open_interest, symbol)
            fetcher_logger.info(f"Fetched open interest for {symbol}: {res.get('openInterestAmount')}")
            return res
        except Exception as e:
            fetcher_logger.warning(f"Open interest not available for {symbol}: {e}")
            return {'symbol': symbol, 'openInterestAmount': 0.0, 'timestamp': int(time.time() * 1000)}

    def fetch_open_interest_history(self, symbol: str = config.default_symbol, timeframe: str = '4h', limit: int = 10) -> List[Dict[str, Any]]:
        try:
            res = self._retry_call(self.client.fetch_open_interest_history, symbol, timeframe=timeframe, limit=limit)
            fetcher_logger.info(f"Fetched {len(res)} open interest history records for {symbol}")
            return res
        except Exception as e:
            fetcher_logger.warning(f"Open interest history not available for {symbol}: {e}")
            return []

    def fetch_taker_buy_ratio(self, symbol: str = config.default_symbol) -> float:
        """
        Fetches taker buy/sell ratio and computes taker buy percentage [0.0, 1.0].
        If buyVol and sellVol exist: buyVol / (buyVol + sellVol).
        If buySellRatio R exists: R / (1.0 + R).
        """
        clean_sym = symbol.split("/")[0] + "USDT" if "/" in symbol else symbol.replace(":USDT", "").replace("/", "")
        try:
            url = f"https://fapi.binance.com/futures/data/takerlongshortRatio?symbol={clean_sym}&period=5m&limit=1"
            resp = requests.get(url, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                if data and isinstance(data, list) and len(data) > 0:
                    buy_vol = float(data[-1].get("buyVol", 0.0))
                    sell_vol = float(data[-1].get("sellVol", 0.0))
                    if buy_vol + sell_vol > 0:
                        ratio = buy_vol / (buy_vol + sell_vol)
                        fetcher_logger.info(f"Fetched taker buy ratio for {symbol}: {ratio:.4f}")
                        return ratio
                    bs_ratio = float(data[-1].get("buySellRatio", 1.0))
                    ratio = bs_ratio / (1.0 + bs_ratio)
                    fetcher_logger.info(f"Derived taker buy ratio from buySellRatio for {symbol}: {ratio:.4f}")
                    return ratio
        except Exception as e:
            fetcher_logger.warning(f"Error fetching taker buy ratio for {symbol}: {e}")
        return 0.5
