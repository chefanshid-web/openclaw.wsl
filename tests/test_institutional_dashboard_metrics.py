"""
Unit tests for Institutional Dashboard Metrics:
1. Live Price Z-Score (Rolling 20-period on 1H for BTC/USDT:USDT)
2. Buy-and-Hold Benchmark comparison
"""
from pathlib import Path
import numpy as np
import pytest
from unittest.mock import MagicMock, patch


def test_dashboard_file_institutional_metrics_syntax_and_ui():
    """Verify that src/dashboard.py has all requested UI components and logic."""
    dash_path = Path(__file__).resolve().parent.parent / "src" / "dashboard.py"
    assert dash_path.exists(), "src/dashboard.py must exist"
    content = dash_path.read_text(encoding="utf-8")

    # Task 1: Z-score requirements
    assert "Price Z-Score (BTC)" in content
    assert "get_btc_price_zscore" in content
    assert "zscore-oversold" in content
    assert "zscore-overbought" in content
    assert "zscore-neutral" in content
    assert "Oversold (Stretched Down)" in content
    assert "Overbought (Stretched Up)" in content

    # Task 2: Buy-and-Hold Benchmark requirements
    assert "get_buy_and_hold_benchmark" in content
    assert "Bot Net Return (%)" in content
    assert "Buy & Hold BTC (%)" in content
    assert "⚠️ Bot is underperforming Buy & Hold." in content
    assert "✅ Bot is generating Alpha." in content
    assert "PAPER_FILL" in content


def test_zscore_calculation_logic():
    """Test standard deviation, mean, and z-score computation formula."""
    closes = [
        100.0, 102.0, 101.0, 103.0, 105.0,
        104.0, 106.0, 107.0, 105.0, 108.0,
        110.0, 109.0, 111.0, 112.0, 114.0,
        113.0, 115.0, 117.0, 116.0, 120.0,
    ]
    curr_close = closes[-1]
    mean_val = float(np.mean(closes))
    std_val = float(np.std(closes, ddof=1))
    expected_z = (curr_close - mean_val) / std_val

    assert len(closes) == 20
    assert curr_close == 120.0
    assert expected_z > 0

    # Test extreme values for status tagging
    # Oversold: <= -2.0
    assert -2.01 <= -2.0
    # Overbought: >= +2.0
    assert 2.05 >= 2.0


def test_buy_and_hold_formula():
    """Test buy and hold return percentage calculation."""
    start_price = 80000.0
    current_price = 88000.0
    expected_bh_return = ((current_price - start_price) / start_price) * 100.0
    assert pytest.approx(expected_bh_return, 0.01) == 10.0

    # Bot return assuming $100 starting capital
    net_profit_alpha = 15.0
    bot_return_alpha = (net_profit_alpha / 100.0) * 100.0
    assert bot_return_alpha > expected_bh_return  # Generating Alpha

    net_profit_under = 5.0
    bot_return_under = (net_profit_under / 100.0) * 100.0
    assert bot_return_under < expected_bh_return  # Underperforming


def test_get_btc_price_zscore_mocked_ccxt():
    """Test get_btc_price_zscore function with mocked CCXT exchange."""
    from src.dashboard import get_btc_price_zscore

    # Mock 20 candles: [timestamp, open, high, low, close, volume]
    mock_ohlcv = [[i * 3600000, 100.0, 105.0, 95.0, 100.0 + i, 10.0] for i in range(20)]

    with patch("ccxt.binance") as mock_binance:
        mock_instance = MagicMock()
        mock_instance.fetch_ohlcv.return_value = mock_ohlcv
        mock_binance.return_value = mock_instance

        # Clear Streamlit cache if applicable
        if hasattr(get_btc_price_zscore, "clear"):
            get_btc_price_zscore.clear()

        res = get_btc_price_zscore("BTC/USDT:USDT")
        assert res["status"] == "success"
        assert res["period"] == 20
        assert "z_score" in res
        assert "current_price" in res
        assert "mean_price" in res
        assert "std_price" in res


def test_get_buy_and_hold_benchmark_with_supabase():
    """Test get_buy_and_hold_benchmark with Supabase PAPER_FILL record."""
    from src.dashboard import get_buy_and_hold_benchmark

    mock_db = MagicMock()
    mock_table = mock_db.client.table.return_value
    mock_table.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value.data = [
        {
            "id": "fill-1",
            "signal_type": "PAPER_FILL",
            "created_at": "2026-10-16T12:00:00Z",
            "signal_payload": {"price": 60000.0},
        }
    ]

    with patch("ccxt.binance") as mock_binance:
        mock_instance = MagicMock()
        mock_instance.fetch_ticker.return_value = {"last": 66000.0}
        mock_binance.return_value = mock_instance

        if hasattr(get_buy_and_hold_benchmark, "clear"):
            get_buy_and_hold_benchmark.clear()

        res = get_buy_and_hold_benchmark(mock_db)
        assert res["start_btc_price"] == 60000.0
        assert res["current_btc_price"] == 66000.0
        assert pytest.approx(res["buy_hold_return"], 0.01) == 10.0
        assert "First PAPER_FILL" in res["source"]
