import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from src.macro_fetcher import MacroFetcher, FundingOIResult


class TestMacroFetcher:

    @pytest.fixture
    def mock_exchange(self):
        exchange = MagicMock()
        exchange.fetch_funding_rate.return_value = {
            "symbol": "BTC/USDT:USDT",
            "fundingRate": 0.0001
        }
        exchange.fetch_open_interest.return_value = {
            "symbol": "BTC/USDT:USDT",
            "openInterestAmount": 100000.0
        }
        exchange.fetch_open_interest_history.return_value = [
            {"openInterestAmount": "80000.0"},
            {"openInterestAmount": "90000.0"},
            {"openInterestAmount": "100000.0"}
        ]
        return exchange

    def test_is_weekend_scenarios(self):
        fetcher = MacroFetcher()

        # Monday 10:00 UTC -> False
        dt_monday = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
        assert fetcher.is_weekend(dt_monday) is False

        # Friday 15:59 UTC -> False
        dt_fri_before = datetime(2026, 10, 23, 15, 59, tzinfo=timezone.utc)
        assert fetcher.is_weekend(dt_fri_before) is False

        # Friday 16:00 UTC -> True
        dt_fri_16 = datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)
        assert fetcher.is_weekend(dt_fri_16) is True

        # Friday 21:30 UTC -> True
        dt_fri_after = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc)
        assert fetcher.is_weekend(dt_fri_after) is True

        # Saturday 14:00 UTC -> True
        dt_sat = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
        assert fetcher.is_weekend(dt_sat) is True

        # Sunday 23:59 UTC -> True
        dt_sun = datetime(2026, 10, 25, 23, 59, tzinfo=timezone.utc)
        assert fetcher.is_weekend(dt_sun) is True

    def test_macro_risk_weekend_halt(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        with patch.object(fetcher, 'is_weekend', return_value=True):
            res = fetcher.get_macro_risk()
            assert res["score"] == -999
            assert res["is_valid"] is False
            assert "Weekend liquidity void" in res["reason"]

    def test_macro_risk_telemetry_failure_fail_closed(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        with patch.object(fetcher, 'is_weekend', return_value=False), \
             patch.object(fetcher, '_fetch_yfinance_ticker', return_value=None):
            res = fetcher.get_macro_risk()
            assert res["score"] == -1
            assert res["is_valid"] is False
            assert "TradFi Telemetry Offline" in res["reason"]

    def test_macro_risk_score_risk_on(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        with patch.object(fetcher, 'is_weekend', return_value=False), \
             patch.object(fetcher, '_fetch_yfinance_ticker') as mock_ticker, \
             patch.object(fetcher, 'get_btc_dominance', return_value=58.5):
            # Nasdaq up > 0.5%, DXY down (< 0.0%)
            mock_ticker.side_effect = lambda sym: {
                "^IXIC": {"price": 18000.0, "change_24h_pct": 1.2},
                "DX-Y.NYB": {"price": 102.0, "change_24h_pct": -0.4}
            }[sym]

            res = fetcher.get_macro_risk()
            assert res["score"] == 10
            assert res["Macro_Risk_Score"] == 10
            assert res["is_valid"] is True
            assert res["raw_data"]["regime"] == "Risk On"
            assert res["raw_data"]["btc_dominance"] == 58.5
            assert res["raw_data"]["nasdaq_change_24h"] == 1.2
            assert res["raw_data"]["dxy_change_24h"] == -0.4

    def test_macro_risk_score_risk_off(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        with patch.object(fetcher, 'is_weekend', return_value=False), \
             patch.object(fetcher, '_fetch_yfinance_ticker') as mock_ticker, \
             patch.object(fetcher, 'get_btc_dominance', return_value=55.0):
            # Nasdaq down > 0.5% (change < -0.5%), DXY up (> 0.0%)
            mock_ticker.side_effect = lambda sym: {
                "^IXIC": {"price": 17500.0, "change_24h_pct": -0.8},
                "DX-Y.NYB": {"price": 105.0, "change_24h_pct": 0.6}
            }[sym]

            res = fetcher.get_macro_risk()
            assert res["score"] == 0
            assert res["Macro_Risk_Score"] == 0
            assert res["is_valid"] is True
            assert res["raw_data"]["regime"] == "Risk Off"

    def test_macro_risk_score_neutral(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        with patch.object(fetcher, 'is_weekend', return_value=False), \
             patch.object(fetcher, '_fetch_yfinance_ticker') as mock_ticker, \
             patch.object(fetcher, 'get_btc_dominance', return_value=52.0):
            # Nasdaq up 0.2%, DXY up 0.1% -> Neutral
            mock_ticker.side_effect = lambda sym: {
                "^IXIC": {"price": 17900.0, "change_24h_pct": 0.2},
                "DX-Y.NYB": {"price": 104.0, "change_24h_pct": 0.1}
            }[sym]

            res = fetcher.get_macro_risk()
            assert res["score"] == 5
            assert res["Macro_Risk_Score"] == 5
            assert res["is_valid"] is True
            assert res["raw_data"]["regime"] == "Neutral"

    def test_check_economic_calendar(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        assert fetcher.check_economic_calendar() is False

    def test_get_funding_and_oi(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        res = fetcher.get_funding_and_oi("BTC/USDT:USDT")

        # Check dict access
        assert res["funding_rate"] == 0.0001
        assert pytest.approx(res["oi_change_24h_pct"], 0.01) == 25.0
        assert res["open_interest"] == 100000.0
        assert res["is_valid"] is True

        # Check property access
        assert res.funding_rate == 0.0001
        assert pytest.approx(res.oi_change_24h_pct, 0.01) == 25.0

        # Check tuple unpacking
        fr, oi_chg = res
        assert fr == 0.0001
        assert pytest.approx(oi_chg, 0.01) == 25.0

    def test_btc_dominance_fallback_on_failure(self, mock_exchange):
        fetcher = MacroFetcher(exchange=mock_exchange)
        with patch("requests.get", side_effect=Exception("API offline")):
            btc_d = fetcher.get_btc_dominance()
            assert btc_d == 50.0

    def test_macro_risk_stale_cache_rejects_with_negative_one(self, mock_exchange):
        import time
        fetcher = MacroFetcher(exchange=mock_exchange)
        fetcher._cache["^IXIC"] = {
            "timestamp": time.time() - 3601,
            "data": {"price": 18000.0, "change_24h_pct": 1.2}
        }
        fetcher._cache["DX-Y.NYB"] = {
            "timestamp": time.time() - 3601,
            "data": {"price": 102.0, "change_24h_pct": -0.4}
        }
        with patch.object(fetcher, 'is_weekend', return_value=False):
            res = fetcher.get_macro_risk()
            assert res["score"] == -1
            assert res["is_valid"] is False
            assert res["reason"] == "Stale Macro Data"
