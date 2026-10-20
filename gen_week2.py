import os

base = os.path.expanduser('~/crypto-screener-v2')

# 1. src/features/sweep.py
sweep_content = '''import pandas as pd
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
                        'likelihood_ratio': float(1.5 + wick_ratio)
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
                        'likelihood_ratio': float(1.5 + wick_ratio)
                    })

        return sweeps
'''

with open(f'{base}/src/features/sweep.py', 'w') as f:
    f.write(sweep_content)

# 2. src/features/order_flow.py
order_flow_content = '''import pandas as pd
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
'''

with open(f'{base}/src/features/order_flow.py', 'w') as f:
    f.write(order_flow_content)

# 3. src/scoring/families.py
families_content = '''from typing import List, Dict, Any

VALID_FAMILIES = {
    'Trend',
    'Momentum',
    'OrderFlow',
    'Volatility',
    'Structure',
    'Manipulation'
}

def reduce_family_signals(signals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Orthogonal Family Reduction:
    Groups signals by feature family and takes the strongest signal (highest likelihood ratio)
    within each family to avoid double-counting correlated indicators.
    """
    reduced: Dict[str, Dict[str, Any]] = {}
    for sig in signals:
        fam = sig.get('family', 'Structure')
        lr = sig.get('likelihood_ratio', 1.0)
        if fam not in reduced or lr > reduced[fam].get('likelihood_ratio', 1.0):
            reduced[fam] = sig
    return reduced
'''

with open(f'{base}/src/scoring/families.py', 'w') as f:
    f.write(families_content)

# 4. src/scoring/bayesian.py
bayesian_content = '''import math
from typing import List, Dict, Any
from src.scoring.families import reduce_family_signals

class BayesianCombiner:
    def __init__(self, prior_prob: float = 0.50):
        self.prior_prob = prior_prob
        self.prior_logit = self._prob_to_logit(prior_prob)

    def _prob_to_logit(self, p: float) -> float:
        p = max(min(p, 0.999), 0.001)
        return math.log(p / (1.0 - p))

    def _logit_to_prob(self, logit: float) -> float:
        return 1.0 / (1.0 + math.exp(-logit))

    def combine(self, signals: List[Dict[str, Any]]) -> float:
        """
        Combines signals across orthogonal families using log-likelihood ratios:
        logit(P) = logit(P_prior) + sum_family(ln(LR_family))
        Within each family, takes the single strongest signal (no double counting).
        """
        reduced = reduce_family_signals(signals)
        logit = self.prior_logit

        for fam, sig in reduced.items():
            lr = sig.get('likelihood_ratio', 1.0)
            if lr > 0:
                logit += math.log(lr)

        return self._logit_to_prob(logit)
'''

with open(f'{base}/src/scoring/bayesian.py', 'w') as f:
    f.write(bayesian_content)

# 5. src/scoring/calibration.py
calibration_content = '''import numpy as np
from sklearn.isotonic import IsotonicRegression
from typing import List, Tuple

class ProbabilityCalibrator:
    def __init__(self, brier_drift_threshold: float = 0.25):
        self.model = IsotonicRegression(out_of_bounds='clip', y_min=0.01, y_max=0.99)
        self.is_fitted = False
        self.brier_drift_threshold = brier_drift_threshold

    def fit(self, predicted_probs: np.ndarray, actual_outcomes: np.ndarray):
        p = np.asarray(predicted_probs, dtype=float)
        y = np.asarray(actual_outcomes, dtype=float)
        self.model.fit(p, y)
        self.is_fitted = True

    def predict(self, raw_probs: np.ndarray) -> np.ndarray:
        p = np.asarray(raw_probs, dtype=float)
        if not self.is_fitted:
            return p
        return self.model.predict(p)

    @staticmethod
    def calculate_brier_score(predicted_probs: np.ndarray, actual_outcomes: np.ndarray) -> float:
        p = np.asarray(predicted_probs, dtype=float)
        y = np.asarray(actual_outcomes, dtype=float)
        return float(np.mean((p - y) ** 2))

    @staticmethod
    def calculate_log_loss(predicted_probs: np.ndarray, actual_outcomes: np.ndarray) -> float:
        p = np.clip(np.asarray(predicted_probs, dtype=float), 1e-6, 1.0 - 1e-6)
        y = np.asarray(actual_outcomes, dtype=float)
        return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))

    def check_calibration_drift(self, predicted_probs: np.ndarray, actual_outcomes: np.ndarray) -> Tuple[bool, float]:
        score = self.calculate_brier_score(predicted_probs, actual_outcomes)
        has_drift = score > self.brier_drift_threshold
        return has_drift, score
'''

with open(f'{base}/src/scoring/calibration.py', 'w') as f:
    f.write(calibration_content)

# 6. tests/test_features_and_scoring.py
test_scoring_content = '''import pytest
import numpy as np
import pandas as pd
from src.features.sweep import LiquiditySweepDetector
from src.features.order_flow import OrderFlowAnalyzer
from src.scoring.families import reduce_family_signals
from src.scoring.bayesian import BayesianCombiner
from src.scoring.calibration import ProbabilityCalibrator

def test_liquidity_sweep_detector():
    # Construct synthetic candles with a deliberate bearish sweep on candle 11
    highs = [100.0] * 10 + [108.0]
    lows = [95.0] * 10 + [96.0]
    opens = [96.0] * 10 + [97.0]
    closes = [98.0] * 10 + [97.5]  # high penetrated 100, closed at 97.5 (wick = 108 - 97.5 = 10.5 out of 12)
    timestamps = [1710000000000 + i * 900000 for i in range(11)]

    df = pd.DataFrame({
        'timestamp': timestamps,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': [100.0] * 11
    })

    detector = LiquiditySweepDetector(swing_window=5, wick_ratio_threshold=0.4)
    sweeps = detector.detect(df)
    assert len(sweeps) >= 1
    assert sweeps[0]['signal_type'] == 'Liquidity_Sweep_Bearish'
    assert sweeps[0]['direction'] == 'SHORT'
    assert sweeps[0]['family'] == 'Manipulation'

def test_orthogonal_family_reduction_prevents_inflation():
    combiner = BayesianCombiner(prior_prob=0.50)

    # 1 signal in Trend family
    sig1 = {'family': 'Trend', 'likelihood_ratio': 1.5}
    p1 = combiner.combine([sig1])
    assert p1 > 0.50

    # Add a SECOND correlated signal in the SAME Trend family with smaller LR
    sig2 = {'family': 'Trend', 'likelihood_ratio': 1.2}
    p2 = combiner.combine([sig1, sig2])
    # Must NOT inflate probability because family takes max(1.5, 1.2) = 1.5
    assert p2 == p1

    # Add an orthogonal signal from OrderFlow family -> must legitimately update probability
    sig_ortho = {'family': 'OrderFlow', 'likelihood_ratio': 1.6}
    p3 = combiner.combine([sig1, sig_ortho])
    assert p3 > p1

def test_calibration_and_brier_score():
    calibrator = ProbabilityCalibrator(brier_drift_threshold=0.25)
    np.random.seed(42)
    # 200 synthetic predictions and ground truth
    raw_p = np.random.uniform(0.1, 0.9, 200)
    outcomes = (raw_p + np.random.normal(0, 0.1, 200) > 0.5).astype(int)

    calibrator.fit(raw_p, outcomes)
    calibrated_p = calibrator.predict(raw_p)

    brier = calibrator.calculate_brier_score(calibrated_p, outcomes)
    log_loss = calibrator.calculate_log_loss(calibrated_p, outcomes)

    assert brier < 0.25
    assert log_loss > 0.0
    has_drift, _ = calibrator.check_calibration_drift(calibrated_p, outcomes)
    assert has_drift is False
'''

with open(f'{base}/tests/test_features_and_scoring.py', 'w') as f:
    f.write(test_scoring_content)

print("Week 2 features and scoring generated successfully.")
