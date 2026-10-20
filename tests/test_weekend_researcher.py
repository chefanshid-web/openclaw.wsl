"""
Unit tests for src/weekend_researcher.py (Phase 4.5 Weekend Research Engine)
"""

import os
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from src.weekend_researcher import is_weekend, WeekendResearcher


class TestWeekendResearcher(unittest.TestCase):
    def test_is_weekend(self):
        """Verify is_weekend correctly identifies Saturdays (5) and Sundays (6)."""
        # Saturday
        sat = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(is_weekend(sat))

        # Sunday
        sun = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(is_weekend(sun))

        # Monday
        mon = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(is_weekend(mon))

        # Wednesday
        wed = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(is_weekend(wed))

    def test_time_gate_blocks_non_weekend_without_force(self):
        """Verify run_all aborts on weekdays if force is False."""
        wed = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        researcher = WeekendResearcher()
        success = researcher.run_all(force=False, as_of_date=wed)
        self.assertFalse(success)

    def test_research_scraper_appends_papers(self):
        """Verify research scraper creates/appends papers to RESEARCH_LOG.md."""
        test_log = "TEST_RESEARCH_LOG.md"
        if os.path.exists(test_log):
            os.remove(test_log)

        try:
            researcher = WeekendResearcher()
            with patch.object(researcher, "scrape_research_papers") as mock_scrape:
                mock_scrape.side_effect = lambda as_of_date=None: test_log
                res_path = researcher.scrape_research_papers()
                self.assertEqual(res_path, test_log)

            # Test actual method with custom log path
            sat = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
            with patch("src.weekend_researcher.open", unittest.mock.mock_open()) as mock_file:
                path = researcher.scrape_research_papers(as_of_date=sat)
                self.assertEqual(path, "RESEARCH_LOG.md")
                mock_file.assert_called()
        finally:
            if os.path.exists(test_log):
                os.remove(test_log)

    def test_graveyard_resurrection_recommendations(self):
        """Verify graveyard resurrection check parses graveyard and recommends candidates."""
        researcher = WeekendResearcher()
        sat = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        with patch("src.weekend_researcher.open", unittest.mock.mock_open(read_data="# Graveyard")) as mock_file:
            path = researcher.check_graveyard_resurrection(as_of_date=sat)
            self.assertEqual(path, "library_active.md")
            mock_file.assert_called()

    def test_generate_backtest_report_file(self):
        """Verify backtest report generation creates markdown report."""
        researcher = WeekendResearcher(reports_dir="reports")
        sat = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        details = {
            "total_trades": 150,
            "symbol_stats": {
                "BTC": {"trades": 50, "win_rate": 45.0, "mean_return": 0.25},
                "ETH": {"trades": 50, "win_rate": 42.0, "mean_return": 0.18},
                "SOL": {"trades": 50, "win_rate": 48.0, "mean_return": 0.32},
            },
        }

        report_file = researcher.generate_backtest_report(
            win_rate=45.0,
            sharpe=1.25,
            max_dd=12.5,
            details=details,
            as_of_date=sat,
        )
        self.assertTrue(os.path.exists(report_file))
        with open(report_file, "r") as f:
            content = f.read()
        self.assertIn("Weekend Quantitative Backtest Report", content)
        self.assertIn("BTC/USDT", content)
        self.assertIn("45.00%", content)

    def test_propose_strategy_records_proposed_action(self):
        """Verify Hunter Agent strategy proposal records PROPOSED action in ledger."""
        researcher = WeekendResearcher()
        with patch.object(researcher, "record_ledger") as mock_record:
            mock_record.return_value = {"action": "PROPOSED", "strategy_name": "Test Strat"}
            res = researcher.propose_strategy(
                strategy_name="H10: Order Flow Imbalance",
                hypothesis="Passive limit order clusters absorb market orders",
                domain="Microstructure",
                agent_name="Hunter",
            )
            mock_record.assert_called_once()
            call_kwargs = mock_record.call_args[1]
            self.assertEqual(call_kwargs["action"], "PROPOSED")
            self.assertEqual(call_kwargs["agent_name"], "Hunter")
            self.assertEqual(call_kwargs["strategy_name"], "H10: Order Flow Imbalance")

    def test_validate_strategy_candidate_pass_and_reject(self):
        """Verify Pydantic schema validation records REJECTED when invalid."""
        researcher = WeekendResearcher()

        # Valid candidate: profit_multiplier (3.0) >= stop_multiplier (1.5)
        valid_cand = {
            "strategy_name": "Valid Strategy",
            "direction": "LONG",
            "stop_multiplier": 1.5,
            "profit_multiplier": 3.0,
            "min_sharpe": 1.0,
        }
        passed, msg = researcher.validate_strategy_candidate(valid_cand)
        self.assertTrue(passed)

        # Invalid candidate: profit_multiplier (1.0) < stop_multiplier (2.0)
        invalid_cand = {
            "strategy_name": "Negative Expectancy Strat",
            "direction": "LONG",
            "stop_multiplier": 2.0,
            "profit_multiplier": 1.0,
        }
        with patch.object(researcher, "record_ledger") as mock_record:
            passed, msg = researcher.validate_strategy_candidate(invalid_cand, agent_name="Validator")
            self.assertFalse(passed)
            self.assertIn("Pydantic Validation Error", msg)
            mock_record.assert_called_once()
            call_kwargs = mock_record.call_args[1]
            self.assertEqual(call_kwargs["action"], "REJECTED")
            self.assertEqual(call_kwargs["agent_name"], "Validator")

    def test_evaluate_strategy_candidate_lifecycle(self):
        """Verify evaluate_strategy_candidate executes proposal, backtest, and rejection/approval."""
        researcher = WeekendResearcher()

        cand = {
            "strategy_name": "Candidate Alpha",
            "stop_multiplier": 1.5,
            "profit_multiplier": 3.0,
            "min_sharpe": 1.0,
            "max_drawdown_limit": 25.0,
        }

        # Sub-test 1: Fails CPCV Sharpe hurdle
        with patch.object(researcher, "backtest_strategy") as mock_bt:
            mock_bt.return_value = (40.0, 0.65, 18.0, {"total_trades": 100})
            with patch.object(researcher, "record_ledger") as mock_record:
                res = researcher.evaluate_strategy_candidate(cand)
                self.assertEqual(res["status"], "REJECTED")
                self.assertIn("CPCV test failed: Sharpe ratio 0.65 < 1.00", res["reason"])
                # Expected calls: PROPOSED, BACKTESTED, REJECTED
                actions = [call[1]["action"] for call in mock_record.call_args_list]
                self.assertIn("PROPOSED", actions)
                self.assertIn("BACKTESTED", actions)
                self.assertIn("REJECTED", actions)

        # Sub-test 2: Passes all hurdles -> APPROVED
        with patch.object(researcher, "backtest_strategy") as mock_bt:
            mock_bt.return_value = (55.0, 1.45, 12.0, {"total_trades": 120})
            with patch.object(researcher, "record_ledger") as mock_record:
                res = researcher.evaluate_strategy_candidate(cand)
                self.assertEqual(res["status"], "APPROVED")
                actions = [call[1]["action"] for call in mock_record.call_args_list]
                self.assertIn("PROPOSED", actions)
                self.assertIn("BACKTESTED", actions)
                self.assertIn("APPROVED", actions)


if __name__ == "__main__":
    unittest.main()
