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
        Validates that src/dashboard.py includes the Expectancy & Calibration Tab,
        Account Balance ($100), Net Expectancy, and chart elements.
        """
        from pathlib import Path
        dash_file = Path(__file__).resolve().parent.parent / "src" / "dashboard.py"
        assert dash_file.exists()
        content = dash_file.read_text(encoding="utf-8")

        # Tab registration
        assert any(k in content for k in ['"Expectancy & Calibration"', '"🎯 Expectancy & Calibration"', '"Calibration"'])
        # Confluence Score vs Outcome mapping
        assert "Confluence Score" in content
        assert "Outcome" in content
        assert "Score ≥ 70 Win Rate" in content
        assert "Account Balance" in content
        assert "Net Expectancy" in content
        assert "st.scatter_chart" in content

    def test_starting_capital_and_position_sizing_100usd(self):
        """
        Task 1: Validates starting capital of $100 in src/config.py
        and that calculate_position_size uses 1% risk ($1.00 per trade).
        """
        from src import config
        from src.paper_executor import calculate_position_size

        assert hasattr(config, "ACCOUNT_BALANCE_USD")
        assert config.ACCOUNT_BALANCE_USD == 100.0

        # When account_balance is None, defaults to config.ACCOUNT_BALANCE_USD ($100.0)
        # Entry = $100, Stop Loss = $90 -> Stop Distance = $10
        # 1% risk on $100 balance = $1.00 risk budget
        # Raw units = 1.00 / 10 = 0.10, fee adjusted = 0.10 * 0.98 = 0.098
        # Total value = 0.098 * 100 = $9.80 (> $5.00 min order)
        pos = calculate_position_size(
            entry_price=100.0,
            stop_loss=90.0,
        )
        assert pos.risk_amount == 1.0  # 1% of $100.0
        assert pos.status == "VALID"
        assert pos.is_rejected is False
        assert pytest.approx(pos.units, 0.001) == 0.098
        assert pytest.approx(pos.total_value, 0.01) == 9.80

    def test_rejected_min_size_exchange_gate(self):
        """
        Task 1: If calculated position size is less than exchange minimum order size ($5 on Binance),
        the trade is rejected and logged as REJECTED_MIN_SIZE.
        """
        from src.paper_executor import calculate_position_size

        # Entry = $10.0, Stop = $8.0 -> Stop Distance = $2.0
        # Risk Budget = 1.0 (on $100 balance)
        # Raw units = 1.0 / 2.0 = 0.5, fee adjusted = 0.5 * 0.98 = 0.49
        # Total value = 0.49 * 10.0 = $4.90 (< $5.00 Binance min order)
        pos = calculate_position_size(
            account_balance=100.0,
            entry_price=10.0,
            stop_loss=8.0,
            min_order_usd=5.0,
        )
        assert pos.status == "REJECTED_MIN_SIZE"
        assert pos.is_rejected is True
        assert "below exchange minimum order size" in pos.rejection_reason
        assert pos.total_value < 5.0
