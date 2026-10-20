"""
Unit & Integration Tests for Phase 4: Weekend Auditor
File: tests/test_weekend_auditor.py
"""

import os
import io
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from src.weekend_auditor import (
    WeekendAuditor,
    is_saturday,
    test_proposed_rule,
)


def test_is_saturday():
    # Saturday (e.g., 2026-10-17 is Saturday)
    sat_dt = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert is_saturday(sat_dt) is True

    # Sunday (e.g., 2026-10-18 is Sunday)
    sun_dt = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert is_saturday(sun_dt) is False

    # Monday
    mon_dt = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    assert is_saturday(mon_dt) is False


def test_test_proposed_rule(capsys):
    test_proposed_rule("Momentum breakout on 4h")
    captured = capsys.readouterr()
    expected_msg = (
        "Warning: Any rule proposed by the LLM must be validated here against "
        "3 months of historical data before being added to library_active.md."
    )
    assert expected_msg in captured.out


def test_time_gate_blocks_non_saturdays(capsys):
    auditor = WeekendAuditor()
    # Sunday
    sun_dt = datetime(2026, 10, 18, 10, 0, 0, tzinfo=timezone.utc)
    res = auditor.run_audit(force=False, as_of_date=sun_dt)
    captured = capsys.readouterr()
    assert res is None
    assert "Weekend Auditor only runs on Saturdays" in captured.out


def test_find_missed_trades():
    auditor = WeekendAuditor()
    sample_plans = [
        # Near misses (60-69)
        {"id": "1", "status": "PENDING", "plan_metadata": {"source_signal_score": 65.0}},
        {"id": "2", "status": "TIMEOUT", "plan_metadata": {"source_signal_score": 60.0}},
        {"id": "3", "status": "TIMEOUT", "plan_metadata": {"source_signal_score": 69.9}},
        # Excluded: score < 60
        {"id": "4", "status": "PENDING", "plan_metadata": {"source_signal_score": 58.0}},
        # Excluded: score >= 70 (these are viable/high confluence plans)
        {"id": "5", "status": "PENDING", "plan_metadata": {"source_signal_score": 75.0}},
        # Excluded: already filled
        {"id": "6", "status": "FILLED", "plan_metadata": {"source_signal_score": 65.0}},
    ]
    missed = auditor.find_missed_trades(sample_plans)
    missed_ids = [m["id"] for m in missed]
    assert missed_ids == ["1", "2", "3"]


def test_find_bad_trades():
    auditor = WeekendAuditor()
    sample_plans = [
        # Bad trade: score >= 70 and negative PnL
        {
            "id": "1",
            "status": "FILLED",
            "plan_metadata": {"confluence_score": 78.0, "realized_pnl": -45.0, "outcome": "LOSS"},
        },
        # Good trade: score >= 70 and positive PnL
        {
            "id": "2",
            "status": "FILLED",
            "plan_metadata": {"confluence_score": 82.0, "realized_pnl": 120.0, "outcome": "WIN"},
        },
        # Bad trade: score >= 70 and STOP_LOSS outcome
        {
            "id": "3",
            "status": "STOP_LOSS_HIT",
            "plan_metadata": {"source_signal_score": 72.0, "outcome": "STOP_LOSS"},
        },
        # Not bad: score < 70
        {
            "id": "4",
            "status": "FILLED",
            "plan_metadata": {"confluence_score": 65.0, "realized_pnl": -10.0},
        },
    ]
    bad = auditor.find_bad_trades(sample_plans)
    bad_ids = [b["id"] for b in bad]
    assert "1" in bad_ids
    assert "3" in bad_ids
    assert "2" not in bad_ids
    assert "4" not in bad_ids


def test_calculate_stats():
    auditor = WeekendAuditor()
    filled_plans = [
        {"id": "1", "plan_metadata": {"confluence_score": 80.0, "realized_pnl": 100.0, "outcome": "WIN"}},
        {"id": "2", "plan_metadata": {"confluence_score": 70.0, "realized_pnl": -50.0, "outcome": "LOSS"}},
        {"id": "3", "plan_metadata": {"confluence_score": 90.0, "realized_pnl": 50.0, "outcome": "WIN"}},
    ]
    stats = auditor.calculate_stats(filled_plans, [])
    assert stats["total_trades"] == 3
    assert stats["win_rate"] == "66.7%"
    assert stats["total_pnl"] == "+100.00 USDT"
    assert stats["avg_winner_score"] == "85.0"
    assert stats["avg_loser_score"] == "70.0"


def test_generate_report(tmp_path):
    reports_dir = str(tmp_path / "reports")
    auditor = WeekendAuditor(reports_dir=reports_dir)

    sat_dt = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    stats = {
        "total_trades": 5,
        "win_rate": "60.0%",
        "total_pnl": "+150.00 USDT",
        "avg_winner_score": "82.5",
        "avg_loser_score": "71.0",
    }
    missed = [
        {"symbol": "SOL/USDT", "direction": "LONG", "entry_price": 140.0, "status": "PENDING", "created_at": "2026-10-15T10:00:00Z", "plan_metadata": {"source_signal_score": 65.0}}
    ]
    bad = [
        {"symbol": "BTC/USDT", "direction": "SHORT", "entry_price": 68000.0, "status": "FILLED", "created_at": "2026-10-16T12:00:00Z", "plan_metadata": {"confluence_score": 75.0, "realized_pnl": -200.0, "outcome": "LOSS"}}
    ]

    report_path = auditor.generate_report(stats, missed, bad, as_of_date=sat_dt)
    assert os.path.exists(report_path)

    with open(report_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "## Weekly Stats" in content
    assert '## The "Missed Trades" (Score 60-69)' in content
    assert '## The "Bad Trades" (Score > 70 that lost)' in content
    assert "## Proposed Rule" in content
    assert "*Pending Hunter Agent Review*" in content
    assert "SOL/USDT" in content
    assert "BTC/USDT" in content


def test_probe_schema(caplog):
    mock_db = MagicMock()
    mock_db.client.table.return_value.select.return_value.limit.return_value.execute.return_value.data = [
        {"id": "plan-1", "symbol": "BTC/USDT", "plan_metadata": {"confluence_score": 75.0, "atr": 100.0}}
    ]
    auditor = WeekendAuditor(db_manager=mock_db)
    with caplog.at_level("INFO"):
        auditor._probe_schema()
    assert "trade_plans keys:" in caplog.text
    assert "plan_metadata keys:" in caplog.text
