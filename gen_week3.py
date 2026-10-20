import os

base = os.path.expanduser('~/crypto-screener-v2')

# 1. src/costs/model.py
cost_content = '''import math
from dataclasses import dataclass
from typing import Dict, Any, Tuple

@dataclass
class TransactionCostBreakdown:
    fee_bps: float
    half_spread_bps: float
    slippage_bps: float
    market_impact_bps: float
    funding_bps: float
    total_cost_bps: float
    is_eligible: bool
    rejection_reason: str = ""

class CostModel:
    def __init__(self,
                 taker_fee_bps: float = 4.0,     # 0.04%
                 maker_fee_bps: float = 2.0,     # 0.02%
                 half_spread_bps: float = 1.0,   # 0.01%
                 slippage_bps: float = 1.5,      # base slippage
                 impact_constant_y: float = 0.5):
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.half_spread_bps = half_spread_bps
        self.slippage_bps = slippage_bps
        self.impact_constant_y = impact_constant_y

    def calculate_market_impact_bps(self, order_size_usd: float, daily_volume_usd: float, daily_volatility_pct: float) -> float:
        """
        Square-root law for market impact:
        I = Y * sigma * sqrt(Q / V)
        Returns impact in basis points (bps).
        """
        if daily_volume_usd <= 0 or order_size_usd <= 0:
            return 0.0
        
        ratio = order_size_usd / daily_volume_usd
        # Daily volatility converted to bps (1% = 100 bps)
        sigma_bps = daily_volatility_pct * 10000.0
        impact_bps = self.impact_constant_y * sigma_bps * math.sqrt(ratio)
        return float(impact_bps)

    def evaluate_cost(self,
                      expected_move_bps: float,
                      order_size_usd: float,
                      daily_volume_usd: float,
                      daily_volatility_pct: float = 0.03,
                      is_taker: bool = True,
                      holding_hours: float = 4.0,
                      funding_rate_8h: float = 0.0001) -> TransactionCostBreakdown:
        """
        Calculates all frictional costs and evaluates the eligibility gate:
        expected_move > 2 * total_cost
        """
        fee_bps = self.taker_fee_bps if is_taker else self.maker_fee_bps
        impact_bps = self.calculate_market_impact_bps(order_size_usd, daily_volume_usd, daily_volatility_pct)
        funding_bps = abs(funding_rate_8h) * (holding_hours / 8.0) * 10000.0
        
        # Round-trip cost = 2 * (fee + spread + slippage + impact) + funding
        total_cost_bps = 2.0 * (fee_bps + self.half_spread_bps + self.slippage_bps + impact_bps) + funding_bps
        
        # Eligibility Gate: Rule - expected_move must exceed 2x total cost
        required_edge_bps = 2.0 * total_cost_bps
        is_eligible = expected_move_bps > required_edge_bps
        rejection_reason = "" if is_eligible else f"Expected move ({expected_move_bps:.1f} bps) < 2x cost ({required_edge_bps:.1f} bps)"
        
        return TransactionCostBreakdown(
            fee_bps=fee_bps,
            half_spread_bps=self.half_spread_bps,
            slippage_bps=self.slippage_bps,
            market_impact_bps=impact_bps,
            funding_bps=funding_bps,
            total_cost_bps=total_cost_bps,
            is_eligible=is_eligible,
            rejection_reason=rejection_reason
        )
'''

with open(f'{base}/src/costs/model.py', 'w') as f:
    f.write(cost_content)

# 2. src/backtest/cpcv.py
cpcv_content = '''import numpy as np
import scipy.stats as stats
from typing import List, Tuple, Dict, Any

class CombinatorialPurgedCrossValidation:
    def __init__(self, n_splits: int = 5, n_test_splits: int = 2, purge_window: int = 5, embargo_pct: float = 0.01):
        self.n_splits = n_splits
        self.n_test_splits = n_test_splits
        self.purge_window = purge_window
        self.embargo_pct = embargo_pct

    def generate_splits(self, n_samples: int) -> List[Tuple[np.ndarray, np.ndarray]]:
        """
        Generates purged and embargoed train/test partition indices.
        """
        indices = np.arange(n_samples)
        fold_size = n_samples // self.n_splits
        folds = [indices[i * fold_size : (i + 1) * fold_size if i < self.n_splits - 1 else n_samples]
                 for i in range(self.n_splits)]
        
        splits = []
        import itertools
        for test_fold_ids in itertools.combinations(range(self.n_splits), self.n_test_splits):
            test_indices = np.concatenate([folds[i] for i in test_fold_ids])
            train_fold_ids = [i for i in range(self.n_splits) if i not in test_fold_ids]
            raw_train = np.concatenate([folds[i] for i in train_fold_ids])
            
            # Apply purging & embargoing
            embargo_size = int(n_samples * self.embargo_pct)
            test_set = set(test_indices)
            purged_train = []
            for idx in raw_train:
                # Purge if near test set boundaries
                if any(abs(idx - t_idx) <= self.purge_window for t_idx in test_set):
                    continue
                # Embargo trailing indices immediately following test folds
                if any(0 <= idx - t_max <= embargo_size for t_max in [np.max(folds[i]) for i in test_fold_ids]):
                    continue
                purged_train.append(idx)
                
            splits.append((np.array(purged_train), test_indices))
        return splits

    @staticmethod
    def calculate_sharpe(returns: np.ndarray, risk_free_rate: float = 0.0) -> float:
        ret = np.asarray(returns, dtype=float)
        if len(ret) < 2:
            return 0.0
        excess = ret - risk_free_rate
        std = np.std(excess, ddof=1)
        if std == 0:
            return 0.0
        return float(np.mean(excess) / std * np.sqrt(252 * 24 * 4)) # Annualized for 15m intervals

    @staticmethod
    def calculate_deflated_sharpe(sharpe_oos: float,
                                  n_trials: int,
                                  var_trials: float,
                                  n_samples: int,
                                  skew: float = -0.5,
                                  kurtosis: float = 4.0) -> float:
        """
        Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014)
        Adjusts OOS Sharpe for selection bias across multiple trial iterations.
        """
        if n_trials <= 1:
            return float(stats.norm.cdf(sharpe_oos * np.sqrt(n_samples)))
        
        # Expected maximum Sharpe under null hypothesis
        euler_mascheroni = 0.5772156649
        z_n = (1.0 - euler_mascheroni) * stats.norm.ppf(1.0 - 1.0 / n_trials) + euler_mascheroni * stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
        e_max_sharpe = math.sqrt(var_trials) * z_n
        
        # Standard deviation of Sharpe under non-normal returns
        denom = math.sqrt(1.0 - skew * sharpe_oos + ((kurtosis - 1.0) / 4.0) * (sharpe_oos ** 2))
        if denom <= 0:
            denom = 1.0
        t_stat = (sharpe_oos - e_max_sharpe) * math.sqrt(n_samples - 1) / denom
        return float(stats.norm.cdf(t_stat))

    @staticmethod
    def verify_plateau(param_returns: Dict[float, float]) -> bool:
        """
        Plateau check: Verifies performance does not collapse when perturbed +/-20%.
        If Sharpe drops by more than 40% at +/-20% neighbors, it is a fragile spike, not a plateau.
        """
        if 1.0 not in param_returns:
            return False
        center_perf = param_returns[1.0]
        if center_perf <= 0:
            return False
            
        for multiplier in [0.8, 1.2]:
            if multiplier in param_returns:
                perf = param_returns[multiplier]
                if perf < 0.60 * center_perf:  # Edge decay / spike rejection
                    return False
        return True
'''

with open(f'{base}/src/backtest/cpcv.py', 'w') as f:
    f.write(cpcv_content)

# 3. tests/test_cost_and_backtest.py
test_content = '''import pytest
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
'''

with open(f'{base}/tests/test_cost_and_backtest.py', 'w') as f:
    f.write(test_content)

print("Week 3 cost model, CPCV, and tests generated.")
