"""
Unit Tests for Backtest Metrics & Statistical Rigor
File: tests/test_backtest_metrics.py
"""

import numpy as np
import pandas as pd
import pytest

from src.backtest.metrics import (
    calculate_bootstrap_confidence,
    calculate_expectancy,
    calculate_sharpe_ratio,
)


class TestBacktestMetrics:

    def test_bootstrap_confidence_synthetic_trades(self):
        """
        Resamples trade returns 1000 times and validates 5th and 95th percentiles
        for Sharpe ratio and Win Rate.
        """
        np.random.seed(42)
        # 100 trades with positive expected return (60% win rate)
        wins = np.random.uniform(0.01, 0.05, size=60)
        losses = np.random.uniform(-0.04, -0.01, size=40)
        returns = np.concatenate([wins, losses])
        np.random.shuffle(returns)

        res = calculate_bootstrap_confidence(returns, n_bootstraps=1000, random_seed=42)

        assert "sharpe_ci" in res
        assert "win_rate_ci" in res
        assert "sharpe_p5" in res
        assert "sharpe_p95" in res
        assert "win_rate_p5" in res
        assert "win_rate_p95" in res

        # 5th percentile must be <= 95th percentile
        assert res["sharpe_ci"][0] <= res["sharpe_ci"][1]
        assert res["win_rate_ci"][0] <= res["win_rate_ci"][1]

        # Win rate should be close to 60%
        assert 45.0 <= res["win_rate_ci"][0] <= 65.0
        assert 55.0 <= res["win_rate_ci"][1] <= 75.0

        # Sharpe ratio percentiles should be positive for profitable distribution
        assert res["sharpe_ci"][1] > 0.0

    def test_bootstrap_confidence_dict_inputs(self):
        """
        Validates calculate_bootstrap_confidence with trade dictionaries.
        """
        trades = [
            {"return": 0.02, "realized_pnl": 50.0},
            {"return": -0.01, "realized_pnl": -25.0},
            {"return": 0.03, "realized_pnl": 75.0},
            {"return": 0.01, "realized_pnl": 30.0},
            {"return": -0.02, "realized_pnl": -40.0},
        ]
        res = calculate_bootstrap_confidence(trades, n_bootstraps=500, random_seed=123)
        assert res["n_samples"] == 5
        assert res["win_rate_ci"][0] <= res["win_rate_ci"][1]
        assert 0.0 <= res["win_rate_ci"][0] <= 100.0
        assert 0.0 <= res["win_rate_ci"][1] <= 100.0

    def test_bootstrap_confidence_empty_guards(self):
        """
        Ensures safe 0.0 return when empty list or invalid trades are provided.
        """
        res = calculate_bootstrap_confidence([], n_bootstraps=100)
        assert res["n_samples"] == 0
        assert res["sharpe_ci"] == (0.0, 0.0)
        assert res["win_rate_ci"] == (0.0, 0.0)
        assert res["sharpe_p5"] == 0.0
        assert res["win_rate_p95"] == 0.0

    def test_calculate_expectancy_positive(self):
        """
        Expectancy = (Win_Rate * Avg_Win_USD) - (Loss_Rate * Avg_Loss_USD) - Avg_Costs_USD
        """
        # Win Rate 60%, Avg Win $100, Loss Rate 40%, Avg Loss $50, Cost $5
        # (0.60 * 100) - (0.40 * 50) - 5 = 60 - 20 - 5 = +35.0
        exp = calculate_expectancy(
            win_rate=60.0,  # percentage
            avg_win_usd=100.0,
            loss_rate=40.0,
            avg_loss_usd=50.0,
            avg_costs_usd=5.0,
        )
        assert pytest.approx(exp, 0.01) == 35.0

        # Fractional input
        exp_frac = calculate_expectancy(
            win_rate=0.60,
            avg_win_usd=100.0,
            avg_loss_usd=50.0,
            avg_costs_usd=5.0,
        )
        assert pytest.approx(exp_frac, 0.01) == 35.0

    def test_calculate_expectancy_negative(self):
        """
        Strategy with negative net expectation.
        """
        # Win Rate 30%, Avg Win $40, Loss Rate 70%, Avg Loss $80, Cost $4
        # (0.30 * 40) - (0.70 * 80) - 4 = 12 - 56 - 4 = -48.0
        exp = calculate_expectancy(
            win_rate=30.0,
            avg_win_usd=40.0,
            loss_rate=70.0,
            avg_loss_usd=80.0,
            avg_costs_usd=4.0,
        )
        assert pytest.approx(exp, 0.01) == -48.0
        assert exp < 0.0

    def test_dashboard_calibration_tab_structure(self):
        """
        Validates that src/dashboard.py includes the Calibration Tab and
        chart elements.
        """
        from pathlib import Path
        dash_file = Path(__file__).resolve().parent.parent / "src" / "dashboard.py"
        assert dash_file.exists()
        content = dash_file.read_text(encoding="utf-8")

        # Tab registration
        assert '"🎯 Calibration"' in content or '"Calibration"' in content
        # Confluence Score vs Outcome mapping
        assert "Confluence Score" in content
        assert "Outcome" in content
        assert "Score ≥ 70 Win Rate" in content
