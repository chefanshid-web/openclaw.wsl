import os
import sys

base = os.path.expanduser('~/crypto-screener-v2')
print("Writing files to", base)

logger_content = '''import os
import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from src.config import config

os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)

logger = logging.getLogger('System')
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(config.log_path)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    logger.addHandler(fh)

def get_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

class PointInTimeLogger:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            cur = conn.cursor()
            # 1. signals
            cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                feature_family TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                raw_score REAL NOT NULL,
                calibrated_prob REAL NOT NULL,
                metadata TEXT
            );
            """)

            # 2. trades
            cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                client_order_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price REAL NOT NULL,
                amount REAL NOT NULL,
                fee REAL NOT NULL,
                status TEXT NOT NULL
            );
            """)

            # 3. performance
            cur.execute("""
            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                metric_value REAL NOT NULL,
                window TEXT NOT NULL
            );
            """)

            # 4. hazard_events
            cur.execute("""
            CREATE TABLE IF NOT EXISTS hazard_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT,
                severity TEXT NOT NULL
            );
            """)

            # 5. calibration
            cur.execute("""
            CREATE TABLE IF NOT EXISTS calibration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                brier_score REAL NOT NULL,
                log_loss REAL NOT NULL,
                sample_count INTEGER NOT NULL
            );
            """)

            # 6. evidence_ledger (Doctrine requirement)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS evidence_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                what_failed TEXT NOT NULL,
                what_tried TEXT NOT NULL,
                what_fixed TEXT NOT NULL,
                system_state TEXT NOT NULL
            );
            """)

            # 7. gap_log
            cur.execute("""
            CREATE TABLE IF NOT EXISTS gap_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT NOT NULL,
                known_at_time TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                gap_description TEXT NOT NULL,
                severity TEXT NOT NULL
            );
            """)
            conn.commit()

    def log_signal(self, event_time: str, known_at_time: str, symbol: str,
                   timeframe: str, feature_family: str, signal_type: str,
                   raw_score: float, calibrated_prob: float, metadata: Optional[Dict[str, Any]] = None):
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO signals 
                   (event_time, known_at_time, symbol, timeframe, feature_family, signal_type, raw_score, calibrated_prob, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_time, known_at_time, symbol, timeframe, feature_family, signal_type,
                 raw_score, calibrated_prob, json.dumps(metadata or {}))
            )

    def log_trade(self, event_time: str, known_at_time: str, client_order_id: str,
                  symbol: str, side: str, order_type: str, price: float, amount: float,
                  fee: float, status: str):
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO trades 
                   (event_time, known_at_time, client_order_id, symbol, side, order_type, price, amount, fee, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_time, known_at_time, client_order_id, symbol, side, order_type, price, amount, fee, status)
            )

    def log_evidence(self, event_time: str, known_at_time: str, what_failed: str,
                     what_tried: str, what_fixed: str, system_state: str):
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO evidence_ledger 
                   (event_time, known_at_time, what_failed, what_tried, what_fixed, system_state)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event_time, known_at_time, what_failed, what_tried, what_fixed, system_state)
            )
'''

with open(f'{base}/src/logger.py', 'w') as f:
    f.write(logger_content)

test_logger_content = '''import os
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
'''

with open(f'{base}/tests/test_logger.py', 'w') as f:
    f.write(test_logger_content)

print("Logger and test files generated successfully.")
