import pandas as pd
import numpy as np
from typing import List, Dict, Any

class LiquiditySweepDetector:
    def __init__(self, swing_window: int = 10, wick_ratio_threshold: float = 0.4):
        self.swing_window = swing_window
        self.wick_ratio_threshold = wick_ratio_threshold

    def detect(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Detects liquidity sweeps on OHLCV dataframe.
        - Bearish sweep: High > prior swing high, but close < prior swing high and upper wick is prominent.
        - Bullish sweep: Low < prior swing low, but close > prior swing low and lower wick is prominent.
        """
        if len(df) < self.swing_window + 2:
            return []

        sweeps = []
        highs = df['high'].values
        lows = df['low'].values
        opens = df['open'].values
        closes = df['close'].values
        timestamps = df['timestamp'].values

        for i in range(self.swing_window, len(df)):
            prior_high = np.max(highs[i - self.swing_window:i])
            prior_low = np.min(lows[i - self.swing_window:i])
            
            curr_h = highs[i]
            curr_l = lows[i]
            curr_o = opens[i]
            curr_c = closes[i]
            candle_range = curr_h - curr_l
            
            if candle_range <= 0:
                continue

            # Check Bearish Sweep (Buy-side liquidity swept)
            if curr_h > prior_high and curr_c < prior_high:
                upper_wick = curr_h - max(curr_o, curr_c)
                wick_ratio = upper_wick / candle_range
                if wick_ratio >= self.wick_ratio_threshold:
                    sweeps.append({
                        'timestamp': int(timestamps[i]),
                        'family': 'Manipulation',
                        'signal_type': 'Liquidity_Sweep_Bearish',
                        'direction': 'SHORT',
                        'level': float(prior_high),
                        'penetration': float(curr_h - prior_high),
                        'raw_score': float(1.0 + wick_ratio),
                        'likelihood_ratio': 1.0,  # NOT an edge. Must be estimated from data (edge.md)
                    })

            # Check Bullish Sweep (Sell-side liquidity swept)
            if curr_l < prior_low and curr_c > prior_low:
                lower_wick = min(curr_o, curr_c) - curr_l
                wick_ratio = lower_wick / candle_range
                if wick_ratio >= self.wick_ratio_threshold:
                    sweeps.append({
                        'timestamp': int(timestamps[i]),
                        'family': 'Manipulation',
                        'signal_type': 'Liquidity_Sweep_Bullish',
                        'direction': 'LONG',
                        'level': float(prior_low),
                        'penetration': float(prior_low - curr_l),
                        'raw_score': float(1.0 + wick_ratio),
                        'likelihood_ratio': 1.0,  # NOT an edge. Must be estimated from data (edge.md)
                    })

        return sweeps
