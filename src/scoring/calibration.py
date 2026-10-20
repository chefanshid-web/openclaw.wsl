import numpy as np
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
