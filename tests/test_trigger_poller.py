import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from src.trigger_poller import TriggerPoller


class TestTriggerPoller:

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.check_halt.return_value = False
        return db

    @pytest.fixture
    def mock_executor(self):
        executor = MagicMock()
        executor.fill_triggered_plan.return_value = True
        return executor

    @pytest.fixture
    def mock_exchange(self):
        exchange = MagicMock()
        exchange.fetch_ticker.return_value = {"last": 67950.0, "close": 67950.0}
        return exchange

    def test_trigger_condition_long(self, mock_db, mock_executor, mock_exchange):
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)

        # Long: entry limit is 68000.0
        assert poller.check_trigger_condition("LONG", current_price=68000.0, entry_price=68000.0) is True
        assert poller.check_trigger_condition("LONG", current_price=67900.0, entry_price=68000.0) is True
        assert poller.check_trigger_condition("LONG", current_price=68100.0, entry_price=68000.0) is False

    def test_trigger_condition_short(self, mock_db, mock_executor, mock_exchange):
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)

        # Short: entry limit is 68000.0
        assert poller.check_trigger_condition("SHORT", current_price=68000.0, entry_price=68000.0) is True
        assert poller.check_trigger_condition("SHORT", current_price=68100.0, entry_price=68000.0) is True
        assert poller.check_trigger_condition("SHORT", current_price=67900.0, entry_price=68000.0) is False

    def test_evaluate_plan_timeout(self, mock_db, mock_executor, mock_exchange):
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()

        plan = {
            "id": "plan-timeout-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 68000.0,
            "created_at": stale_time
        }

        with patch.object(poller, "update_plan_status", return_value=True) as mock_update:
            res = poller.evaluate_plan(plan)
            assert res == "TIMEOUT"
            mock_update.assert_called_once()
            args = mock_update.call_args[0]
            assert args[0] == "plan-timeout-1"
            assert args[1] == "TIMEOUT"

    def test_evaluate_plan_triggered_and_sent_to_executor(self, mock_db, mock_executor, mock_exchange):
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)
        poller.adverse_selection_rate = 0.0  # Test normal triggered path
        recent_time = datetime.now(timezone.utc).isoformat()

        plan = {
            "id": "plan-triggered-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 68000.0,  # market price is 67950.0 -> triggered!
            "created_at": recent_time
        }

        with patch.object(poller, "update_plan_status", return_value=True) as mock_update:
            res = poller.evaluate_plan(plan)
            assert res == "TRIGGERED"
            mock_update.assert_called_once()
            assert mock_update.call_args[0][1] == "TRIGGERED"
            mock_executor.fill_triggered_plan.assert_called_once_with(plan, fill_price=67950.0)

    def test_evaluate_plan_adverse_selection_rejected(self, mock_db, mock_executor, mock_exchange):
        """If price crosses entry but immediately reverses, 30% of the time mark as REJECTED."""
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)
        poller.adverse_selection_rate = 1.0  # Force adverse selection reversal
        recent_time = datetime.now(timezone.utc).isoformat()

        plan = {
            "id": "plan-adverse-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 68000.0,
            "created_at": recent_time
        }

        with patch.object(poller, "update_plan_status", return_value=True) as mock_update:
            res = poller.evaluate_plan(plan)
            assert res == "REJECTED"
            mock_update.assert_called_once()
            assert mock_update.call_args[0][1] == "REJECTED"
            assert mock_update.call_args[0][2]["reason"] == "ADVERSE_SELECTION_REVERSAL"
            mock_executor.fill_triggered_plan.assert_not_called()

    def test_stale_ticker_skips_trigger_and_executes_zero_trades(self, mock_db, mock_executor, mock_exchange):
        """If CCXT fetch_ticker timestamp is older than 10 seconds, skip trigger evaluation and execute zero trades."""
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)
        stale_ts_ms = (time.time() - 15.0) * 1000.0  # 15 seconds old (> 10s cutoff)
        mock_exchange.fetch_ticker.return_value = {
            "last": 67950.0,
            "close": 67950.0,
            "timestamp": stale_ts_ms,
        }

        plan = {
            "id": "plan-stale-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 68000.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        with patch.object(poller, "update_plan_status") as mock_update:
            res = poller.evaluate_plan(plan)
            assert res == "ERROR"
            mock_update.assert_not_called()
            mock_executor.fill_triggered_plan.assert_not_called()
            mock_executor.fill_plan.assert_not_called()

    def test_fresh_ticker_evaluates_trigger_normally(self, mock_db, mock_executor, mock_exchange):
        """If CCXT fetch_ticker timestamp is fresh (<= 10s), evaluate trigger normally."""
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)
        poller.adverse_selection_rate = 0.0
        fresh_ts_ms = (time.time() - 2.0) * 1000.0  # 2 seconds old (<= 10s)
        mock_exchange.fetch_ticker.return_value = {
            "last": 67950.0,
            "close": 67950.0,
            "timestamp": fresh_ts_ms,
        }

        plan = {
            "id": "plan-fresh-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 68000.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        with patch.object(poller, "update_plan_status", return_value=True) as mock_update:
            res = poller.evaluate_plan(plan)
            assert res == "TRIGGERED"
            mock_update.assert_called_once()
            mock_executor.fill_triggered_plan.assert_called_once_with(plan, fill_price=67950.0)

    def test_poll_once_halted_skips(self, mock_db, mock_executor, mock_exchange):
        mock_db.check_halt.return_value = True
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)

        res = poller.poll_once()
        assert res == {"evaluated": 0, "triggered": 0, "timeout": 0}

    def test_probe_schema_called_only_once_across_multiple_polls(self, mock_db, mock_executor, mock_exchange):
        poller = TriggerPoller(db_manager=mock_db, executor=mock_executor, exchange=mock_exchange)
        with patch.object(poller, "_probe_schema") as mock_probe, \
             patch.object(poller, "get_pending_plans", return_value=[]):
            poller.poll_once()
            poller.poll_once()
            poller.poll_once()
            assert mock_probe.call_count == 1
            assert poller._schema_probed is True

    def test_probe_schema_retries_if_ensure_db_fails_initially(self, mock_db, mock_executor, mock_exchange):
        poller = TriggerPoller(db_manager=None, executor=mock_executor, exchange=mock_exchange)
        with patch.object(poller, "_probe_schema") as mock_probe, \
             patch.object(poller, "get_pending_plans", return_value=[]):
            # First call: _ensure_db returns None
            with patch.object(poller, "_ensure_db", return_value=None):
                poller.poll_once()
                assert mock_probe.call_count == 0
                assert poller._schema_probed is False

            # Second call: _ensure_db succeeds
            with patch.object(poller, "_ensure_db", return_value=mock_db):
                poller.poll_once()
                assert mock_probe.call_count == 1
                assert poller._schema_probed is True


def test_paper_executor_probe_schema_called_only_once():
    from src.paper_executor import PaperExecutor
    mock_db = MagicMock()
    mock_db.check_halt.return_value = False
    executor = PaperExecutor(db_manager=mock_db)
    with patch.object(executor, "_probe_schema") as mock_probe, \
         patch.object(executor, "get_triggered_plans", return_value=[]):
        executor.poll_once()
        executor.poll_once()
        assert mock_probe.call_count == 1
        assert executor._schema_probed is True


def test_paper_executor_probe_schema_retries_on_initial_db_failure():
    from src.paper_executor import PaperExecutor
    mock_db = MagicMock()
    mock_db.check_halt.return_value = False
    executor = PaperExecutor(db_manager=None)
    with patch.object(executor, "_probe_schema") as mock_probe, \
         patch.object(executor, "get_triggered_plans", return_value=[]):
        # 1st attempt: db failure
        with patch.object(executor, "_ensure_db", return_value=None):
            executor.poll_once()
            assert mock_probe.call_count == 0
            assert executor._schema_probed is False

        # 2nd attempt: db recovers
        with patch.object(executor, "_ensure_db", return_value=mock_db):
            executor.poll_once()
            assert mock_probe.call_count == 1
            assert executor._schema_probed is True


def test_paper_executor_atomic_lock_prevents_double_fill():
    from src.paper_executor import PaperExecutor
    mock_db = MagicMock()
    executor = PaperExecutor(db_manager=mock_db)

    plan = {
        "id": "plan-atomic-1",
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "entry_price": 68000.0,
        "plan_metadata": {
            "triggered_at": datetime.now(timezone.utc).isoformat()
        }
    }

    mock_table = mock_db.client.table.return_value
    mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"plan_metadata": {}}
    ]
    mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = []

    res = executor.fill_triggered_plan(plan)
    assert res is False
    mock_db.log_signal.assert_not_called()


def test_paper_executor_atomic_lock_successful_fill():
    from src.paper_executor import PaperExecutor
    mock_db = MagicMock()
    executor = PaperExecutor(db_manager=mock_db)

    plan = {
        "id": "plan-atomic-2",
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "entry_price": 68000.0,
        "plan_metadata": {
            "triggered_at": datetime.now(timezone.utc).isoformat()
        }
    }

    mock_table = mock_db.client.table.return_value
    mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
        {"plan_metadata": {}}
    ]
    mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
        {"id": "plan-atomic-2", "status": "FILLED"}
    ]

    res = executor.fill_triggered_plan(plan)
    assert res is True
    mock_db.log_signal.assert_called_once()
    assert mock_db.log_signal.call_args[1]["signal_type"] == "PAPER_FILL"


def test_paper_executor_ghost_fill_ttl_expiration_marks_missed():
    from src.paper_executor import PaperExecutor
    mock_db = MagicMock()
    executor = PaperExecutor(db_manager=mock_db)

    old_trigger = (datetime.now(timezone.utc) - timedelta(seconds=65)).isoformat()
    plan = {
        "id": "plan-ghost-1",
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "entry_price": 68000.0,
        "plan_metadata": {
            "triggered_at": old_trigger
        }
    }

    with patch.object(executor, "update_plan_status", return_value=True) as mock_update:
        res = executor.fill_triggered_plan(plan)
        assert res is False
        mock_update.assert_called_once()
        assert mock_update.call_args[0][0] == "plan-ghost-1"
        assert mock_update.call_args[0][1] == "MISSED"
        mock_db.log_signal.assert_not_called()
