import math
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
