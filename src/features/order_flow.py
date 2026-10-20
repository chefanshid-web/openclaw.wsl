import pandas as pd
import numpy as np
from typing import Dict, Any, List

class OrderFlowAnalyzer:
    def __init__(self, vol_ma_period: int = 20):
        self.vol_ma_period = vol_ma_period

    def calculate_volume_ratio(self, df: pd.DataFrame) -> pd.Series:
        vol = df['volume']
        vol_ma = vol.rolling(window=self.vol_ma_period).mean()
        return vol / vol_ma.replace(0, np.nan)

    def calculate_obi(self, order_book: Dict[str, Any]) -> float:
        """
        Order Book Imbalance: (bid_vol - ask_vol) / (bid_vol + ask_vol)
        """
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        bid_vol = sum(b[1] for b in bids)
        ask_vol = sum(a[1] for a in asks)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def estimate_cvd_divergence(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Approximate candle-level delta: signed volume by candle body direction.
        Checks for price/delta divergence over recent window.
        """
        if len(df) < 10:
            return []

        signals = []
        body = df['close'] - df['open']
        candle_range = (df['high'] - df['low']).replace(0, 1e-8)
        signed_ratio = np.clip(body / candle_range, -1.0, 1.0)
        approx_delta = df['volume'] * signed_ratio
        cum_delta = approx_delta.cumsum()

        # Check last candle vs 5-candle trend
        recent_price_diff = df['close'].iloc[-1] - df['close'].iloc[-5]
        recent_delta_diff = cum_delta.iloc[-1] - cum_delta.iloc[-5]

        # Bullish CVD divergence: Price lower or flat, Delta higher
        if recent_price_diff <= 0 and recent_delta_diff > 0:
            signals.append({
                'timestamp': int(df['timestamp'].iloc[-1]),
                'family': 'OrderFlow',
                'signal_type': 'CVD_Bullish_Divergence',
                'direction': 'LONG',
                'raw_score': 1.5,
                'likelihood_ratio': 1.6
            })
        # Bearish CVD divergence: Price higher or flat, Delta lower
        elif recent_price_diff >= 0 and recent_delta_diff < 0:
            signals.append({
                'timestamp': int(df['timestamp'].iloc[-1]),
                'family': 'OrderFlow',
                'signal_type': 'CVD_Bearish_Divergence',
                'direction': 'SHORT',
                'raw_score': 1.5,
                'likelihood_ratio': 1.6
            })

        return signals
