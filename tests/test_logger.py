import os
import pytest
from datetime import datetime, timezone, timedelta
from src.logger import PointInTimeLogger

TEST_DB = 'data/test_signals.db'

@pytest.fixture
def logger():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    logger_instance = PointInTimeLogger(db_path=TEST_DB)
    yield logger_instance
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

def test_point_in_time_signal_logging(logger):
    now = datetime.now(timezone.utc)
    event_time = now.isoformat()
    known_at_time = (now + timedelta(milliseconds=45)).isoformat()

    logger.log_signal(
        event_time=event_time,
        known_at_time=known_at_time,
        symbol='BTC/USDT',
        timeframe='15m',
        feature_family='OrderFlow',
        signal_type='CVD_Divergence',
        raw_score=1.85,
        calibrated_prob=0.74,
        metadata={'cvd': 450.2, 'obi': 0.18}
    )

    with logger._get_conn() as conn:
        row = conn.execute("SELECT * FROM signals WHERE symbol='BTC/USDT'").fetchone()
        assert row is not None
        assert row['event_time'] == event_time
        assert row['known_at_time'] == known_at_time
        assert row['raw_score'] == 1.85
        assert row['calibrated_prob'] == 0.74

        t_event = datetime.fromisoformat(row['event_time'])
        t_known = datetime.fromisoformat(row['known_at_time'])
        assert t_known >= t_event
        assert (t_known - t_event).total_seconds() * 1000 <= 500

def test_acceptance_100_rows_ordering(logger):
    base_time = datetime.now(timezone.utc)
    for i in range(100):
        t_event = (base_time + timedelta(seconds=i)).isoformat()
        t_known = (base_time + timedelta(seconds=i, milliseconds=10)).isoformat()
        logger.log_signal(
            event_time=t_event,
            known_at_time=t_known,
            symbol='BTC/USDT',
            timeframe='15m',
            feature_family='Trend',
            signal_type='BOS_Bullish',
            raw_score=1.0,
            calibrated_prob=0.65
        )

    with logger._get_conn() as conn:
        rows = conn.execute("SELECT event_time, known_at_time FROM signals ORDER BY id ASC").fetchall()
        assert len(rows) == 100
        for row in rows:
            assert row['known_at_time'] > row['event_time']
