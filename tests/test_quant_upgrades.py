"""
Test Suite for Senior Quant Upgrades
(tests/test_quant_upgrades.py)

Covers:
Task 1: Whale Detection (Order Flow Imbalance & check_whale_footprint)
Task 2: Regime Detection (14-period ADX calculation & apply_regime_gate)
Task 3: Trend Filter (filter_short_by_ema via DataFetcher, 4H EMA 200)
Task 4: Cool-Down Timer (cool_down_until column, set_halt +4h, TriggerPoller ignore gate)
Task 5: Slippage Simulation (random slippage in [0, 0.001] for LONG and SHORT fills)
Task 6: Multi-Timeframe Architecture (1H core scanner with 4H macro EMA 200 trend filter)
Dynamic Upgrade 1: Session Awareness (is_valid_session, London/NY overlap 07:00-16:00 UTC)
Dynamic Upgrade 2: Volatility Expansion Filter (Current 14-ATR > 1.2 * 24-period Avg ATR)
Dynamic Upgrade 3: Breakeven + Trailing Stop Logic (update_trailing_stop, 1.5x ATR, 20 EMA / 1x ATR)
"""

import os
import logging
import pytest
import numpy as np
import pandas as pd
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta, time as dt_time

from src.scanner import (
    MarketScanner,
    check_gap_risk,
    check_whale_footprint,
    calculate_adx,
    apply_regime_gate,
    is_valid_session,
    calculate_volatility_filter,
)
from src.macro_fetcher import MacroFetcher
from src.scorer import TradeScorer, filter_short_by_ema
from src.db_manager import SupabaseManager
from src.trigger_poller import TriggerPoller
from src.paper_executor import PaperExecutor, calculate_position_size


@pytest.fixture
def sample_ohlcv():
    """Generates synthetic 1H OHLCV dataframe with 50 bars and volatility expansion on the final bars."""
    dates = pd.date_range("2026-10-01", periods=60, freq="1h")
    np.random.seed(42)
    close = 50000.0 + np.cumsum(np.random.randn(60) * 100)
    high = close + np.random.uniform(50, 100, 60)
    low = close - np.random.uniform(50, 100, 60)
    open_p = close + np.random.uniform(-30, 30, 60)

    # Inject volatility expansion on the last 5 bars (ATR expands > 1.5x normal)
    for i in range(-5, 0):
        high[i] = close[i] + 350.0
        low[i] = close[i] - 350.0

    volume = np.random.uniform(10, 100, 60)
    timestamps = [int(d.timestamp() * 1000) for d in dates]

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume
    })


@pytest.fixture
def mock_fetcher(sample_ohlcv):
    fetcher = MagicMock()
    fetcher.fetch_ohlcv.return_value = sample_ohlcv
    fetcher.fetch_funding_rate_history.return_value = [
        {"fundingRate": "0.0001", "timestamp": 1729000000000 + i * 3600000} for i in range(100)
    ]
    fetcher.fetch_open_interest_history.return_value = [
        {"openInterestAmount": 1000.0},
        {"openInterestAmount": 1030.0}  # +3% delta
    ]
    fetcher.fetch_taker_buy_ratio.return_value = 0.70  # Aggressive buying
    return fetcher


class TestQuantUpgrades:

    # --------------------------------------------------------------------------
    # Task 1: Whale Detection (Order Flow Imbalance)
    # --------------------------------------------------------------------------
    def test_check_whale_footprint_function_buying(self):
        """check_whale_footprint: OI delta > 2.5% and taker buy ratio > 0.65 -> AGGRESSIVE_BUYING."""
        res = check_whale_footprint(oi_delta=0.03, taker_buy_ratio=0.72)
        assert res is not None
        assert res["signal_type"] == "WHALE_FLOW_DETECTED"
        assert res["direction"] == "LONG"
        assert res["flow_type"] == "AGGRESSIVE_BUYING"
        assert res["score"] == 0.72

    def test_check_whale_footprint_function_selling(self):
        """check_whale_footprint: OI delta > 2.5% and taker buy ratio < 0.35 -> AGGRESSIVE_SELLING."""
        res = check_whale_footprint(oi_delta=0.04, taker_buy_ratio=0.28)
        assert res is not None
        assert res["signal_type"] == "WHALE_FLOW_DETECTED"
        assert res["direction"] == "SHORT"
        assert res["flow_type"] == "AGGRESSIVE_SELLING"
        assert res["score"] == 0.72

    def test_check_whale_footprint_neutral_rejected(self):
        """check_whale_footprint returns None when thresholds not met."""
        assert check_whale_footprint(oi_delta=0.01, taker_buy_ratio=0.75) is None
        assert check_whale_footprint(oi_delta=0.04, taker_buy_ratio=0.50) is None

    def test_whale_detection_scanner_integration(self, mock_fetcher):
        """MarketScanner detects and logs WHALE_FLOW_DETECTED during valid session."""
        mock_db = MagicMock()
        scanner = MarketScanner(symbols=["BTC/USDT:USDT"], db_manager=mock_db)
        scanner.fetcher = mock_fetcher

        with patch.object(scanner, "is_valid_session", return_value=True):
            with patch.object(scanner, "calculate_adx", return_value=18.0):
                scanner.scan_symbol("BTC/USDT:USDT")

                calls = [c[1]["signal_type"] for c in mock_db.log_signal.call_args_list]
                assert "WHALE_FLOW_DETECTED" in calls

    # --------------------------------------------------------------------------
    # Task 2: Regime Detection (ADX Gate)
    # --------------------------------------------------------------------------
    def test_calculate_adx_computation(self, sample_ohlcv):
        """calculate_adx correctly computes 14-period ADX on pandas DataFrame."""
        adx_val = calculate_adx(sample_ohlcv, period=14)
        assert isinstance(adx_val, float)
        assert 0.0 <= adx_val <= 100.0

    def test_apply_regime_gate_ranging(self):
        """ADX < 20 only allows MEAN_REVERSION signals."""
        adx = 18.0
        assert apply_regime_gate("MEAN_REVERSION", adx) == "MEAN_REVERSION"
        assert apply_regime_gate({"signal_type": "MEAN_REVERSION"}, adx) == {"signal_type": "MEAN_REVERSION"}
        assert apply_regime_gate("CASCADE", adx) is None
        assert apply_regime_gate("BREAKOUT", adx) is None

    def test_apply_regime_gate_trending(self):
        """ADX > 25 only allows CASCADE or BREAKOUT signals."""
        adx = 28.0
        assert apply_regime_gate("CASCADE", adx) == "CASCADE"
        assert apply_regime_gate("BREAKOUT", adx) == "BREAKOUT"
        assert apply_regime_gate({"signal_type": "CASCADE_CONTINUATION"}, adx) == {"signal_type": "CASCADE_CONTINUATION"}
        assert apply_regime_gate("MEAN_REVERSION", adx) is None

    def test_apply_regime_gate_choppy(self):
        """ADX in [20, 25] rejects all signals."""
        for adx in [20.0, 22.5, 25.0]:
            assert apply_regime_gate("MEAN_REVERSION", adx) is None
            assert apply_regime_gate("CASCADE", adx) is None
            assert apply_regime_gate("BREAKOUT", adx) is None

    def test_scanner_adx_choppy_regime(self, mock_fetcher):
        """Scanner rejects all standard signals when ADX is between 20 and 25."""
        mock_db = MagicMock()
        scanner = MarketScanner(symbols=["BTC/USDT:USDT"], db_manager=mock_db)
        scanner.fetcher = mock_fetcher

        with patch.object(scanner, "is_valid_session", return_value=True):
            with patch.object(scanner, "calculate_adx", return_value=22.5):
                sig = scanner.scan_symbol("BTC/USDT:USDT")
                assert sig is None

    # --------------------------------------------------------------------------
    # Task 3: Trend Filter (filter_short_by_ema, 4H EMA 200)
    # --------------------------------------------------------------------------
    def test_filter_short_by_ema_rejects_above_200(self):
        """filter_short_by_ema rejects SHORT if current price > 4H 200 EMA."""
        mock_df = pd.DataFrame({"close": [50000.0] * 250})
        mock_data_fetcher = MagicMock()
        mock_data_fetcher.fetch_ohlcv.return_value = mock_df

        plan = {
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 52000.0,
        }

        res = filter_short_by_ema(plan, "BTC/USDT:USDT", data_fetcher=mock_data_fetcher)
        assert res is None
        mock_data_fetcher.fetch_ohlcv.assert_called_with("BTC/USDT:USDT", timeframe="4h", limit=250, use_cache=True)

    def test_filter_short_by_ema_approves_below_200(self):
        """filter_short_by_ema approves SHORT if current price <= 4H 200 EMA."""
        mock_df = pd.DataFrame({"close": [55000.0] * 250})
        mock_data_fetcher = MagicMock()
        mock_data_fetcher.fetch_ohlcv.return_value = mock_df

        plan = {
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 52000.0,
        }

        res = filter_short_by_ema(plan, "BTC/USDT:USDT", data_fetcher=mock_data_fetcher)
        assert res is not None
        assert res["direction"] == "SHORT"
        assert res["entry_price"] == 52000.0

    def test_filter_short_by_ema_long_unaffected(self):
        """filter_short_by_ema does not reject LONG plans."""
        mock_data_fetcher = MagicMock()
        plan = {
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 52000.0,
        }
        res = filter_short_by_ema(plan, "BTC/USDT:USDT", data_fetcher=mock_data_fetcher)
        assert res == plan
        mock_data_fetcher.fetch_ohlcv.assert_not_called()

    # --------------------------------------------------------------------------
    # Task 4: Cool-Down Timer (cool_down_until, set_halt +4h, TriggerPoller)
    # --------------------------------------------------------------------------
    def test_set_halt_sets_cool_down_four_hours(self):
        """set_halt() sets cool_down_until to now + 4 hours."""
        mock_client = MagicMock()
        with patch("supabase.create_client", return_value=mock_client):
            mgr = SupabaseManager(url="https://mock.supabase.co", key="key")

            res = mgr.set_halt("Risk breaker tripped")
            assert "cool_down_until" in res
            cool_dt = datetime.fromisoformat(res["cool_down_until"])
            now = datetime.now(timezone.utc)
            diff_hours = (cool_dt - now).total_seconds() / 3600.0
            assert pytest.approx(diff_hours, abs=0.1) == 4.0

    def test_trigger_poller_respects_cool_down(self):
        """When cool_down_until is active, TriggerPoller ignores all plans."""
        mock_db = MagicMock()
        mock_db.check_halt.return_value = False
        future_cooldown = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        mock_db.get_cool_down_until.return_value = future_cooldown

        poller = TriggerPoller(db_manager=mock_db)
        res = poller.poll_once()
        assert res == {"evaluated": 0, "triggered": 0, "timeout": 0}

        plan = {
            "id": "plan-cool-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 68000.0,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        eval_res = poller.evaluate_plan(plan)
        assert eval_res == "COOL_DOWN"

    # --------------------------------------------------------------------------
    # Task 5: Honest Paper Fills (Slippage Simulation)
    # --------------------------------------------------------------------------
    def test_paper_executor_slippage_simulation_long(self):
        """Long fill price = entry_price * (1 + slippage), slippage in [0, 0.001]."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"plan_metadata": {}}
        ]
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"id": "plan-slip-long", "status": "FILLED"}
        ]

        executor = PaperExecutor(db_manager=mock_db)
        plan = {
            "id": "plan-slip-long",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "plan_metadata": {
                "triggered_at": datetime.now(timezone.utc).isoformat()
            }
        }

        with patch("random.uniform", return_value=0.0008):
            res = executor.fill_triggered_plan(plan)
            assert res is True
            expected_fill = 50000.0 * 1.0008
            signal_call = mock_db.log_signal.call_args[1]
            assert pytest.approx(signal_call["metadata"]["fill_price"], 0.01) == expected_fill

    def test_paper_executor_slippage_simulation_short(self):
        """Short fill price = entry_price * (1 - slippage), slippage in [0, 0.001]."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"plan_metadata": {}}
        ]
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"id": "plan-slip-short", "status": "FILLED"}
        ]

        executor = PaperExecutor(db_manager=mock_db)
        plan = {
            "id": "plan-slip-short",
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "plan_metadata": {
                "triggered_at": datetime.now(timezone.utc).isoformat()
            }
        }

        with patch("random.uniform", return_value=0.0005):
            res = executor.fill_triggered_plan(plan)
            assert res is True
            expected_fill = 50000.0 * (1.0 - 0.0005)
            signal_call = mock_db.log_signal.call_args[1]
            assert pytest.approx(signal_call["metadata"]["fill_price"], 0.01) == expected_fill

    # --------------------------------------------------------------------------
    # Dynamic Upgrade 1: Session Awareness (The Time Gate)
    # --------------------------------------------------------------------------
    def test_is_valid_session_london_ny_overlap(self):
        """is_valid_session returns True only between 07:00 and 16:00 UTC."""
        t1 = datetime(2026, 10, 19, 7, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 19, 11, 30, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 10, 19, 16, 0, 0, tzinfo=timezone.utc)
        assert is_valid_session(t1) is True
        assert is_valid_session(t2) is True
        assert is_valid_session(t3) is True

    def test_is_valid_session_asian_and_off_hours_rejected(self):
        """is_valid_session returns False outside 07:00-16:00 UTC (Asian session)."""
        t1 = datetime(2026, 10, 19, 2, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 19, 6, 59, 59, tzinfo=timezone.utc)
        t3 = datetime(2026, 10, 19, 16, 0, 1, tzinfo=timezone.utc)
        t4 = datetime(2026, 10, 19, 22, 15, 0, tzinfo=timezone.utc)
        assert is_valid_session(t1) is False
        assert is_valid_session(t2) is False
        assert is_valid_session(t3) is False
        assert is_valid_session(t4) is False

    def test_scanner_aborts_during_asian_session(self, mock_fetcher):
        """Scanner immediately aborts and returns None/empty if is_valid_session is False."""
        mock_db = MagicMock()
        scanner = MarketScanner(symbols=["BTC/USDT:USDT"], db_manager=mock_db)
        scanner.fetcher = mock_fetcher

        with patch.object(scanner, "is_valid_session", return_value=False):
            sig = scanner.scan_symbol("BTC/USDT:USDT")
            assert sig is None
            # scan_all also returns empty list
            assert scanner.scan_all() == []

    # --------------------------------------------------------------------------
    # Dynamic Upgrade 2: Volatility Expansion Filter
    # --------------------------------------------------------------------------
    def test_volatility_filter_expansion_true(self, sample_ohlcv):
        """Current 14-period ATR > 1.2 * 24-period Avg ATR evaluates to is_expanded=True."""
        curr_atr, avg_atr, is_expanded = calculate_volatility_filter(sample_ohlcv, atr_period=14, avg_period=24)
        assert curr_atr > 0
        assert avg_atr > 0
        assert curr_atr > 1.2 * avg_atr
        assert is_expanded is True

    def test_volatility_filter_flat_market_rejected(self):
        """Flat / dead market with Current ATR <= 1.2 * Avg ATR returns is_expanded=False."""
        dates = pd.date_range("2026-10-01", periods=60, freq="1h")
        # Totally flat ranges: high - low = 10.0 everywhere
        df_flat = pd.DataFrame({
            "timestamp": [int(d.timestamp() * 1000) for d in dates],
            "open": [50000.0] * 60,
            "high": [50005.0] * 60,
            "low": [49995.0] * 60,
            "close": [50000.0] * 60,
            "volume": [50.0] * 60
        })
        curr_atr, avg_atr, is_expanded = calculate_volatility_filter(df_flat, atr_period=14, avg_period=24)
        assert is_expanded is False  # 10.0 is not > 1.2 * 10.0

    def test_scanner_volatility_expansion_gate(self, mock_fetcher):
        """Scanner rejects signals when volatility filter fails (market is dead)."""
        mock_db = MagicMock()
        scanner = MarketScanner(symbols=["BTC/USDT:USDT"], db_manager=mock_db)
        scanner.fetcher = mock_fetcher

        with patch.object(scanner, "is_valid_session", return_value=True):
            with patch.object(scanner, "calculate_volatility_filter", return_value=(10.0, 10.0, False)):
                sig = scanner.scan_symbol("BTC/USDT:USDT")
                assert sig is None

    # --------------------------------------------------------------------------
    # Dynamic Upgrade 3: Breakeven + Trailing Stop Logic
    # --------------------------------------------------------------------------
    def test_trailing_stop_breakeven_long(self):
        """LONG plan: when price moves >= 1.5x ATR in our favor, stop moved to entry price."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-trail-long-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 48500.0,
            "atr": 1000.0,
            "plan_metadata": {"atr": 1000.0}
        }

        # Price moves exactly 1.5x ATR in our favor: 50000 + 1500 = 51500.0
        # 1x ATR trailing stop = 51500 - 1000 = 50500.0 (greater than entry_price 50000.0)
        res = executor.update_trailing_stop(plan, current_price=51500.0, ema_20=50000.0)
        assert res is not None
        assert res["is_breakeven_reached"] is True
        assert res["new_stop_loss"] >= 50000.0  # Stop raised to at least breakeven
        assert res["new_stop_loss"] == 50500.0  # 1x ATR trailing stop applied

        # Verify DB update called
        mock_db.client.table.assert_called_with("trade_plans")

    def test_trailing_stop_breakeven_short(self):
        """SHORT plan: when price moves >= 1.5x ATR in our favor, stop moved to entry price."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-trail-short-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "stop_loss": 51500.0,
            "atr": 1000.0,
            "plan_metadata": {"atr": 1000.0}
        }

        # Price drops 1.5x ATR in our favor: 50000 - 1500 = 48500.0
        # 1x ATR trailing stop = 48500 + 1000 = 49500.0 (less than entry_price 50000.0)
        res = executor.update_trailing_stop(plan, current_price=48500.0, ema_20=50000.0)
        assert res is not None
        assert res["is_breakeven_reached"] is True
        assert res["new_stop_loss"] <= 50000.0  # Stop lowered to at least breakeven
        assert res["new_stop_loss"] == 49500.0  # 1x ATR trailing stop applied

    def test_trailing_stop_no_move_before_threshold(self):
        """When favorable price move is < 1.5x ATR, stop loss is not altered."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-trail-short-no-move",
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "stop_loss": 51500.0,
            "atr": 1000.0,
            "plan_metadata": {"atr": 1000.0}
        }

        # Price moves only 0.5x ATR in favor (49500 vs 50000)
        res = executor.update_trailing_stop(plan, current_price=49500.0, ema_20=50000.0)
        assert res["is_breakeven_reached"] is False
        assert res["new_stop_loss"] == 51500.0  # Original stop preserved

    def test_trailing_stop_20_ema_trail(self):
        """Once in profit, stop loss trails behind 1H 20 EMA if tighter."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-trail-ema",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 48500.0,
            "atr": 1000.0,
            "plan_metadata": {"atr": 1000.0}
        }

        # Price at 53000 (favorable move 3000 = 3x ATR)
        # 1x ATR trailing = 52000
        # 20 EMA is at 52500 (even tighter trailing)
        res = executor.update_trailing_stop(plan, current_price=53000.0, ema_20=52500.0)
        assert res["is_breakeven_reached"] is True
        assert res["new_stop_loss"] == 52500.0
        assert res["trailing_reason"] == "1H 20-EMA Trailing Stop"

    # --------------------------------------------------------------------------
    # Hybrid Multi-Timeframe Architecture Tests
    # --------------------------------------------------------------------------
    def test_config_multi_timeframe_constants(self):
        """Verify config has TIMEFRAME='1h' and MACRO_TIMEFRAME='4h'."""
        from src.config import config, TIMEFRAME, MACRO_TIMEFRAME
        assert TIMEFRAME == "1h"
        assert MACRO_TIMEFRAME == "4h"
        assert config.TIMEFRAME == "1h"
        assert config.MACRO_TIMEFRAME == "4h"

    def test_scanner_uses_1h_timeframe_for_ohlcv_and_oi(self, mock_fetcher):
        """Verify MarketScanner calls fetch_ohlcv and fetch_open_interest_history with 1h."""
        mock_db = MagicMock()
        scanner = MarketScanner(symbols=["BTC/USDT:USDT"], db_manager=mock_db)
        scanner.fetcher = mock_fetcher

        with patch.object(scanner, "is_valid_session", return_value=True):
            with patch.object(scanner, "calculate_adx", return_value=15.0):
                sig = scanner.scan_symbol("BTC/USDT:USDT")
                assert scanner.TIMEFRAME == "1h"
                mock_fetcher.fetch_ohlcv.assert_called_with("BTC/USDT:USDT", timeframe="1h", limit=50, use_cache=False)
                mock_fetcher.fetch_open_interest_history.assert_called_with("BTC/USDT:USDT", timeframe="1h", limit=5)
                assert sig is not None
                assert sig["metadata"]["timeframe"] == "1h"

    def test_scorer_trend_filter_fetches_macro_4h_timeframe(self):
        """Verify TradeScorer fetches 4h MACRO_TIMEFRAME for 200 EMA check on 1h signal."""
        mock_df = pd.DataFrame({"close": [50000.0] * 250})
        mock_data_fetcher = MagicMock()
        mock_data_fetcher.fetch_ohlcv.return_value = mock_df

        scorer = TradeScorer(data_fetcher=mock_data_fetcher)
        signal = {
            "symbol": "BTC/USDT:USDT",
            "score": 85.0,
            "metadata": {
                "close": 52000.0,
                "atr": 1000.0,
                "direction": "SHORT",
                "timeframe": "1h"
            }
        }
        mock_macro = MagicMock()
        mock_macro.get_macro_risk.return_value = {"score": 10, "is_valid": True}
        mock_macro.get_funding_and_oi.return_value = {"funding_rate": 0.0001, "oi_change_24h_pct": 5.0}

        scorer.calculate_trade_plan(signal, macro_fetcher=mock_macro)
        mock_data_fetcher.fetch_ohlcv.assert_called_with("BTC/USDT:USDT", timeframe="4h", limit=250, use_cache=True)

    # --------------------------------------------------------------------------
    # Task 1 & 2: Config & Dynamic Position Sizing (1% Account Risk)
    # --------------------------------------------------------------------------
    def test_config_risk_and_trailing_parameters(self):
        """Verify config has 1% risk, 1.5 stop multiplier, and 3.0 trail multiplier."""
        from src.config import (
            config,
            RISK_PER_TRADE_PERCENT,
            ATR_MULTIPLIER_FOR_STOP,
            ATR_MULTIPLIER_FOR_TRAIL,
        )
        assert RISK_PER_TRADE_PERCENT == 0.01
        assert ATR_MULTIPLIER_FOR_STOP == 1.5
        assert ATR_MULTIPLIER_FOR_TRAIL == 3.0
        assert config.RISK_PER_TRADE_PERCENT == 0.01
        assert config.ATR_MULTIPLIER_FOR_STOP == 1.5
        assert config.ATR_MULTIPLIER_FOR_TRAIL == 3.0

    def test_calculate_position_size_exact_one_percent_risk(self):
        """Verify risk amount is exactly 1% and formula accounts for 2% fee buffer & step size."""
        account_balance = 100000.0
        entry_price = 50000.0

        for atr in [250.0, 500.0, 1000.0, 2500.0]:
            res = calculate_position_size(account_balance, entry_price, atr_value=atr)
            units, total_value = res
            # Risk budget must be exactly 1% of balance ($1,000)
            expected_risk = account_balance * 0.01
            assert pytest.approx(res.risk_amount, 0.01) == expected_risk
            # Stop distance = 1.5 * atr
            stop_distance = atr * 1.5
            raw_qty = expected_risk / stop_distance
            fee_adj_qty = raw_qty * 0.98
            assert pytest.approx(res.raw_quantity, 0.001) == raw_qty
            assert pytest.approx(res.fee_adjusted_quantity, 0.001) == fee_adj_qty
            # Units rounded down to step size
            assert units <= fee_adj_qty
            assert pytest.approx(units * stop_distance, 1.0) == fee_adj_qty * stop_distance
            # Total value equals units * entry_price
            assert pytest.approx(total_value, 0.01) == units * entry_price

    def test_fill_triggered_plan_logs_position_size_and_risk(self):
        """fill_triggered_plan calculates and logs position size and risk amount in metadata."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"plan_metadata": {}}
        ]
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"id": "plan-size-1", "status": "FILLED"}
        ]

        executor = PaperExecutor(db_manager=mock_db)
        plan = {
            "id": "plan-size-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "atr": 1000.0,
            "plan_metadata": {
                "triggered_at": datetime.now(timezone.utc).isoformat(),
                "atr": 1000.0,
            }
        }

        res = executor.fill_triggered_plan(plan)
        assert res is True
        # Check update payload has position size and risk amount
        update_call = mock_table.update.call_args[0][0]
        meta = update_call["plan_metadata"]
        assert "position_size" in meta
        assert "risk_amount" in meta
        assert pytest.approx(meta["risk_amount"], 0.01) == 100000.0 * 0.01  # $1,000
        # Stop distance = 1000 * 1.5 = 1500, units = round_down(1000 / 1500 * 0.98, 0.001) = 0.653
        assert pytest.approx(meta["position_size"], 0.001) == 0.653
        assert pytest.approx(meta["raw_quantity"], 0.001) == 1000.0 / 1500.0
        assert pytest.approx(meta["fee_adjusted_quantity"], 0.001) == (1000.0 / 1500.0) * 0.98

    # --------------------------------------------------------------------------
    # Task 3: Structure-Based Breakeven & Chandelier Trailing Stops
    # --------------------------------------------------------------------------
    def test_structure_based_breakeven_long(self):
        """LONG: If current_price > recent swing high, stop moves to entry_price * 1.001."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-be-long",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 48500.0,
            "atr": 500.0,
            "plan_metadata": {"atr": 500.0}
        }

        # Swing high = 51000.0, current price breaks out to 51200.0
        res = executor.update_trailing_stop(
            plan,
            current_price=51200.0,
            atr_value=500.0,
            recent_highs=[50500.0, 50800.0, 51000.0],
            recent_lows=[49500.0, 49800.0]
        )
        assert res is not None
        assert res["closed"] is False
        assert res["is_breakeven_reached"] is True
        # Stop set to at least entry * 1.001 (50000 * 1.001 = 50050.0)
        expected_be = 50000.0 * 1.001
        assert res["new_stop_loss"] >= expected_be

    def test_structure_based_breakeven_short(self):
        """SHORT: If current_price < recent swing low, stop moves to entry_price * 0.999."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-be-short",
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "stop_loss": 51500.0,
            "atr": 500.0,
            "plan_metadata": {"atr": 500.0}
        }

        # Swing low = 49000.0, current price breaks below to 48800.0
        res = executor.update_trailing_stop(
            plan,
            current_price=48800.0,
            atr_value=500.0,
            recent_highs=[50500.0, 50800.0],
            recent_lows=[49800.0, 49500.0, 49000.0]
        )
        assert res is not None
        assert res["closed"] is False
        assert res["is_breakeven_reached"] is True
        # Stop set to at least entry * 0.999 (50000 * 0.999 = 49950.0)
        expected_be = 50000.0 * 0.999
        assert res["new_stop_loss"] <= expected_be

    def test_chandelier_trailing_stop_long_ratchet_up_only(self):
        """LONG: Chandelier stop = max(recent_highs) - (3.0 * atr). Stop only moves UP, never down."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-chan-long",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 50050.0,  # Already at breakeven
            "atr": 1000.0,
            "plan_metadata": {"is_breakeven": True, "atr": 1000.0}
        }

        # Price rallies to peak 55000.0
        # Chandelier stop = 55000 - (3.0 * 1000) = 52000.0
        res = executor.update_trailing_stop(
            plan,
            current_price=54500.0,
            atr_value=1000.0,
            recent_highs=[52000.0, 53500.0, 55000.0],
            recent_lows=[49000.0]
        )
        assert res["new_stop_loss"] == 52000.0

        # Now market retraces to 52500, recent highs drop to [52500]
        plan["stop_loss"] = 52000.0
        res_dip = executor.update_trailing_stop(
            plan,
            current_price=52500.0,
            atr_value=1000.0,
            recent_highs=[52500.0],
            recent_lows=[51500.0]
        )
        # Stop loss MUST NOT move down, remains 52000.0
        assert res_dip["new_stop_loss"] == 52000.0

    def test_chandelier_trailing_stop_short_ratchet_down_only(self):
        """SHORT: Chandelier stop = min(recent_lows) + (3.0 * atr). Stop only moves DOWN, never up."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-chan-short",
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "stop_loss": 49950.0,  # Already at breakeven
            "atr": 1000.0,
            "plan_metadata": {"is_breakeven": True, "atr": 1000.0}
        }

        # Price dumps to trough 45000.0
        # Chandelier stop = 45000 + (3.0 * 1000) = 48000.0
        res = executor.update_trailing_stop(
            plan,
            current_price=45500.0,
            atr_value=1000.0,
            recent_highs=[50000.0],
            recent_lows=[47500.0, 46000.0, 45000.0]
        )
        assert res["new_stop_loss"] == 48000.0

        # Now market bounces to 47500
        plan["stop_loss"] = 48000.0
        res_bounce = executor.update_trailing_stop(
            plan,
            current_price=47500.0,
            atr_value=1000.0,
            recent_highs=[48500.0],
            recent_lows=[47000.0]
        )
        # Stop loss MUST NOT move up, remains 48000.0
        assert res_bounce["new_stop_loss"] == 48000.0

    # --------------------------------------------------------------------------
    # Task 4: The Trailing Stop Monitor (PAPER_EXIT with exit_reason="TRAILING_STOP")
    # --------------------------------------------------------------------------
    def test_trailing_stop_monitor_closes_trade_long(self):
        """LONG: If current_price <= stop_loss, close trade and log PAPER_EXIT with exit_reason='TRAILING_STOP'."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-exit-long",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 52000.0,  # Trailed stop at 52,000
            "take_profit": 56000.0,
            "atr": 1000.0,
            "plan_metadata": {"is_breakeven": True}
        }

        # Market price drops to 51950.0 (<= 52000.0 stop_loss)
        res = executor.update_trailing_stop(plan, current_price=51950.0)
        assert res["closed"] is True
        assert res["exit_reason"] == "TRAILING_STOP"
        assert res["exit_price"] == 51950.0

        # Verify trade_plans status updated to CLOSED
        mock_table = mock_db.client.table.return_value
        status_update = mock_table.update.call_args[0][0]
        assert status_update["status"] == "CLOSED"

        # Verify PAPER_EXIT signal logged to signals table
        exit_signal = mock_db.log_signal.call_args[1]
        assert exit_signal["signal_type"] == "PAPER_EXIT"
        assert exit_signal["metadata"]["exit_reason"] == "TRAILING_STOP"
        assert exit_signal["metadata"]["exit_price"] == 51950.0

    def test_trailing_stop_monitor_closes_trade_short(self):
        """SHORT: If current_price >= stop_loss, close trade and log PAPER_EXIT with exit_reason='TRAILING_STOP'."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-exit-short",
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "stop_loss": 48000.0,  # Trailed stop at 48,000
            "take_profit": 44000.0,
            "atr": 1000.0,
            "plan_metadata": {"is_breakeven": True}
        }

        # Market price bounces to 48050.0 (>= 48000.0 stop_loss)
        res = executor.update_trailing_stop(plan, current_price=48050.0)
        assert res["closed"] is True
        assert res["exit_reason"] == "TRAILING_STOP"
        assert res["exit_price"] == 48050.0

        # Verify PAPER_EXIT signal logged to signals table
        exit_signal = mock_db.log_signal.call_args[1]
        assert exit_signal["signal_type"] == "PAPER_EXIT"
        assert exit_signal["metadata"]["exit_reason"] == "TRAILING_STOP"
        assert exit_signal["metadata"]["exit_price"] == 48050.0

    # --------------------------------------------------------------------------
    # Task 1, 2, 3: Gap Risk Filter (check_gap_risk, 1H OHLCV, 14-period ATR)
    # --------------------------------------------------------------------------
    def test_check_gap_risk_massive_gap(self):
        """Massive gap: prev_close=100, current_open=98, ATR=1.5. Assert check_gap_risk returns True."""
        mock_df = pd.DataFrame([
            {"timestamp": 1729000000000, "open": 99.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100, "atr": 1.5},
            {"timestamp": 1729003600000, "open": 98.0, "high": 99.0, "low": 97.5, "close": 98.5, "volume": 100, "atr": 1.5},
        ])
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_ohlcv.return_value = mock_df
        mock_fetcher.fetch_atr.return_value = 1.5

        # Direct function call with mock fetcher
        assert check_gap_risk("BTC/USDT:USDT", fetcher=mock_fetcher) is True

        # Call with patched DataFetcher
        with patch("src.scanner.DataFetcher", return_value=mock_fetcher):
            assert check_gap_risk("BTC/USDT:USDT") is True

    def test_check_gap_risk_normal_gap(self):
        """Normal gap: prev_close=100, current_open=100.1, ATR=1.5. Assert check_gap_risk returns False."""
        mock_df = pd.DataFrame([
            {"timestamp": 1729000000000, "open": 99.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100, "atr": 1.5},
            {"timestamp": 1729003600000, "open": 100.1, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 100, "atr": 1.5},
        ])
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_ohlcv.return_value = mock_df
        mock_fetcher.fetch_atr.return_value = 1.5

        # Direct function call with mock fetcher
        assert check_gap_risk("BTC/USDT:USDT", fetcher=mock_fetcher) is False

        # Call with patched DataFetcher
        with patch("src.scanner.DataFetcher", return_value=mock_fetcher):
            assert check_gap_risk("BTC/USDT:USDT") is False

    def test_scanner_skips_signal_on_high_gap_risk(self, mock_fetcher, caplog):
        """Scanner logs warning and skips signal generation when check_gap_risk is True."""
        mock_db = MagicMock()
        scanner = MarketScanner(symbols=["BTC/USDT:USDT"], db_manager=mock_db)
        scanner.fetcher = mock_fetcher

        with patch.object(scanner, "is_valid_session", return_value=True):
            with patch.object(scanner, "check_gap_risk", return_value=True):
                with caplog.at_level(logging.WARNING):
                    sig = scanner.scan_symbol("BTC/USDT:USDT")
                    assert sig is None
                    assert "High Gap Risk detected on BTC/USDT:USDT. Skipping signal." in caplog.text

    def test_scanner_scan_all_skips_high_gap_risk(self, mock_fetcher, caplog):
        """scan_all skips high gap risk coins and logs warning."""
        mock_db = MagicMock()
        scanner = MarketScanner(symbols=["BTC/USDT:USDT", "ETH/USDT:USDT"], db_manager=mock_db)
        scanner.fetcher = mock_fetcher

        def gap_risk_mock(sym, **kwargs):
            return sym == "BTC/USDT:USDT"

        with patch.object(scanner, "is_valid_session", return_value=True):
            with patch.object(scanner, "check_gap_risk", side_effect=gap_risk_mock):
                with patch.object(scanner, "scan_symbol", return_value={"symbol": "ETH/USDT:USDT", "signal_type": "MEAN_REVERSION"}):
                    with caplog.at_level(logging.WARNING):
                        results = scanner.scan_all()
                        assert len(results) == 1
                        assert results[0]["symbol"] == "ETH/USDT:USDT"
                        assert "High Gap Risk detected on BTC/USDT:USDT. Skipping signal." in caplog.text

    # --------------------------------------------------------------------------
    # Relative Strength Rotation Filter (All Coin Theory)
    # --------------------------------------------------------------------------
    def test_macro_fetcher_eth_btc_trend_altseason(self):
        """ETH/BTC 50 EMA > 200 EMA returns 'ALTSEASON'."""
        mock_exchange = MagicMock()
        # Rising prices from 0.05 to 0.08
        closes = [0.05 + (i * 0.00015) for i in range(250)]
        mock_ohlcv = [[1729000000000 + i * 14400000, c, c + 0.001, c - 0.001, c, 100] for i, c in enumerate(closes)]
        mock_exchange.fetch_ohlcv.return_value = mock_ohlcv

        fetcher = MacroFetcher(exchange=mock_exchange)
        trend = fetcher.get_eth_btc_trend()
        assert trend == "ALTSEASON"
        mock_exchange.fetch_ohlcv.assert_called_with("ETH/BTC", timeframe="4h", limit=250)

    def test_macro_fetcher_eth_btc_trend_btc_dominance(self):
        """ETH/BTC 50 EMA < 200 EMA returns 'BTC_DOMINANCE'."""
        mock_exchange = MagicMock()
        # Falling prices from 0.08 down to 0.04
        closes = [0.08 - (i * 0.0002) for i in range(250)]
        mock_ohlcv = [[1729000000000 + i * 14400000, c, c + 0.001, c - 0.001, c, 100] for i, c in enumerate(closes)]
        mock_exchange.fetch_ohlcv.return_value = mock_ohlcv

        fetcher = MacroFetcher(exchange=mock_exchange)
        trend = fetcher.get_eth_btc_trend()
        assert trend == "BTC_DOMINANCE"

    def test_rotation_filter_rejects_eth_approves_btc_during_btc_dominance(self):
        """Mock ETH/BTC trend to 'BTC_DOMINANCE'. Verify ETH Long rejected, BTC Long approved."""
        mock_macro = MagicMock()
        mock_macro.get_eth_btc_trend.return_value = "BTC_DOMINANCE"
        mock_macro.get_macro_risk.return_value = {"score": 10, "is_valid": True, "reason": "Risk On"}
        mock_macro.get_funding_and_oi.return_value = {"funding_rate": 0.0001, "oi_change_24h_pct": 5.0}

        scorer = TradeScorer(macro_fetcher=mock_macro)

        eth_long_signal = {
            "id": "sig-eth-long",
            "symbol": "ETH/USDT:USDT",
            "score": 85.0,
            "event_time": "2026-10-19T06:00:00Z",
            "metadata": {
                "close": 3000.0,
                "atr": 50.0,
                "direction": "LONG",
            }
        }

        btc_long_signal = {
            "id": "sig-btc-long",
            "symbol": "BTC/USDT:USDT",
            "score": 85.0,
            "event_time": "2026-10-19T06:00:00Z",
            "metadata": {
                "close": 60000.0,
                "atr": 1000.0,
                "direction": "LONG",
            }
        }

        # 1. ETH Long must be rejected during BTC_DOMINANCE
        eth_plan = scorer.calculate_trade_plan(eth_long_signal, macro_fetcher=mock_macro)
        assert eth_plan is None

        # 2. BTC Long must be approved during BTC_DOMINANCE
        btc_plan = scorer.calculate_trade_plan(btc_long_signal, macro_fetcher=mock_macro)
        assert btc_plan is not None
        assert btc_plan["symbol"] == "BTC/USDT:USDT"
        assert btc_plan["status"] == "PENDING"
        # 3. Log rotation regime in plan_metadata
        assert btc_plan["plan_metadata"]["rotation_regime"] == "BTC_DOMINANCE"

    def test_rotation_filter_allows_altcoin_during_altseason(self):
        """Mock ETH/BTC trend to 'ALTSEASON'. Verify ETH Long is approved and logs ALTSEASON."""
        mock_macro = MagicMock()
        mock_macro.get_eth_btc_trend.return_value = "ALTSEASON"
        mock_macro.get_macro_risk.return_value = {"score": 10, "is_valid": True, "reason": "Risk On"}
        mock_macro.get_funding_and_oi.return_value = {"funding_rate": 0.0001, "oi_change_24h_pct": 5.0}

        scorer = TradeScorer(macro_fetcher=mock_macro)

        eth_long_signal = {
            "id": "sig-eth-altseason",
            "symbol": "ETH/USDT:USDT",
            "score": 85.0,
            "event_time": "2026-10-19T06:00:00Z",
            "metadata": {
                "close": 3000.0,
                "atr": 50.0,
                "direction": "LONG",
            }
        }

        eth_plan = scorer.calculate_trade_plan(eth_long_signal, macro_fetcher=mock_macro)
        assert eth_plan is not None
        assert eth_plan["symbol"] == "ETH/USDT:USDT"
        assert eth_plan["status"] == "PENDING"
        assert eth_plan["plan_metadata"]["rotation_regime"] == "ALTSEASON"

    def test_calculate_confluence_score_direct_rotation_gate(self):
        """calculate_confluence_score enforces rotation filter directly."""
        mock_macro = MagicMock()
        mock_macro.get_eth_btc_trend.return_value = "BTC_DOMINANCE"
        macro_res = {"score": 10, "is_valid": True}
        deriv_res = {"funding_rate": 0.0001, "oi_change_24h_pct": 5.0}

        scorer = TradeScorer(macro_fetcher=mock_macro)

        # ETH during BTC_DOMINANCE -> Rejected
        approved, score, reason = scorer.calculate_confluence_score(
            tech_score=85.0,
            macro_result=macro_res,
            derivative_result=deriv_res,
            symbol="ETH/USDT:USDT",
            macro_fetcher=mock_macro,
        )
        assert approved is False
        assert "BTC_DOMINANCE" in reason

        # BTC during BTC_DOMINANCE -> Approved
        approved_btc, score_btc, reason_btc = scorer.calculate_confluence_score(
            tech_score=85.0,
            macro_result=macro_res,
            derivative_result=deriv_res,
            symbol="BTC/USDT:USDT",
            macro_fetcher=mock_macro,
        )
        assert approved_btc is True
        assert score_btc >= 70.0

    # --------------------------------------------------------------------------
    # Pydantic Schema Gate Tests (Validators, Scorer, PaperExecutor)
    # --------------------------------------------------------------------------
    def test_validators_trade_plan_valid(self):
        """TradePlan instantiates successfully for valid LONG and SHORT plans."""
        from src.validators import TradePlan

        p_long = TradePlan(
            symbol="BTC/USDT:USDT",
            direction="LONG",
            entry_price=68000.0,
            stop_loss=67000.0,
            take_profit=70000.0,
            position_size=1.5,
            atr=500.0,
        )
        assert p_long.symbol == "BTC/USDT:USDT"
        assert p_long.direction == "LONG"
        assert p_long.stop_loss < p_long.entry_price < p_long.take_profit

        p_short = TradePlan(
            symbol="ETH/USDT:USDT",
            direction="SHORT",
            entry_price=3500.0,
            stop_loss=3600.0,
            take_profit=3300.0,
            position_size=10.0,
            atr=50.0,
        )
        assert p_short.symbol == "ETH/USDT:USDT"
        assert p_short.direction == "SHORT"
        assert p_short.take_profit < p_short.entry_price < p_short.stop_loss

    def test_validators_trade_plan_invalid_stop_loss(self):
        """TradePlan rejects stop_loss >= entry for LONG and stop_loss <= entry for SHORT."""
        from src.validators import TradePlan, ValidationError

        # LONG with stop_loss >= entry
        with pytest.raises(ValidationError) as exc_info:
            TradePlan(
                symbol="BTC/USDT:USDT",
                direction="LONG",
                entry_price=50000.0,
                stop_loss=51000.0,
                take_profit=55000.0,
                position_size=1.0,
                atr=100.0,
            )
        assert "stop_loss" in str(exc_info.value)

        # SHORT with stop_loss <= entry
        with pytest.raises(ValidationError) as exc_info:
            TradePlan(
                symbol="BTC/USDT:USDT",
                direction="SHORT",
                entry_price=50000.0,
                stop_loss=49000.0,
                take_profit=45000.0,
                position_size=1.0,
                atr=100.0,
            )
        assert "stop_loss" in str(exc_info.value)

    def test_validators_trade_plan_invalid_take_profit(self):
        """TradePlan rejects take_profit <= entry for LONG and take_profit >= entry for SHORT."""
        from src.validators import TradePlan, ValidationError

        # LONG with take_profit <= entry
        with pytest.raises(ValidationError) as exc_info:
            TradePlan(
                symbol="BTC/USDT:USDT",
                direction="LONG",
                entry_price=50000.0,
                stop_loss=48000.0,
                take_profit=49000.0,
                position_size=1.0,
                atr=100.0,
            )
        assert "take_profit" in str(exc_info.value)

        # SHORT with take_profit >= entry
        with pytest.raises(ValidationError) as exc_info:
            TradePlan(
                symbol="BTC/USDT:USDT",
                direction="SHORT",
                entry_price=50000.0,
                stop_loss=52000.0,
                take_profit=53000.0,
                position_size=1.0,
                atr=100.0,
            )
        assert "take_profit" in str(exc_info.value)

    def test_validators_trade_plan_forbids_extra_fields(self):
        """TradePlan rejects unknown extra fields due to extra='forbid'."""
        from src.validators import TradePlan, ValidationError

        with pytest.raises(ValidationError) as exc_info:
            TradePlan(
                symbol="BTC/USDT:USDT",
                direction="LONG",
                entry_price=50000.0,
                stop_loss=48000.0,
                take_profit=54000.0,
                position_size=1.0,
                atr=100.0,
                unauthorized_extra_param="exploit",
            )
        assert "unauthorized_extra_param" in str(exc_info.value)

    def test_scorer_save_trade_plan_rejects_invalid_long_stop_loss(self):
        """Scorer rejects invalid plan (LONG with Stop Loss ABOVE entry) and does NOT save to Supabase."""
        mock_db = MagicMock()
        scorer = TradeScorer(db_manager=mock_db)

        invalid_plan = {
            "signal_id": "sig-invalid-sl-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 60000.0,
            "stop_loss": 62000.0,  # Invalid: Stop Loss ABOVE entry on LONG
            "take_profit": 65000.0,
            "position_size": 1.0,
            "atr": 500.0,
            "status": "PENDING",
        }

        saved = scorer.save_trade_plan(invalid_plan)
        assert saved is None
        # Verify db.client.table was never called for insertion
        mock_db.client.table.assert_not_called()

    def test_scorer_save_trade_plan_rejects_extra_unauthorized_fields(self):
        """Scorer rejects plans with unauthorized extra fields."""
        mock_db = MagicMock()
        scorer = TradeScorer(db_manager=mock_db)

        sneaky_plan = {
            "signal_id": "sig-sneaky-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 60000.0,
            "stop_loss": 58000.0,
            "take_profit": 65000.0,
            "position_size": 1.0,
            "atr": 500.0,
            "sneaky_injected_key": "malicious_payload",
            "status": "PENDING",
        }

        saved = scorer.save_trade_plan(sneaky_plan)
        assert saved is None
        mock_db.client.table.assert_not_called()

    def test_scorer_save_trade_plan_saves_valid_plan(self):
        """Scorer validates and saves well-formed plans to Supabase."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.eq.return_value.execute.return_value.data = []
        mock_table.insert.return_value.execute.return_value.data = [{"id": "plan-saved-1"}]

        scorer = TradeScorer(db_manager=mock_db)
        valid_plan = {
            "signal_id": "sig-valid-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 60000.0,
            "stop_loss": 58000.0,
            "take_profit": 65000.0,
            "position_size": 1.0,
            "atr": 500.0,
            "status": "PENDING",
        }

        saved = scorer.save_trade_plan(valid_plan)
        assert saved is not None
        mock_table.insert.assert_called_once()

    def test_paper_executor_rejects_invalid_schema_as_rejected_schema(self):
        """PaperExecutor rejects invalid schema plans, updates status to REJECTED_SCHEMA, and returns False."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        # Plan with LONG having Stop Loss ABOVE entry price
        invalid_plan = {
            "id": "plan-corrupted-db-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 55000.0,  # Invalid for LONG
            "take_profit": 60000.0,
            "status": "TRIGGERED",
        }

        with patch.object(executor, "update_plan_status", return_value=True) as mock_update:
            res = executor.fill_triggered_plan(invalid_plan)
            assert res is False
            mock_update.assert_called_once()
            call_args = mock_update.call_args[0]
            assert call_args[0] == "plan-corrupted-db-1"
            assert call_args[1] == "REJECTED_SCHEMA"
            mock_db.log_signal.assert_not_called()

    def test_paper_executor_rejects_extra_unauthorized_fields(self):
        """PaperExecutor rejects plans containing extra forbidden fields."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        rogue_plan = {
            "id": "plan-rogue-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "stop_loss": 52000.0,
            "take_profit": 48000.0,
            "status": "TRIGGERED",
            "sneaky_rogue_param": 999,
        }

        with patch.object(executor, "update_plan_status", return_value=True) as mock_update:
            res = executor.fill_triggered_plan(rogue_plan)
            assert res is False
            mock_update.assert_called_once()
            call_args = mock_update.call_args[0]
            assert call_args[0] == "plan-rogue-1"
            assert call_args[1] == "REJECTED_SCHEMA"

    # --------------------------------------------------------------------------
    # Quant Audit P0 Upgrades: Task 1 Position Sizing & Task 2 Exposure / Drawdown
    # --------------------------------------------------------------------------
    def test_calculate_position_size_strict_formula_with_stop_loss(self):
        """Task 1: Verify exact 4-step formula with stop loss and CCXT step rounding."""
        account_balance = 100000.0
        entry_price = 50000.0
        stop_loss = 48500.0  # Stop distance = 1500.0

        res = calculate_position_size(
            account_balance=account_balance,
            entry_price=entry_price,
            stop_loss=stop_loss,
            symbol="BTC/USDT:USDT"
        )
        # 1. Risk_Budget = Account_Balance * 0.01 = $1000.0
        assert res.risk_amount == 1000.0
        # 2. Stop_Distance = abs(Entry_Price - Stop_Loss) = 1500.0
        assert res.stop_distance == 1500.0
        # 3. Raw_Quantity = Risk_Budget / Stop_Distance = 1000 / 1500 = 0.666666...
        assert pytest.approx(res.raw_quantity, 0.0001) == 1000.0 / 1500.0
        # 4. Fee_Adjusted_Quantity = Raw_Quantity * 0.98 = 0.653333...
        assert pytest.approx(res.fee_adjusted_quantity, 0.0001) == (1000.0 / 1500.0) * 0.98
        # 5. Round down to nearest step size (0.001 for BTC) -> 0.653
        assert res.units == 0.653
        assert res.total_value == 0.653 * 50000.0

    def test_calculate_position_size_ccxt_market_limits_and_contract_multiplier(self):
        """Task 1: Verify CCXT market limits (min_qty, max_qty, step_size) and contractSize."""
        mock_exchange = MagicMock()
        mock_exchange.market.return_value = {
            "symbol": "CUSTOM/USDT",
            "contractSize": 5.0,  # 1 contract = 5 units
            "precision": {"amount": 0.05},  # Step size 0.05
            "limits": {
                "amount": {
                    "min": 0.1,
                    "max": 20.0
                }
            }
        }
        account_balance = 50000.0
        entry_price = 100.0
        stop_loss = 90.0  # Stop distance = 10.0
        res = calculate_position_size(
            account_balance=account_balance,
            entry_price=entry_price,
            stop_loss=stop_loss,
            symbol="CUSTOM/USDT",
            exchange=mock_exchange
        )
        assert res.risk_amount == 500.0
        assert res.stop_distance == 10.0
        assert pytest.approx(res.raw_quantity, 0.001) == 50.0
        assert pytest.approx(res.fee_adjusted_quantity, 0.001) == 49.0
        assert res.units == 9.8
        assert res.contract_multiplier == 5.0

    def test_portfolio_exposure_cap_rejects_third_trade(self):
        """Task 2: If 2 trades are already open (PENDING, TRIGGERED, FILLED), reject new plan."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.in_.return_value.execute.return_value.data = [
            {"id": "plan-1", "symbol": "BTC/USDT:USDT", "direction": "LONG", "status": "FILLED"},
            {"id": "plan-2", "symbol": "SOL/USDT:USDT", "direction": "SHORT", "status": "PENDING"},
        ]
        mock_table.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = []

        scorer = TradeScorer(db_manager=mock_db)
        passed, reason = scorer.check_portfolio_exposure("ETH/USDT:USDT", "LONG")
        assert passed is False
        assert "exposure cap reached" in reason.lower()

        # calculate_trade_plan also returns None
        mock_macro = MagicMock()
        mock_macro.get_macro_risk.return_value = {"score": 10, "is_valid": True}
        mock_macro.get_funding_and_oi.return_value = {"funding_rate": 0.0001, "oi_change_24h_pct": 5.0}

        signal = {
            "id": "sig-eth-3rd",
            "symbol": "ETH/USDT:USDT",
            "score": 85.0,
            "metadata": {"close": 3000.0, "atr": 50.0, "direction": "LONG"}
        }
        plan = scorer.calculate_trade_plan(signal, macro_fetcher=mock_macro)
        assert plan is None

    def test_portfolio_exposure_rejects_correlated_long_btc_and_long_eth(self):
        """Task 2: If LONG BTC is open, reject new LONG ETH setup due to high correlation."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.in_.return_value.execute.return_value.data = [
            {"id": "plan-1", "symbol": "BTC/USDT:USDT", "direction": "LONG", "status": "FILLED"},
        ]
        mock_table.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = []

        scorer = TradeScorer(db_manager=mock_db)
        # LONG ETH should be rejected because LONG BTC is already open
        passed, reason = scorer.check_portfolio_exposure("ETH/USDT:USDT", "LONG")
        assert passed is False
        assert "correlation conflict" in reason.lower()

        # SHORT ETH is opposing (hedging/relative strength spread) and should be allowed
        passed_short, reason_short = scorer.check_portfolio_exposure("ETH/USDT:USDT", "SHORT")
        assert passed_short is True

    def test_realized_drawdown_circuit_breaker_halts_at_daily_floor(self):
        """Task 2: If realized losses today reach 2% of account balance, halt and reject plan."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = [
            {
                "id": "closed-1",
                "symbol": "BTC/USDT:USDT",
                "status": "CLOSED",
                "plan_metadata": {"realized_pnl": -1500.0, "closed_at": datetime.now(timezone.utc).isoformat()}
            },
            {
                "id": "closed-2",
                "symbol": "ETH/USDT:USDT",
                "status": "CLOSED",
                "plan_metadata": {"realized_pnl": -1000.0, "closed_at": datetime.now(timezone.utc).isoformat()}
            }
        ]
        mock_table.select.return_value.in_.return_value.execute.return_value.data = []

        scorer = TradeScorer(db_manager=mock_db)
        passed, reason = scorer.check_realized_drawdown_circuit_breaker(account_balance=100000.0)
        assert passed is False
        assert "risk circuit breaker tripped" in reason.lower()
        mock_db.set_halt.assert_called_once()

        mock_macro = MagicMock()
        mock_macro.get_macro_risk.return_value = {"score": 10, "is_valid": True}
        mock_macro.get_funding_and_oi.return_value = {"funding_rate": 0.0001, "oi_change_24h_pct": 5.0}

        signal = {
            "id": "sig-post-halt",
            "symbol": "SOL/USDT:USDT",
            "score": 90.0,
            "metadata": {"close": 150.0, "atr": 5.0, "direction": "LONG"}
        }
        plan = scorer.calculate_trade_plan(signal, macro_fetcher=mock_macro)
        assert plan is None

    def test_realized_drawdown_strictly_excludes_floating_pnl(self):
        """Task 2: Circuit breaker must strictly isolate realized equity and ignore floating PnL on open trades."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        # 1 Open trade with massive floating loss (-$10,000)
        mock_table.select.return_value.in_.return_value.execute.return_value.data = [
            {
                "id": "open-filled-1",
                "symbol": "BTC/USDT:USDT",
                "direction": "LONG",
                "entry_price": 60000.0,
                "status": "FILLED",
                "plan_metadata": {"floating_pnl": -10000.0}
            }
        ]
        # Closed trades today have small profit (+$100)
        mock_table.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = [
            {
                "id": "closed-today-1",
                "status": "CLOSED",
                "plan_metadata": {"realized_pnl": 100.0, "closed_at": datetime.now(timezone.utc).isoformat()}
            }
        ]

        scorer = TradeScorer(db_manager=mock_db)
        passed, reason = scorer.check_realized_drawdown_circuit_breaker(account_balance=100000.0)
        assert passed is True
        mock_db.set_halt.assert_not_called()


    # --------------------------------------------------------------------------
    # Quant Audit P1 Upgrades: Adverse Selection Rejection & Half-Tick Slippage
    # --------------------------------------------------------------------------
    def test_paper_executor_adverse_selection_marks_rejected(self):
        """PaperExecutor evaluate_plan marks touched plan as REJECTED under adverse selection."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)
        executor.adverse_selection_rate = 1.0  # Force adverse selection

        plan = {
            "id": "plan-adverse-exec-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        with patch.object(executor, "check_market_touch", return_value=(True, 49950.0)), \
             patch.object(executor, "update_plan_status", return_value=True) as mock_update:
            executor.evaluate_plan(plan)
            mock_update.assert_called_once()
            assert mock_update.call_args[0][0] == "plan-adverse-exec-1"
            assert mock_update.call_args[0][1] == "REJECTED"
            assert mock_update.call_args[0][2]["reason"] == "ADVERSE_SELECTION_REVERSAL"

    def test_paper_executor_maker_slippage_within_half_tick_bounds(self):
        """Maker orders should not slip more than half a tick (0.0% to 0.05%, max 0.0005)."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        mock_table.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {"plan_metadata": {}}
        ]
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value.data = [
            {"id": "plan-slip-bounds-1", "status": "FILLED"}
        ]

        executor = PaperExecutor(db_manager=mock_db)
        executor.adverse_selection_rate = 0.0
        plan = {
            "id": "plan-slip-bounds-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "plan_metadata": {
                "triggered_at": datetime.now(timezone.utc).isoformat()
            }
        }

        with patch("random.uniform", wraps=__import__("random").uniform) as spy_uniform:
            res = executor.fill_triggered_plan(plan)
            assert res is True
            # Verify uniform was called with range (0, 0.0005)
            spy_uniform.assert_called_with(0, 0.0005)

    def test_paper_executor_partially_filled_state(self):
        """PaperExecutor partially_fill_plan transitions status to PARTIALLY_FILLED."""
        mock_db = MagicMock()
        executor = PaperExecutor(db_manager=mock_db)

        plan = {
            "id": "plan-partial-1",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 50000.0,
            "stop_loss": 48500.0,
        }

        with patch.object(executor, "update_plan_status", return_value=True) as mock_update:
            res = executor.partially_fill_plan(plan, fill_ratio=0.5)
            assert res is True
            mock_update.assert_called_once()
            assert mock_update.call_args[0][0] == "plan-partial-1"
            assert mock_update.call_args[0][1] == "PARTIALLY_FILLED"
            meta = mock_update.call_args[0][2]
            assert meta["fill_ratio"] == 0.5
            assert meta["position_size"] > 0

    def test_scorer_exposure_cap_counts_partially_filled_trades(self):
        """PARTIALLY_FILLED trades count toward the 2-trade exposure cap."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        # 1 FILLED and 1 PARTIALLY_FILLED trade open
        mock_table.select.return_value.in_.return_value.execute.return_value.data = [
            {"id": "plan-open-1", "symbol": "BTC/USDT:USDT", "direction": "LONG", "status": "FILLED"},
            {"id": "plan-open-2", "symbol": "SOL/USDT:USDT", "direction": "SHORT", "status": "PARTIALLY_FILLED"},
        ]
        mock_table.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = []

        scorer = TradeScorer(db_manager=mock_db)
        passed, reason = scorer.check_portfolio_exposure("ETH/USDT:USDT", "LONG")
        assert passed is False
        assert "exposure cap reached" in reason.lower()

    def test_is_friday_flatten_time_boundary_conditions(self):
        """Verify Friday Flatten Rule boundary triggers at Friday 15:30 UTC, 30 min before 16:00 UTC weekend lock."""
        from src.paper_executor import is_friday_flatten_time

        # Thursday 15:30 UTC -> False
        thu_1530 = datetime(2026, 10, 15, 15, 30, tzinfo=timezone.utc)
        assert is_friday_flatten_time(thu_1530) is False

        # Friday 15:00 UTC -> False
        fri_1500 = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
        assert is_friday_flatten_time(fri_1500) is False

        # Friday 15:29 UTC -> False
        fri_1529 = datetime(2026, 10, 16, 15, 29, tzinfo=timezone.utc)
        assert is_friday_flatten_time(fri_1529) is False

        # Friday 15:30 UTC -> True (Exact Trigger Time!)
        fri_1530 = datetime(2026, 10, 16, 15, 30, tzinfo=timezone.utc)
        assert is_friday_flatten_time(fri_1530) is True

        # Friday 15:59 UTC -> True (Still before 16:00 UTC weekend gate)
        fri_1559 = datetime(2026, 10, 16, 15, 59, tzinfo=timezone.utc)
        assert is_friday_flatten_time(fri_1559) is True

        # Friday 16:00 UTC -> True (Weekend Gate locked)
        fri_1600 = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
        assert is_friday_flatten_time(fri_1600) is True

        # Saturday 12:00 UTC -> True
        sat_1200 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
        assert is_friday_flatten_time(sat_1200) is True

        # Sunday 23:59 UTC -> True
        sun_2359 = datetime(2026, 10, 18, 23, 59, tzinfo=timezone.utc)
        assert is_friday_flatten_time(sun_2359) is True

        # Monday 00:00 UTC -> False
        mon_0000 = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
        assert is_friday_flatten_time(mon_0000) is False

    def test_friday_flatten_closes_positions_cancels_pending_and_sets_weekend_flattened(self):
        """Hard rule: At Friday 15:30 UTC, closes all FILLED positions at market, cancels PENDING orders, and sets WEEKEND_FLATTENED."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value

        # Mock 1 FILLED trade and 1 PENDING order in DB
        filled_trade = {
            "id": "plan-filled-fri",
            "symbol": "BTC/USDT:USDT",
            "direction": "LONG",
            "entry_price": 68000.0,
            "status": "FILLED",
            "position_size": 0.5,
            "plan_metadata": {},
        }
        pending_trade = {
            "id": "plan-pending-fri",
            "symbol": "ETH/USDT:USDT",
            "direction": "SHORT",
            "entry_price": 2700.0,
            "status": "PENDING",
            "plan_metadata": {},
        }

        # Mock queries
        mock_table.select.return_value.in_.return_value.execute.side_effect = [
            MagicMock(data=[filled_trade]),   # filled query
            MagicMock(data=[pending_trade]),  # pending query
        ]

        executor = PaperExecutor(db_manager=mock_db)
        fri_1530 = datetime(2026, 10, 16, 15, 30, tzinfo=timezone.utc)

        with patch.object(executor, "close_trade", wraps=executor.close_trade) as spy_close, \
             patch.object(executor, "update_plan_status", wraps=executor.update_plan_status) as spy_update:
            res = executor.execute_friday_flatten(now_dt=fri_1530)
            assert res["status"] == "WEEKEND_FLATTENED"
            assert res["closed_positions"] == 1
            assert res["cancelled_orders"] == 1

            # Verify close_trade called with exit_reason="FRIDAY_FLATTEN"
            spy_close.assert_called_once()
            assert spy_close.call_args[1]["exit_reason"] == "FRIDAY_FLATTEN"

            # Verify update_plan_status was called for closing filled and cancelling pending
            assert spy_update.call_args_list[0][0][0] == "plan-filled-fri"
            assert spy_update.call_args_list[0][0][1] == "CLOSED"
            assert spy_update.call_args_list[0][0][2]["exit_reason"] == "FRIDAY_FLATTEN"

            assert spy_update.call_args_list[1][0][0] == "plan-pending-fri"
            assert spy_update.call_args_list[1][0][1] == "CANCELLED"
            assert spy_update.call_args_list[1][0][2]["reason"] == "FRIDAY_FLATTEN"

            # Verify system_state set to WEEKEND_FLATTENED
            mock_table.update.assert_called()

    def test_intra_hour_circuit_breaker_hard_liquidation(self):
        """Hard liquidation trigger: If equity drops 5% in 60 minutes, immediately flatten all positions, halt trading for 24h, and set CIRCUIT_BREAKER."""
        mock_db = MagicMock()
        mock_table = mock_db.client.table.return_value
        executor = PaperExecutor(db_manager=mock_db)

        from src.db_manager import SupabaseManager
        mgr = object.__new__(SupabaseManager)
        mgr.client = mock_db.client

        with patch.object(executor, "flatten_all_positions", return_value={"closed_positions": 1, "cancelled_orders": 1}) as mock_flatten:
            passed, drop_pct, reason = mgr.check_intra_hour_drawdown(
                starting_equity=100000.0,
                current_equity=95000.0,
                time_elapsed_minutes=50.0,
                executor=executor,
            )
            assert passed is False
            assert drop_pct == 0.05
            assert "Intra-Hour Hard Liquidation" in reason
            mock_flatten.assert_called_once()
            # Verify system_state status set to CIRCUIT_BREAKER
            update_call = mock_table.update.call_args[0][0]
            assert update_call["status"] == "CIRCUIT_BREAKER"
            assert "cool_down_until" in update_call
