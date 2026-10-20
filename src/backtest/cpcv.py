import math
import numpy as np
import scipy.stats as stats
from typing import List, Tuple, Dict, Any

class CombinatorialPurgedCrossValidation:
    def __init__(self, n_splits: int = 5, n_test_splits: int = 2, purge_window: int = 5, embargo_pct: float = 0.01):
        self.n_splits = n_splits
        self.n_test_splits = n_test_splits
        self.purge_window = purge_window
        self.embargo_pct = embargo_pct

    def generate_splits(self, n_samples: int) -> List[Tuple[np.ndarray, np.ndarray]]:
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
            
            embargo_size = int(n_samples * self.embargo_pct)
            test_set = set(test_indices)
            purged_train = []
            for idx in raw_train:
                if any(abs(idx - t_idx) <= self.purge_window for t_idx in test_set):
                    continue
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
        return float(np.mean(excess) / std * np.sqrt(252 * 24 * 4))

    @staticmethod
    def calculate_deflated_sharpe(sharpe_oos: float,
                                  n_trials: int,
                                  var_trials: float,
                                  n_samples: int,
                                  skew: float = -0.5,
                                  kurtosis: float = 4.0) -> float:
        if n_trials <= 1:
            return float(stats.norm.cdf(sharpe_oos * np.sqrt(n_samples)))
        
        euler_mascheroni = 0.5772156649
        z_n = (1.0 - euler_mascheroni) * stats.norm.ppf(1.0 - 1.0 / n_trials) + euler_mascheroni * stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
        e_max_sharpe = math.sqrt(var_trials) * z_n
        
        denom = math.sqrt(1.0 - skew * sharpe_oos + ((kurtosis - 1.0) / 4.0) * (sharpe_oos ** 2))
        if denom <= 0:
            denom = 1.0
        t_stat = (sharpe_oos - e_max_sharpe) * math.sqrt(n_samples - 1) / denom
        return float(stats.norm.cdf(t_stat))

    @staticmethod
    def verify_plateau(param_returns: Dict[float, float]) -> bool:
        if 1.0 not in param_returns:
            return False
        center_perf = param_returns[1.0]
        if center_perf <= 0:
            return False
            
        for multiplier in [0.8, 1.2]:
            if multiplier in param_returns:
                perf = param_returns[multiplier]
                if perf < 0.60 * center_perf:
                    return False
        return True
