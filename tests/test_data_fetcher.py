import os
import pytest
import pandas as pd
from unittest.mock import MagicMock
from src.data_fetcher import DataFetcher

@pytest.fixture
def mock_fetcher(tmp_path):
    fetcher = DataFetcher(exchange_id='binance', enable_rate_limit=False)
    # Mock client methods
    fetcher.client.fetch_ohlcv = MagicMock(return_value=[
        [1710000000000, 65000.0, 65500.0, 64900.0, 65200.0, 150.5],
        [1710000900000, 65200.0, 65800.0, 65100.0, 65750.0, 210.2],
    ])
    fetcher.client.fetch_order_book = MagicMock(return_value={
        'bids': [[65700.0, 2.5], [65690.0, 5.0]],
        'asks': [[65710.0, 3.1], [65720.0, 4.2]],
        'timestamp': 1710001000000
    })
    fetcher.client.fetch_funding_rate = MagicMock(return_value={
        'symbol': 'BTC/USDT',
        'fundingRate': 0.0001,
        'timestamp': 1710001000000
    })
    return fetcher

def test_fetch_ohlcv_and_caching(mock_fetcher, tmp_path):
    cache_file = tmp_path / "BTC_USDT_15m.parquet"
    mock_fetcher.get_cache_path = lambda s, tf: str(cache_file)

    df = mock_fetcher.fetch_ohlcv('BTC/USDT', '15m', limit=2, use_cache=False)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert 'close' in df.columns
    assert os.path.exists(cache_file)

    # Second call should read from cache without invoking fetch_ohlcv again
    mock_fetcher.client.fetch_ohlcv.reset_mock()
    df_cached = mock_fetcher.fetch_ohlcv('BTC/USDT', '15m', limit=2, use_cache=True)
    assert len(df_cached) == 2
    mock_fetcher.client.fetch_ohlcv.assert_not_called()

def test_fetch_order_book(mock_fetcher):
    ob = mock_fetcher.fetch_order_book('BTC/USDT', limit=5)
    assert 'bids' in ob
    assert 'asks' in ob
    assert len(ob['bids']) == 2
    assert len(ob['asks']) == 2

def test_fetch_funding_rate(mock_fetcher):
    fr = mock_fetcher.fetch_funding_rate('BTC/USDT')
    assert fr['symbol'] == 'BTC/USDT'
    assert fr['fundingRate'] == 0.0001
