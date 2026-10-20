"""
Unit tests for SupabaseManager (offline mock verification)
"""
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
from src.db_manager import SupabaseManager


class TestSupabaseManagerUnit(unittest.TestCase):
    @patch("supabase.create_client")
    def setUp(self, mock_create):
        self.mock_client = MagicMock()
        mock_create.return_value = self.mock_client
        self.mgr = SupabaseManager(url="https://mock.supabase.co", key="mock-key")

    def test_lookahead_constraint_rejection(self):
        """Verify that known_at_time < event_time triggers ValueError."""
        event_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        known_at_time = datetime(2026, 1, 1, 11, 59, 59, tzinfo=timezone.utc)

        with self.assertRaises(ValueError) as ctx:
            self.mgr.log_signal("BTC/USDT", event_time, "BREAKOUT_LONG", 0.85, known_at_time=known_at_time)

        self.assertIn("Constraint violation", str(ctx.exception))

    def test_valid_signal_logging(self):
        """Verify valid signal with known_at_time >= event_time."""
        event_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        known_at_time = datetime(2026, 1, 1, 12, 1, 0, tzinfo=timezone.utc)

        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.insert.return_value.execute.return_value.data = [
            {"id": "00000000-0000-0000-0000-000000000001", "symbol": "BTC/USDT", "score": 0.85, "retry_count": 0}
        ]

        result = self.mgr.log_signal(
            symbol="BTC/USDT",
            event_time=event_time,
            signal_type="BREAKOUT_LONG",
            score=0.85,
            metadata={"source": "test"},
            known_at_time=known_at_time,
            retry_count=0,
        )
        self.assertEqual(result["symbol"], "BTC/USDT")
        mock_table.insert.assert_called_once()

    def test_increment_retry_under_threshold(self):
        """Verify increment_retry increments counter and does not escalate under 3."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"retry_count": 1}
        ]

        res = self.mgr.increment_retry("00000000-0000-0000-0000-000000000001")
        self.assertEqual(res["retry_count"], 2)
        self.assertFalse(res["escalate_to_operator"])
        mock_table.update.assert_called_with({"retry_count": 2})

    def test_increment_retry_escalation(self):
        """Verify increment_retry triggers escalation when retry_count reaches >= 3."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"retry_count": 2}
        ]

        res = self.mgr.increment_retry("00000000-0000-0000-0000-000000000001")
        self.assertEqual(res["retry_count"], 3)
        self.assertTrue(res["escalate_to_operator"])
        mock_table.update.assert_called_with({"retry_count": 3})

    def test_check_halt(self):
        """Verify check_halt returns True when status is 'HALT' and False when 'RUNNING'."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table

        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"status": "RUNNING"}
        ]
        self.assertFalse(self.mgr.check_halt())

        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"status": "HALT"}
        ]
        self.assertTrue(self.mgr.check_halt())

    def test_set_halt(self):
        """Verify set_halt sends 'HALT' status and reason."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.execute.return_value.data = [
            {"id": 1, "status": "HALT", "reason": "Test Kill Switch"}
        ]

        res = self.mgr.set_halt("Test Kill Switch")
        self.assertEqual(res["status"], "HALT")
        mock_table.update.assert_called_once()

    def test_set_circuit_breaker(self):
        """Verify set_circuit_breaker updates system_state to 'CIRCUIT_BREAKER' with 24h cool down."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.execute.return_value.data = [
            {"id": 1, "status": "CIRCUIT_BREAKER", "reason": "5% intra-hour drop"}
        ]

        res = self.mgr.set_circuit_breaker("5% intra-hour drop")
        self.assertEqual(res["status"], "CIRCUIT_BREAKER")
        mock_table.update.assert_called_once()
        call_payload = mock_table.update.call_args[0][0]
        self.assertEqual(call_payload["status"], "CIRCUIT_BREAKER")
        self.assertIn("cool_down_until", call_payload)

    def test_set_weekend_flattened(self):
        """Verify set_weekend_flattened updates system_state to 'WEEKEND_FLATTENED'."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.update.return_value.eq.return_value.execute.return_value.data = [
            {"id": 1, "status": "WEEKEND_FLATTENED", "reason": "Friday 15:30 UTC flatten"}
        ]

        res = self.mgr.set_weekend_flattened("Friday 15:30 UTC flatten")
        self.assertEqual(res["status"], "WEEKEND_FLATTENED")

    def test_check_halt_with_circuit_breaker_and_weekend_flattened(self):
        """Verify check_halt returns True for CIRCUIT_BREAKER and WEEKEND_FLATTENED states."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table

        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"status": "CIRCUIT_BREAKER"}
        ]
        self.assertTrue(self.mgr.check_halt())

        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"status": "WEEKEND_FLATTENED"}
        ]
        self.assertTrue(self.mgr.check_halt())

    def test_check_intra_hour_drawdown_hard_liquidation(self):
        """Verify 5% drop in 60 minutes triggers hard liquidation circuit breaker."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.select.return_value.in_.return_value.execute.return_value.data = []
        mock_table.update.return_value.eq.return_value.execute.return_value.data = [
            {"id": 1, "status": "CIRCUIT_BREAKER"}
        ]

        # 5% drop: 100,000 -> 95,000 in 45 minutes
        passed, drop_pct, reason = self.mgr.check_intra_hour_drawdown(
            starting_equity=100000.0,
            current_equity=95000.0,
            time_elapsed_minutes=45.0
        )
        self.assertFalse(passed)
        self.assertAlmostEqual(drop_pct, 0.05, places=4)
        self.assertIn("Intra-Hour Hard Liquidation", reason)

    def test_check_intra_hour_drawdown_safe(self):
        """Verify drop < 5% does not trip intra-hour circuit breaker."""
        # 3% drop: 100,000 -> 97,000 in 30 minutes
        passed, drop_pct, reason = self.mgr.check_intra_hour_drawdown(
            starting_equity=100000.0,
            current_equity=97000.0,
            time_elapsed_minutes=30.0
        )
        self.assertTrue(passed)
        self.assertAlmostEqual(drop_pct, 0.03, places=4)



    @patch("src.db_manager.time.sleep")
    def test_retry_db_call_backoff_and_recovery(self, mock_sleep):
        """Verify retry_db_call retries with exponential backoff on transient errors."""
        from src.db_manager import retry_db_call

        calls = {"count": 0}

        def flaky_op():
            calls["count"] += 1
            if calls["count"] < 3:
                raise ConnectionResetError("Remote server closed connection")
            return {"status": "success"}

        res = retry_db_call(flaky_op, max_retries=3, base_delay=1.0)
        self.assertEqual(res["status"], "success")
        self.assertEqual(calls["count"], 3)
        # Attempt 0: delay = 1.0 * (2**0) = 1.0; Attempt 1: delay = 1.0 * (2**1) = 2.0
        mock_sleep.assert_has_calls([unittest.mock.call(1.0), unittest.mock.call(2.0)])

    @patch("src.db_manager.time.sleep")
    def test_retry_db_call_exhaustion(self, mock_sleep):
        """Verify retry_db_call raises exception after max_retries are exhausted."""
        from src.db_manager import retry_db_call

        calls = {"count": 0}

        def always_failing():
            calls["count"] += 1
            raise TimeoutError("Gateway timeout")

        with self.assertRaises(TimeoutError):
            retry_db_call(always_failing, max_retries=3, base_delay=1.0)

        self.assertEqual(calls["count"], 3)
        mock_sleep.assert_has_calls([unittest.mock.call(1.0), unittest.mock.call(2.0), unittest.mock.call(4.0)])

    @patch("src.db_manager.time.sleep")
    def test_log_signal_retries_on_network_drop(self, mock_sleep):
        """Verify log_signal retries insert on temporary failure and succeeds."""
        event_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table

        # First attempt fails with network error, second succeeds
        mock_table.insert.return_value.execute.side_effect = [
            ConnectionResetError("Socket disconnected"),
            MagicMock(data=[{"id": "sig-retried-1", "symbol": "ETH/USDT"}])
        ]

        result = self.mgr.log_signal(
            symbol="ETH/USDT",
            event_time=event_time,
            signal_type="BREAKOUT_LONG",
            score=0.90,
        )
        self.assertEqual(result["id"], "sig-retried-1")
        mock_sleep.assert_called_once_with(1.0)

    @patch("src.db_manager.time.sleep")
    def test_save_trade_plan_retries_on_transient_failure(self, mock_sleep):
        """Verify save_trade_plan on SupabaseManager retries on transient error."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.select.return_value.eq.return_value.execute.return_value.data = []

        mock_table.insert.return_value.execute.side_effect = [
            Exception("HTTP 502 Bad Gateway"),
            MagicMock(data=[{"id": "plan-retried-1"}])
        ]

        plan = {
            "symbol": "BTC/USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 48000.0,
            "take_profit": 55000.0,
        }
        res = self.mgr.save_trade_plan(plan)
        self.assertEqual(res["id"], "plan-retried-1")
        mock_sleep.assert_called_once_with(1.0)

    @patch("src.db_manager.time.sleep")
    def test_get_triggered_plans_retries_on_transient_failure(self, mock_sleep):
        """Verify get_triggered_plans on SupabaseManager retries on transient error."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table

        mock_table.select.return_value.eq.return_value.order.return_value.execute.side_effect = [
            Exception("SSL SYSCALL error: EOF detected"),
            MagicMock(data=[{"id": "plan-trig-1", "status": "TRIGGERED"}])
        ]

        plans = self.mgr.get_triggered_plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["id"], "plan-trig-1")
        mock_sleep.assert_called_once_with(1.0)

    @patch("src.db_manager.time.sleep")
    def test_update_plan_status_retries_on_transient_failure(self, mock_sleep):
        """Verify update_plan_status on SupabaseManager retries on transient error."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"plan_metadata": {"old": 1}}
        ]

        mock_table.update.return_value.eq.return_value.execute.side_effect = [
            Exception("Connection reset by peer"),
            MagicMock(data=[{"id": "plan-update-1"}])
        ]

        success = self.mgr.update_plan_status("plan-update-1", "FILLED", {"new": 2})
        self.assertTrue(success)
        mock_sleep.assert_called_once_with(1.0)

    def test_log_factory_ledger_success(self):
        """Verify log_factory_ledger writes row to Supabase."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.insert.return_value.execute.return_value.data = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "strategy_name": "Test Strategy",
                "agent_name": "Hunter",
                "action": "PROPOSED",
                "reason": "Test proposal",
                "metrics": {"param1": 1},
            }
        ]

        res = self.mgr.log_factory_ledger(
            strategy_name="Test Strategy",
            agent_name="Hunter",
            action="PROPOSED",
            reason="Test proposal",
            metrics={"param1": 1},
        )
        self.assertEqual(res["action"], "PROPOSED")
        self.assertEqual(res["strategy_name"], "Test Strategy")
        mock_table.insert.assert_called_once()

    def test_log_factory_ledger_offline_fallback(self):
        """Verify log_factory_ledger gracefully returns row dict on DB insert failure."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.insert.return_value.execute.side_effect = Exception("Relation 'factory_ledger' does not exist")

        res = self.mgr.log_factory_ledger(
            strategy_name="Failing Strategy",
            agent_name="Backtest",
            action="BACKTESTED",
            reason="Table missing",
            metrics={"sharpe": 1.5},
        )
        self.assertEqual(res["strategy_name"], "Failing Strategy")
        self.assertEqual(res["action"], "BACKTESTED")

    def test_get_factory_ledger(self):
        """Verify get_factory_ledger queries Supabase ordered by timestamp desc."""
        mock_table = MagicMock()
        self.mock_client.table.return_value = mock_table
        mock_table.select.return_value.order.return_value.limit.return_value.execute.return_value.data = [
            {"id": "row-1", "strategy_name": "Strat 1", "action": "PROPOSED"},
            {"id": "row-2", "strategy_name": "Strat 2", "action": "BACKTESTED"},
        ]

        rows = self.mgr.get_factory_ledger(limit=10)
        self.assertEqual(len(rows), 2)
        mock_table.select.assert_called_with("*")
        mock_table.select.return_value.order.assert_called_with("timestamp", desc=True)
        mock_table.select.return_value.order.return_value.limit.assert_called_with(10)


if __name__ == "__main__":
    unittest.main()
