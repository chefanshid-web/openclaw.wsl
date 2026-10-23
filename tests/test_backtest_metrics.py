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

    def test_fee_and_friction_constants(self):
        """
        Task 1: Verify fee and friction constants in src/config.py
        - MAKER_FEE_PCT = 0.0002 (0.02%)
        - TAKER_FEE_PCT = 0.0005 (0.05%)
        - SLIPPAGE_PCT = 0.0005 (0.05% average)
        - FUNDING_RATE_BUFFER = 0.0001 (0.01% per 8 hours)
        """
        from src import config

        assert hasattr(config, "MAKER_FEE_PCT")
        assert config.MAKER_FEE_PCT == 0.0002
        assert hasattr(config, "TAKER_FEE_PCT")
        assert config.TAKER_FEE_PCT == 0.0005
        assert hasattr(config, "SLIPPAGE_PCT")
        assert config.SLIPPAGE_PCT == 0.0005
        assert hasattr(config, "FUNDING_RATE_BUFFER")
        assert config.FUNDING_RATE_BUFFER == 0.0001

    def test_honest_fill_entry_slippage_and_fee_deduction(self):
        """
        Task 2 & 3: In fill_triggered_plan():
        - Calculate entry_slippage = random.uniform(0, 0.001)
        - fill_price = entry_price * (1 + entry_slippage) for LONG, (1 - entry_slippage) for SHORT
        - entry_fee = (fill_price * quantity) * MAKER_FEE_PCT
        - Deduct entry_fee from simulated balance
        - PAPER_FILL signal log includes gross_pnl, entry_fee, exit_fee, slippage_cost, net_pnl
        """
        from unittest.mock import MagicMock
        from datetime import datetime, timezone
        from src.paper_executor import PaperExecutor

        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"plan_metadata": {}}
        ]
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"id": "plan-honest-1", "status": "FILLED"}
        ]

        executor = PaperExecutor(db_manager=mock_db)
        initial_balance = executor.simulated_balance

        plan = {
            "id": "plan-honest-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "atr": 1000.0,
            "stop_loss": 48500.0,
            "take_profit": 53000.0,
            "plan_metadata": {
                "triggered_at": datetime.now(timezone.utc).isoformat(),
                "atr": 1000.0,
            }
        }

        res = executor.fill_triggered_plan(plan)
        assert res is True

        # Check update payload
        update_call = mock_table.update.call_args[0][0]
        meta = update_call["plan_metadata"]

        assert "fill_price" in meta
        # For LONG, fill_price >= entry_price
        assert meta["fill_price"] >= 50000.0
        assert meta["fill_price"] <= 50000.0 * 1.0015

        assert "entry_fee" in meta
        assert meta["entry_fee"] > 0.0
        # Simulated balance deducted by entry_fee
        assert executor.simulated_balance == initial_balance - meta["entry_fee"]

        # Check PAPER_FILL signal audit log
        mock_db.log_signal.assert_called_once()
        sig_call = mock_db.log_signal.call_args[1]
        assert sig_call["signal_type"] == "PAPER_FILL"
        sig_meta = sig_call["metadata"]

        assert "gross_pnl" in sig_meta
        assert sig_meta["gross_pnl"] == 0.0
        assert "entry_fee" in sig_meta
        assert sig_meta["entry_fee"] == meta["entry_fee"]
        assert "exit_fee" in sig_meta
        assert sig_meta["exit_fee"] == 0.0
        assert "slippage_cost" in sig_meta
        assert sig_meta["slippage_cost"] >= 0.0
        assert "net_pnl" in sig_meta
        assert sig_meta["net_pnl"] == -meta["entry_fee"]

    def test_honest_exit_slippage_fee_and_net_pnl(self):
        """
        Task 2 & 3: In close_trade():
        - exit_slippage in uniform(0, 0.001)
        - exit_price = stop_loss * (1 - exit_slippage) for LONG, (1 + exit_slippage) for SHORT
        - exit_fee = (exit_price * quantity) * TAKER_FEE_PCT
        - Deduct exit_fee from simulated balance
        - net_pnl = gross_pnl - entry_fee - exit_fee
        """
        from unittest.mock import MagicMock
        from datetime import datetime, timezone
        from src.paper_executor import PaperExecutor

        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value

        executor = PaperExecutor(db_manager=mock_db)
        starting_bal = executor.simulated_balance

        plan = {
            "id": "plan-exit-honest",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 53000.0,
            "position_size": 0.1,
            "plan_metadata": {
                "fill_price": 50020.0,
                "position_size": 0.1,
                "entry_fee": 1.00,
                "slippage_cost": 2.00,
            }
        }

        # Simulate that entry_fee was already deducted
        executor.simulated_balance -= 1.00

        # Close trade via stop loss
        res = executor.close_trade(plan, exit_reason="STOP_LOSS")
        assert res is True

        # Check PAPER_EXIT signal log
        exit_sig = mock_db.log_signal.call_args[1]
        assert exit_sig["signal_type"] == "PAPER_EXIT"
        exit_meta = exit_sig["metadata"]

        assert "gross_pnl" in exit_meta
        assert "entry_fee" in exit_meta
        assert "exit_fee" in exit_meta
        assert "slippage_cost" in exit_meta
        assert "net_pnl" in exit_meta

        # For LONG, exit price is <= 49000.0 (stop_loss * (1 - exit_slippage))
        assert exit_meta["exit_price"] <= 49000.0
        assert exit_meta["exit_fee"] > 0.0
        # Net PnL must equal gross_pnl - entry_fee - exit_fee
        expected_net = round(exit_meta["gross_pnl"] - exit_meta["entry_fee"] - exit_meta["exit_fee"], 4)
        assert pytest.approx(exit_meta["net_pnl"], 0.001) == expected_net

        # Check that simulated balance change across the trade is exactly net_pnl
        total_balance_change = executor.simulated_balance - starting_bal
        assert pytest.approx(total_balance_change, 0.001) == exit_meta["net_pnl"]

    def test_dashboard_pnl_breakdown(self):
        """
        Task 4: Validates that src/dashboard.py includes the Total PnL breakdown:
        Gross Profit, Fees Paid, Slippage Cost, and Net Profit.
        """
        from pathlib import Path
        dash_file = Path(__file__).resolve().parent.parent / "src" / "dashboard.py"
        assert dash_file.exists()
        content = dash_file.read_text(encoding="utf-8")

        assert "Gross Profit" in content
        assert "Fees Paid" in content
        assert "Slippage Cost" in content
        assert "Net Profit" in content
        assert "get_pnl_breakdown" in content
