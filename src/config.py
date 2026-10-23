import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()

_PLACEHOLDERS = ('your_', 'your-', 'changeme', 'placeholder', 'xxx')


def _clean(value: str) -> str:
    """Treat unset or placeholder credentials as absent.

    `.env` ships with API_KEY=your_api_key_here. If that reaches ccxt it signs the
    request and Binance rejects even PUBLIC market data with -2008 Invalid Api-Key ID.
    """
    v = (value or '').strip()
    return '' if any(v.lower().startswith(p) for p in _PLACEHOLDERS) else v


@dataclass(frozen=True)
class SystemConfig:
    exchange: str = os.getenv('EXCHANGE', 'binance')
    api_key: str = _clean(os.getenv('API_KEY', ''))
    api_secret: str = _clean(os.getenv('API_SECRET', ''))
    
    # Model Fallback Chain: DeepSeek -> Gemini -> Ollama
    deepseek_api_key: str = os.getenv('DEEPSEEK_API_KEY', '')
    gemini_api_key: str = os.getenv('GEMINI_API_KEY', '')
    ollama_host: str = os.getenv('OLLAMA_HOST', 'http://localhost:11434')
    
    # Storage & Logging
    db_path: str = os.getenv('DB_PATH', 'data/signals.db')
    log_path: str = os.getenv('LOG_PATH', 'logs/system.log')
    cache_dir: str = os.getenv('CACHE_DIR', 'data/cache')
    
    # Trading & Market Parameters
    default_symbol: str = 'BTC/USDT'
    timeframe: str = os.getenv('TIMEFRAME', '1h')
    TIMEFRAME: str = os.getenv('TIMEFRAME', '1h')
    MACRO_TIMEFRAME: str = os.getenv('MACRO_TIMEFRAME', '4h')
    timeframes: List[str] = field(default_factory=lambda: ['15m', '1h', '4h'])
    latency_budget_ms: int = 500  # max acceptable delta between event_time and known_at_time
    
    # Hard Limits per DOCTRINE.md
    max_capacity_pct: float = float(os.getenv('MAX_CAPACITY_PCT', 0.01))  # Rule 7: <=1% daily volume
    daily_floor_limit: float = float(os.getenv('DAILY_DRAWDOWN_LIMIT', 0.02))  # Rule 11: 2% daily floor
    reconciliation_interval_s: int = 60  # Rule 15: Reconcile every 60s
    max_mid_distance_pct: float = 0.005  # Rule 13: Reject orders >0.5% from mid

    # Dynamic Position Sizing & Trailing Parameters
    risk_per_trade_percent: float = float(os.getenv('RISK_PER_TRADE_PERCENT', 0.01))
    atr_multiplier_for_stop: float = float(os.getenv('ATR_MULTIPLIER_FOR_STOP', 1.5))
    atr_multiplier_for_trail: float = float(os.getenv('ATR_MULTIPLIER_FOR_TRAIL', 3.0))
    account_balance: float = float(os.getenv('ACCOUNT_BALANCE', 100000.0))
    ACCOUNT_BALANCE_USD: float = float(os.getenv('ACCOUNT_BALANCE_USD', 100.0))

    RISK_PER_TRADE_PERCENT: float = float(os.getenv('RISK_PER_TRADE_PERCENT', 0.01))
    ATR_MULTIPLIER_FOR_STOP: float = float(os.getenv('ATR_MULTIPLIER_FOR_STOP', 1.5))
    ATR_MULTIPLIER_FOR_TRAIL: float = float(os.getenv('ATR_MULTIPLIER_FOR_TRAIL', 3.0))

    # Real-World Execution Friction & Fee Constants
    maker_fee_pct: float = float(os.getenv('MAKER_FEE_PCT', 0.0002))
    taker_fee_pct: float = float(os.getenv('TAKER_FEE_PCT', 0.0005))
    slippage_pct: float = float(os.getenv('SLIPPAGE_PCT', 0.0005))
    funding_rate_buffer: float = float(os.getenv('FUNDING_RATE_BUFFER', 0.0001))

    MAKER_FEE_PCT: float = float(os.getenv('MAKER_FEE_PCT', 0.0002))
    TAKER_FEE_PCT: float = float(os.getenv('TAKER_FEE_PCT', 0.0005))
    SLIPPAGE_PCT: float = float(os.getenv('SLIPPAGE_PCT', 0.0005))
    FUNDING_RATE_BUFFER: float = float(os.getenv('FUNDING_RATE_BUFFER', 0.0001))

config = SystemConfig()


# Hybrid Multi-Timeframe Architecture exports
TIMEFRAME = os.getenv('TIMEFRAME', '1h')
MACRO_TIMEFRAME = os.getenv('MACRO_TIMEFRAME', '4h')

# Dynamic Risk & Trailing Parameters
RISK_PER_TRADE_PERCENT = float(os.getenv('RISK_PER_TRADE_PERCENT', 0.01))
ATR_MULTIPLIER_FOR_STOP = float(os.getenv('ATR_MULTIPLIER_FOR_STOP', 1.5))
ATR_MULTIPLIER_FOR_TRAIL = float(os.getenv('ATR_MULTIPLIER_FOR_TRAIL', 3.0))
ACCOUNT_BALANCE_USD = float(os.getenv('ACCOUNT_BALANCE_USD', 100.0))

# Real-World Execution Friction & Fee Constants
MAKER_FEE_PCT = float(os.getenv('MAKER_FEE_PCT', 0.0002))
TAKER_FEE_PCT = float(os.getenv('TAKER_FEE_PCT', 0.0005))
SLIPPAGE_PCT = float(os.getenv('SLIPPAGE_PCT', 0.0005))
FUNDING_RATE_BUFFER = float(os.getenv('FUNDING_RATE_BUFFER', 0.0001))

