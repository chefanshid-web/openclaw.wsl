import os
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
        # WAL: readers never block the writer, which matters once the scanner and
        # the watchdog both touch this file. busy_timeout stops transient lock errors.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
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
            # Doctrine Rule 4 enforced by the database itself: no row may claim to
            # know something before it happened. Triggers (not a CHECK) because
            # existing tables cannot gain a constraint via CREATE TABLE IF NOT EXISTS.
            for table in ("signals", "trades", "performance", "hazard_events",
                          "calibration", "evidence_ledger", "gap_log"):
                cur.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_pit_{table}
                BEFORE INSERT ON {table}
                FOR EACH ROW WHEN NEW.known_at_time < NEW.event_time
                BEGIN
                    SELECT RAISE(ABORT, 'PIT violation: known_at_time < event_time');
                END;
                """)
            # indexes for backtest queries: by time window and by signal family
            for stmt in (
                "CREATE INDEX IF NOT EXISTS idx_signals_event_time ON signals(event_time)",
                "CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type)",
                "CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, event_time)",
            ):
                cur.execute(stmt)
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
