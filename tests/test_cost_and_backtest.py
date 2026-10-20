import pytest
import numpy as np
from src.costs.model import CostModel
from src.backtest.cpcv import CombinatorialPurgedCrossValidation

def test_square_root_market_impact_and_eligibility():
    cost_model = CostModel(taker_fee_bps=4.0, half_spread_bps=1.0, slippage_bps=1.5)
    
    # Order: $50k on $10M daily volume with 3% daily volatility
    impact_bps = cost_model.calculate_market_impact_bps(
        order_size_usd=50_000.0,
        daily_volume_usd=10_000_000.0,
        daily_volatility_pct=0.03
    )
    assert impact_bps > 0.0
    assert impact_bps < 100.0  # reasonable impact

    # Test eligibility gate
    # Case A: expected move = 100 bps > 2x cost (~30 bps) -> Eligible
    res_a = cost_model.evaluate_cost(
        expected_move_bps=100.0,
        order_size_usd=50_000.0,
        daily_volume_usd=10_000_000.0
    )
    assert res_a.is_eligible is True

    # Case B: expected move = 10 bps < 2x cost -> Ineligible
    res_b = cost_model.evaluate_cost(
        expected_move_bps=10.0,
        order_size_usd=50_000.0,
        daily_volume_usd=10_000_000.0
    )
    assert res_b.is_eligible is False
    assert "Expected move" in res_b.rejection_reason

def test_cpcv_purging_and_deflated_sharpe():
    cpcv = CombinatorialPurgedCrossValidation(n_splits=5, n_test_splits=2, purge_window=3)
    splits = cpcv.generate_splits(n_samples=200)
    assert len(splits) == 10  # 5 choose 2 = 10
    
    for train_idx, test_idx in splits:
        # Purging ensures zero overlap between train and test
        assert len(set(train_idx).intersection(set(test_idx))) == 0
        assert len(train_idx) > 0
        assert len(test_idx) > 0

    # Test deflated Sharpe ratio
    dsr = cpcv.calculate_deflated_sharpe(
        sharpe_oos=1.8,
        n_trials=10,
        var_trials=0.15,
        n_samples=500
    )
    assert 0.0 <= dsr <= 1.0

def test_plateau_check_plateau_vs_spike():
    cpcv = CombinatorialPurgedCrossValidation()
    # Robust plateau: stable returns across +/-20%
    plateau_params = {0.8: 1.4, 1.0: 1.5, 1.2: 1.35}
    assert cpcv.verify_plateau(plateau_params) is True

    # Fragile overfitted spike: collapses at +/-20%
    spike_params = {0.8: 0.2, 1.0: 2.2, 1.2: 0.3}
    assert cpcv.verify_plateau(spike_params) is False
