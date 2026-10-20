"""
Unit tests for Agent Health Monitor and SRE Watchdog Auto-Heal Loop.
Verifies Tasks 1, 2, 3, and 4 implementations.
"""

import time
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from src.db_manager import SupabaseManager
from src.sre_watchdog import SREWatchdog


class TestAgentHealthDBManager:
    """Tests for SupabaseManager.update_agent_health and get_agent_health."""

    def test_update_agent_health_with_client(self):
        """Verify update_agent_health formats payload and calls upsert correctly."""
        mock_client = MagicMock()
        mock_table = MagicMock()
        mock_client.table.return_value = mock_table
        mock_table.upsert.return_value.execute.return_value = MagicMock(
            data=[{
                "agent_name": "Scanner",
                "status": "HEALTHY",
                "latency_ms": 145.2,
                "error_msg": None,
                "last_heartbeat": "2026-10-20T12:00:00Z",
            }]
        )

        mgr = SupabaseManager.__new__(SupabaseManager)
        mgr.client = mock_client
        mgr.authenticated = True

        res = mgr.update_agent_health(
            agent_name="Scanner",
            status="HEALTHY",
            latency_ms=145.2,
            error_msg=None,
        )

        assert res["agent_name"] == "Scanner"
        assert res["status"] == "HEALTHY"
        assert res["latency_ms"] == 145.2
        assert res["error_msg"] is None
        assert "last_heartbeat" in res

        mock_client.table.assert_called_with("agent_health")
        mock_table.upsert.assert_called_once()

    def test_update_agent_health_critical_error(self):
        """Verify update_agent_health logs critical errors with exception message."""
        mock_client = MagicMock()
        mock_table = MagicMock()
        mock_client.table.return_value = mock_table
        mock_table.upsert.return_value.execute.return_value = MagicMock(data=[])

        mgr = SupabaseManager.__new__(SupabaseManager)
        mgr.client = mock_client
        mgr.authenticated = True

        res = mgr.update_agent_health(
            agent_name="TriggerPoller",
            status="CRITICAL",
            latency_ms=999.0,
            error_msg="Connection reset by peer",
        )

        assert res["agent_name"] == "TriggerPoller"
        assert res["status"] == "CRITICAL"
        assert res["error_msg"] == "Connection reset by peer"

    def test_update_agent_health_offline_fallback(self):
        """Verify update_agent_health handles offline mode gracefully without crashing."""
        mgr = SupabaseManager.__new__(SupabaseManager)
        mgr.client = None
        mgr.authenticated = False

        res = mgr.update_agent_health("PaperExecutor", "HEALTHY", 80.0, None)
        assert res["agent_name"] == "PaperExecutor"
        assert res["status"] == "HEALTHY"

    def test_get_agent_health_queries_table(self):
        """Verify get_agent_health queries agent_health ordered by last_heartbeat."""
        mock_client = MagicMock()
        mock_table = MagicMock()
        mock_query = MagicMock()
        mock_client.table.return_value = mock_table
        mock_table.select.return_value = mock_query
        mock_query.order.return_value.execute.return_value = MagicMock(data=[
            {"agent_name": "Scanner", "status": "HEALTHY", "latency_ms": 120.0},
            {"agent_name": "TriggerPoller", "status": "HEALTHY", "latency_ms": 40.0},
        ])

        mgr = SupabaseManager.__new__(SupabaseManager)
        mgr.client = mock_client
        mgr.authenticated = True

        data = mgr.get_agent_health()
        assert len(data) == 2
        assert data[0]["agent_name"] == "Scanner"
        mock_client.table.assert_called_with("agent_health")


class TestSREWatchdog:
    """Tests for SREWatchdog auto-healing, stale heartbeat detection, and alerting."""

    def test_watchdog_detects_healthy_agents(self):
        """Healthy agents with recent heartbeats should not trigger healing or alerts."""
        mock_db = MagicMock()
        now_iso = datetime.now(timezone.utc).isoformat()
        mock_db.get_agent_health.return_value = [
            {"agent_name": "Scanner", "status": "HEALTHY", "latency_ms": 110.0, "last_heartbeat": now_iso, "error_msg": None},
            {"agent_name": "TriggerPoller", "status": "HEALTHY", "latency_ms": 35.0, "last_heartbeat": now_iso, "error_msg": None},
        ]

        watchdog = SREWatchdog(db_manager=mock_db, heartbeat_timeout_seconds=60)
        res = watchdog.check_and_heal_once()

        assert res["evaluated"] == 2
        assert res["healthy"] == 2
        assert res["healed"] == 0
        assert res["escalated"] == 0

    def test_watchdog_auto_heals_stale_agent(self):
        """Agents with heartbeats older than 60 seconds should be auto-restarted."""
        mock_db = MagicMock()
        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=75)).isoformat()
        mock_db.get_agent_health.return_value = [
            {"agent_name": "Scanner", "status": "HEALTHY", "latency_ms": 120.0, "last_heartbeat": stale_time, "error_msg": None},
        ]

        watchdog = SREWatchdog(db_manager=mock_db, heartbeat_timeout_seconds=60)
        restart_called = False

        def mock_scanner_runner():
            nonlocal restart_called
            restart_called = True

        watchdog.register_agent("Scanner", mock_scanner_runner)

        res = watchdog.check_and_heal_once()

        assert res["evaluated"] == 1
        assert res["healed"] == 1
        assert res["escalated"] == 0
        assert len(res["details"]) == 1
        assert res["details"][0]["action"] == "HEALED"

        # Verify update_agent_health was called to mark recovered
        mock_db.update_agent_health.assert_called_with("Scanner", "HEALTHY", latency_ms=0.0, error_msg=None)

    def test_watchdog_auto_heals_critical_status(self):
        """Agents with CRITICAL status should immediately trigger restart."""
        mock_db = MagicMock()
        recent_time = datetime.now(timezone.utc).isoformat()
        mock_db.get_agent_health.return_value = [
            {"agent_name": "PaperExecutor", "status": "CRITICAL", "latency_ms": 2500.0, "last_heartbeat": recent_time, "error_msg": "Order rejected: Out of margin"},
        ]

        watchdog = SREWatchdog(db_manager=mock_db, heartbeat_timeout_seconds=60)
        mock_runner = MagicMock()
        watchdog.register_agent("PaperExecutor", mock_runner)

        res = watchdog.check_and_heal_once()

        assert res["evaluated"] == 1
        assert res["healed"] == 1
        assert res["escalated"] == 0
        assert res["details"][0]["agent"] == "PaperExecutor"

    def test_watchdog_escalates_to_whatsapp_when_unregistered(self):
        """Unknown or unregistered agents failing health checks must escalate via WhatsApp."""
        mock_db = MagicMock()
        recent_time = datetime.now(timezone.utc).isoformat()
        mock_db.get_agent_health.return_value = [
            {"agent_name": "UnknownDaemon", "status": "CRITICAL", "latency_ms": 0.0, "last_heartbeat": recent_time, "error_msg": "Fatal crash"},
        ]

        watchdog = SREWatchdog(db_manager=mock_db)
        with patch.object(watchdog, "send_whatsapp_alert", return_value=True) as mock_alert:
            res = watchdog.check_and_heal_once()

            assert res["evaluated"] == 1
            assert res["healed"] == 0
            assert res["escalated"] == 1
            mock_alert.assert_called_once()
            assert "UnknownDaemon" in mock_alert.call_args[0][0]


class TestAgentInstrumentation:
    """Verify that agents instrument their loops with update_agent_health."""

    def test_trigger_poller_instruments_health_loop(self):
        """TriggerPoller.run_continuous calls db.update_agent_health."""
        from src.trigger_poller import TriggerPoller

        mock_db = MagicMock()
        poller = TriggerPoller(db_manager=mock_db)

        def stop_after_one():
            poller.running = False

        poller.poll_once = MagicMock(side_effect=stop_after_one)
        poller.run_continuous(interval_seconds=1)

        mock_db.update_agent_health.assert_called_once()
        args = mock_db.update_agent_health.call_args[0]
        assert args[0] == "TriggerPoller"
        assert args[1] in ("HEALTHY", "WARNING")
        assert isinstance(args[2], float)  # latency_ms
        assert args[3] is None  # error_msg

    def test_paper_executor_instruments_health_loop(self):
        """PaperExecutor.run_continuous calls db.update_agent_health."""
        from src.paper_executor import PaperExecutor

        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        def stop_after_one():
            executor.running = False

        executor.poll_once = MagicMock(side_effect=stop_after_one)
        executor.run_continuous(interval_seconds=1)

        mock_db.update_agent_health.assert_called_once()
        args = mock_db.update_agent_health.call_args[0]
        assert args[0] == "PaperExecutor"
        assert args[1] in ("HEALTHY", "WARNING")
        assert isinstance(args[2], float)
        assert args[3] is None

    def test_macro_fetcher_instruments_health(self):
        """MacroFetcher.get_macro_risk calls update_agent_health on DB."""
        from src.macro_fetcher import MacroFetcher

        mock_db = MagicMock()
        mf = MacroFetcher(db_manager=mock_db)

        # Mock ticker data to ensure healthy fetch
        with patch.object(mf, "_fetch_yfinance_ticker") as mock_fetch:
            mock_fetch.return_value = {
                "price": 18500.0,
                "change_pct": 0.5,
                "change_24h_pct": 0.5,
                "ma_200": 18000.0,
                "timestamp": time.time(),
            }

            result = mf.get_macro_risk()
            assert isinstance(result, dict)
            assert "score" in result or "Macro_Risk_Score" in result

            # Verify health was updated with MacroFetcher
            assert mock_db.update_agent_health.called
            calls = [c[0] for c in mock_db.update_agent_health.call_args_list]
            assert any(c[0] == "MacroFetcher" for c in calls)
