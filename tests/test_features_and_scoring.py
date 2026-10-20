import pytest
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
