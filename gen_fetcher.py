import os

base = os.path.expanduser('~/crypto-screener-v2')

data_fetcher_content = '''import os
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import ccxt
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

class DataFetcher:
    def __init__(self, exchange_id: Optional[str] = None, enable_rate_limit: bool = True):
        self.exchange_id = exchange_id or config.exchange
        exchange_class = getattr(ccxt, self.exchange_id)
        self.client = exchange_class({
            'apiKey': config.api_key,
            'secret': config.api_secret,
            'enableRateLimit': enable_rate_limit,
            'options': {'defaultType': 'future'}
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

    def fetch_ohlcv(self, symbol: str = config.default_symbol, timeframe: str = '15m',
                    limit: int = 100, use_cache: bool = True) -> pd.DataFrame:
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
        
        try:
            df.to_parquet(cache_path, index=False)
            fetcher_logger.info(f"Cached {len(df)} rows to {cache_path}")
        except Exception as e:
            fetcher_logger.warning(f"Failed to write cache {cache_path}: {e}")
            
        return df

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
'''

with open(f'{base}/src/data_fetcher.py', 'w') as f:
    f.write(data_fetcher_content)

print("data_fetcher.py updated with safe name resolution.")
