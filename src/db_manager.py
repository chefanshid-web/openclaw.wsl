"""
OpenClaw Quant Lab - Phase 1: The Cloud Foundation
Database Manager Module (Supabase)
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Union, Callable, List, Tuple
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger("DBManager")


def retry_db_call(
    func: Callable,
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    **kwargs,
) -> Any:
    """
    Helper function executing database operations with exponential backoff retry logic.

    Args:
        func: Callable database operation.
        max_retries: Maximum number of attempts (default 3).
        base_delay: Base delay in seconds for exponential backoff (default 1.0).
        *args, **kwargs: Arguments passed to func if func takes arguments.

    Catches Exception, logs the error, waits base_delay * (2 ** attempt) seconds, and retries.
    """
    func_args = args
    if len(args) == 1 and isinstance(args[0], int):
        max_retries = args[0]
        func_args = ()
    elif len(args) == 2 and isinstance(args[0], int) and isinstance(args[1], (int, float)):
        max_retries = args[0]
        base_delay = float(args[1])
        func_args = ()
    elif "max_retries" in kwargs:
        max_retries = kwargs.pop("max_retries")
    if "base_delay" in kwargs:
        base_delay = kwargs.pop("base_delay")

    last_exception = None
    for attempt in range(max_retries):
        try:
            if callable(func):
                return func(*func_args, **kwargs)
            return func
        except Exception as e:
            last_exception = e
            delay = base_delay * (2 ** attempt)
            logger.warning(
                f"[DB RETRY] Attempt {attempt + 1}/{max_retries} failed: {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)

    logger.error(f"[DB RETRY EXHAUSTED] All {max_retries} attempts failed: {last_exception}")
    raise last_exception


class SupabaseManager:
    """
    Manages cloud database interactions with Supabase for the OpenClaw Quant Lab.
    Handles signals audit logging, lookahead-bias checks, and system kill-switch state.
    """

    SCHEMA_SQL = """-- OpenClaw Quant Lab: Cloud Database Schema (Phase 1)

-- 1. Signals Table
CREATE TABLE IF NOT EXISTS signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    known_at_time TIMESTAMPTZ NOT NULL,
    signal_type TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    retry_count INT DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    CONSTRAINT chk_known_at_after_event CHECK (known_at_time >= event_time)
);

-- Migration for existing signals table:
ALTER TABLE signals ADD COLUMN IF NOT EXISTS retry_count INT DEFAULT 0;

-- Index for efficient querying by symbol and timestamp
CREATE INDEX IF NOT EXISTS idx_signals_symbol_event_time ON signals (symbol, event_time DESC);

-- 2. System State (Kill Switch) Table
CREATE TABLE IF NOT EXISTS system_state (
    id INT PRIMARY KEY DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    reason TEXT,
    cool_down_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE system_state ADD COLUMN IF NOT EXISTS cool_down_until TIMESTAMPTZ;

-- 3. Initial state insertion (id=1, status='RUNNING')
INSERT INTO system_state (id, status, updated_at)
VALUES (1, 'RUNNING', now())
ON CONFLICT (id) DO NOTHING;

-- 4. Trade Plans Table
CREATE TABLE IF NOT EXISTS trade_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id UUID,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    stop_loss DOUBLE PRECISION NOT NULL,
    take_profit DOUBLE PRECISION NOT NULL,
    atr DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    plan_metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trade_plans_status ON trade_plans (status);
CREATE INDEX IF NOT EXISTS idx_trade_plans_symbol ON trade_plans (symbol, created_at DESC);

-- 5. Factory Ledger Table (Append-Only Lifecycle Ledger)
CREATE TABLE IF NOT EXISTS factory_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT now(),
    strategy_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    metrics JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_factory_ledger_timestamp ON factory_ledger (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_factory_ledger_strategy ON factory_ledger (strategy_name);
CREATE INDEX IF NOT EXISTS idx_factory_ledger_action ON factory_ledger (action);

-- 6. Agent Health Table (Heartbeat and SRE Monitoring)
CREATE TABLE IF NOT EXISTS agent_health (
    agent_name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'HEALTHY',
    latency_ms DOUBLE PRECISION,
    last_heartbeat TIMESTAMPTZ DEFAULT now(),
    error_msg TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_health_last_heartbeat ON agent_health (last_heartbeat DESC);
"""

    def __init__(self, url: Optional[str] = None, key: Optional[str] = None):
        """
        Initializes the Supabase client using provided credentials or .env variables.
        """
        self.supabase_url: Optional[str] = url or os.getenv("SUPABASE_URL")
        self.supabase_key: Optional[str] = key or os.getenv("SUPABASE_KEY")

        if not self.supabase_url or not self.supabase_key:
            raise ValueError(
                "Missing Supabase credentials! Please ensure SUPABASE_URL and SUPABASE_KEY "
                "are set in your .env file or passed to SupabaseManager(url, key)."
            )

        try:
            from supabase import create_client, Client
            self.client: Client = create_client(self.supabase_url, self.supabase_key)
        except ImportError:
            raise ImportError(
                "The 'supabase' library is not installed. Please run: pip install -r requirements.txt"
            )

    def init_db(self) -> str:
        """
        Initializes the database schema for the signals and system_state tables.

        Attempts to execute raw SQL via the Supabase RPC function 'exec_sql'.
        If direct SQL execution via RPC is not configured in your Supabase project,
        this method prints step-by-step instructions and returns the raw SQL string
        to be pasted into the Supabase Dashboard SQL Editor.

        Returns:
            str: Raw SQL schema definition.
        """
        print("=" * 60)
        print(" OpenClaw Quant Lab: Database Schema Initialization")
        print("=" * 60)
        print("Attempting to execute raw SQL schema via Supabase RPC ('exec_sql')...")

        try:
            self.client.rpc("exec_sql", {"sql": self.SCHEMA_SQL}).execute()
            print("[+] Successfully executed schema migration via RPC.")
        except Exception as e:
            print("[!] Supabase PostgREST does not support arbitrary raw SQL without an RPC function.")
            print(f"    RPC call output: {e}\n")
            print(">>> INSTRUCTIONS TO RUN SQL IN SUPABASE DASHBOARD:")
            print("1. Go to your Supabase project dashboard: https://supabase.com/dashboard")
            print("2. In the left-hand navigation menu, click 'SQL Editor' (terminal icon).")
            print("3. Click '+ New query'.")
            print("4. Copy and paste the SQL script below and click 'Run'.\n")
            print("-" * 60)
            print(self.SCHEMA_SQL)
            print("-" * 60)

        return self.SCHEMA_SQL

    def log_signal(
        self,
        symbol: str,
        event_time: Union[datetime, str],
        signal_type: str,
        score: float,
        metadata: Optional[Dict[str, Any]] = None,
        known_at_time: Optional[Union[datetime, str]] = None,
        retry_count: int = 0,
    ) -> Dict[str, Any]:
        """
        Inserts a signal row into the 'signals' table.

        CRITICAL LOOKAHEAD PREVENTION:
        Enforces known_at_time >= event_time.
        If known_at_time is not supplied, it defaults to max(current_utc_time, event_time).

        Args:
            symbol: Trading pair or instrument symbol (e.g., 'BTC/USDT').
            event_time: Timestamp of market event (datetime or ISO string).
            signal_type: Signal classification (e.g., 'BREAKOUT_LONG', 'MEAN_REVERSION').
            score: Signal strength or conviction score (float).
            metadata: Additional dictionary payload with context, indicators, or parameters.
            known_at_time: Timestamp when signal became known to execution system.
            retry_count: Number of technical retries attempted for this signal (default 0).

        Returns:
            Dict[str, Any]: The inserted signal record.
        """
        if metadata is None:
            metadata = {}

        event_dt = self._to_datetime(event_time)

        if known_at_time is None:
            now_dt = datetime.now(timezone.utc)
            known_dt = max(now_dt, event_dt + timedelta(milliseconds=1))
        else:
            known_dt = self._to_datetime(known_at_time)

        # Enforce check constraint in application layer before database round-trip
        if known_dt < event_dt:
            raise ValueError(
                f"Constraint violation: known_at_time ({known_dt.isoformat()}) "
                f"must be >= event_time ({event_dt.isoformat()}) to prevent lookahead bias."
            )

        row = {
            "symbol": symbol,
            "event_time": event_dt.isoformat(),
            "known_at_time": known_dt.isoformat(),
            "signal_type": signal_type,
            "score": float(score),
            "retry_count": int(retry_count),
            "metadata": metadata,
        }

        def _do_insert():
            try:
                response = self.client.table("signals").insert(row).execute()
                if response.data and len(response.data) > 0:
                    return response.data[0]
                return row
            except Exception as e:
                # Fallback if retry_count column has not yet been added to Supabase
                if "retry_count" in str(e):
                    row_no_retry = {k: v for k, v in row.items() if k != "retry_count"}
                    response = self.client.table("signals").insert(row_no_retry).execute()
                    if response.data and len(response.data) > 0:
                        return response.data[0]
                    return row_no_retry
                raise

        return retry_db_call(_do_insert)

    def save_trade_plan(self, plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Inserts a trade plan into the 'trade_plans' table with retry logic."""
        if not plan:
            return None

        def _do_save():
            signal_id = plan.get("signal_id")
            if signal_id:
                try:
                    existing = (
                        self.client.table("trade_plans")
                        .select("id, status")
                        .eq("signal_id", signal_id)
                        .execute()
                    )
                    if existing.data and len(existing.data) > 0:
                        logger.info(f"TradePlan already exists for signal_id {signal_id}. Skipping duplicate.")
                        return existing.data[0]
                except Exception as e:
                    logger.warning(f"Error checking existing trade plan: {e}")

            response = self.client.table("trade_plans").insert(plan).execute()
            if response.data and len(response.data) > 0:
                return response.data[0]
            return plan

        return retry_db_call(_do_save)

    def get_triggered_plans(self) -> List[Dict[str, Any]]:
        """Queries trade_plans table for records with status 'TRIGGERED' with retry logic."""
        def _fetch():
            response = (
                self.client.table("trade_plans")
                .select("*")
                .eq("status", "TRIGGERED")
                .order("created_at", desc=False)
                .execute()
            )
            return response.data or []

        return retry_db_call(_fetch)

    def update_plan_status(
        self,
        plan_id: str,
        new_status: str,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Updates the status and metadata of a trade plan with retry logic."""
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "status": new_status,
            "updated_at": now_iso,
        }

        def _do_update():
            current = (
                self.client.table("trade_plans")
                .select("plan_metadata")
                .eq("id", plan_id)
                .limit(1)
                .execute()
            )
            meta = {}
            if current.data and len(current.data) > 0:
                meta = current.data[0].get("plan_metadata") or {}

            if extra_metadata:
                meta.update(extra_metadata)
            payload["plan_metadata"] = meta

            self.client.table("trade_plans").update(payload).eq("id", plan_id).execute()
            logger.info(f"Trade plan {plan_id} transitioned to '{new_status}'")
            return True

        return retry_db_call(_do_update)

    def log_factory_ledger(
        self,
        strategy_name: str,
        agent_name: str,
        action: str,
        reason: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        timestamp: Optional[Union[datetime, str]] = None,
    ) -> Dict[str, Any]:
        """
        Inserts an append-only lifecycle event into the 'factory_ledger' table.
        Columns: id, timestamp, strategy_name, agent_name, action, reason, metrics.

        Args:
            strategy_name: Identifier/title of strategy (e.g. 'H1-R: Gated Sweep Continuation').
            agent_name: Proposing or evaluating agent (e.g. 'Hunter', 'Backtest', 'Red Team', 'Validator').
            action: Lifecycle stage ('PROPOSED', 'BACKTESTED', 'REJECTED', 'APPROVED').
            reason: Exact mathematical reason, threshold breach, or failure mode.
            metrics: Dictionary of quantitative metrics (Sharpe, Win Rate, Max Drawdown, etc.).
            timestamp: Event timestamp (default now UTC).

        Returns:
            Dict[str, Any]: The persisted or prepared ledger record.
        """
        if timestamp:
            ts_dt = self._to_datetime(timestamp)
        else:
            ts_dt = datetime.now(timezone.utc)

        row = {
            "timestamp": ts_dt.isoformat(),
            "strategy_name": str(strategy_name),
            "agent_name": str(agent_name),
            "action": str(action).strip().upper(),
            "reason": str(reason or ""),
            "metrics": metrics or {},
        }

        def _do_insert():
            try:
                response = self.client.table("factory_ledger").insert(row).execute()
                if response.data and len(response.data) > 0:
                    logger.info(
                        f"[FACTORY LEDGER] Logged '{row['action']}' for strategy '{strategy_name}' "
                        f"by agent '{agent_name}'."
                    )
                    return response.data[0]
                return row
            except Exception as e:
                logger.warning(f"Failed to insert into factory_ledger table: {e}")
                return row

        try:
            return retry_db_call(_do_insert)
        except Exception as e:
            logger.error(f"Error logging to factory_ledger: {e}")
            return row

    def get_factory_ledger(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Queries the latest rows from the 'factory_ledger' table, ordered by timestamp descending.

        Args:
            limit: Maximum number of rows to return (default 20).

        Returns:
            List[Dict[str, Any]]: List of ledger records.
        """
        try:
            response = (
                self.client.table("factory_ledger")
                .select("*")
                .order("timestamp", desc=True)
                .limit(limit)
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.debug(f"Could not fetch factory_ledger rows: {e}")
            return []

    def increment_retry(self, signal_id: str) -> Dict[str, Any]:
        """
        Increments the retry_count for a signal after a [TECHNICAL] failure.
        If retry_count reaches >= 3, logs critical alert requiring operator escalation.

        Returns:
            Dict[str, Any]: {"signal_id": signal_id, "retry_count": count, "escalate_to_operator": bool}
        """
        try:
            current = (
                self.client.table("signals")
                .select("retry_count")
                .eq("id", signal_id)
                .limit(1)
                .execute()
            )
            count = 0
            if current.data and len(current.data) > 0:
                count = current.data[0].get("retry_count") or 0

            count += 1
            escalate = count >= 3

            self.client.table("signals").update({"retry_count": count}).eq("id", signal_id).execute()

            if escalate:
                print(
                    f"[OPERATOR ESCALATION] Signal {signal_id} has failed {count} times due to "
                    f"[TECHNICAL] glitches. Max retry threshold (3) reached. Manual intervention required!"
                )
            else:
                print(
                    f"[TECHNICAL RETRY] Signal {signal_id} retry_count incremented to {count}/3. Re-running."
                )

            return {
                "signal_id": signal_id,
                "retry_count": count,
                "escalate_to_operator": escalate,
            }
        except Exception as e:
            print(f"[!] Failed to increment retry_count for signal {signal_id}: {e}")
            return {
                "signal_id": signal_id,
                "retry_count": None,
                "escalate_to_operator": True,
                "error": str(e),
            }

    def check_halt(self) -> bool:
        """
        Queries system_state for id=1 and returns True if status is 'HALT',
        'CIRCUIT_BREAKER', or 'WEEKEND_FLATTENED', else False.

        Returns:
            bool: True if system is HALTED / CIRCUIT_BREAKER / WEEKEND_FLATTENED, False if RUNNING or not found.
        """
        try:
            response = (
                self.client.table("system_state")
                .select("status")
                .eq("id", 1)
                .limit(1)
                .execute()
            )
            if response.data and len(response.data) > 0:
                status = str(response.data[0].get("status", "")).strip().upper()
                return status in ("HALT", "CIRCUIT_BREAKER", "WEEKEND_FLATTENED")
            return False
        except Exception as e:
            print(f"[!] Error checking system halt state: {e}")
            return False

    def set_halt(self, reason: str = "") -> Dict[str, Any]:
        """
        Updates system_state to 'HALT' for id=1, recording reason, timestamp,
        and cool_down_until (now + 4 hours).
        """
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        cool_down_until = (now_dt + timedelta(hours=4)).isoformat()
        payload = {
            "id": 1,
            "status": "HALT",
            "reason": reason,
            "cool_down_until": cool_down_until,
            "updated_at": now_iso,
        }

        try:
            response = (
                self.client.table("system_state")
                .update({
                    "status": "HALT",
                    "reason": reason,
                    "cool_down_until": cool_down_until,
                    "updated_at": now_iso
                })
                .eq("id", 1)
                .execute()
            )
            if not response.data:
                response = self.client.table("system_state").upsert(payload).execute()
            if response.data and len(response.data) > 0:
                return response.data[0]
        except Exception as e:
            # Fallback if column cool_down_until not yet migrated on remote Supabase
            try:
                fallback_payload = {"id": 1, "status": "HALT", "reason": reason, "updated_at": now_iso}
                response = (
                    self.client.table("system_state")
                    .update({"status": "HALT", "reason": reason, "updated_at": now_iso})
                    .eq("id", 1)
                    .execute()
                )
                if not response.data:
                    response = self.client.table("system_state").upsert(fallback_payload).execute()
                if response.data and len(response.data) > 0:
                    ret = dict(response.data[0])
                    ret["cool_down_until"] = cool_down_until
                    return ret
            except Exception:
                pass

        return payload

    def set_circuit_breaker(self, reason: str = "Intra-hour 5% equity drop hard liquidation trigger") -> Dict[str, Any]:
        """
        Hard liquidation trigger:
        Sets system_state status to 'CIRCUIT_BREAKER', halts trading for 24 hours (cool_down_until = now + 24h),
        and records the audit reason.
        """
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        cool_down_until = (now_dt + timedelta(hours=24)).isoformat()
        payload = {
            "id": 1,
            "status": "CIRCUIT_BREAKER",
            "reason": reason,
            "cool_down_until": cool_down_until,
            "updated_at": now_iso,
        }

        try:
            response = (
                self.client.table("system_state")
                .update({
                    "status": "CIRCUIT_BREAKER",
                    "reason": reason,
                    "cool_down_until": cool_down_until,
                    "updated_at": now_iso,
                })
                .eq("id", 1)
                .execute()
            )
            if not response.data:
                response = self.client.table("system_state").upsert(payload).execute()
            if response.data and len(response.data) > 0:
                return response.data[0]
        except Exception as e:
            try:
                fallback_payload = {"id": 1, "status": "CIRCUIT_BREAKER", "reason": reason, "updated_at": now_iso}
                response = (
                    self.client.table("system_state")
                    .update({"status": "CIRCUIT_BREAKER", "reason": reason, "updated_at": now_iso})
                    .eq("id", 1)
                    .execute()
                )
                if not response.data:
                    response = self.client.table("system_state").upsert(fallback_payload).execute()
                if response.data and len(response.data) > 0:
                    ret = dict(response.data[0])
                    ret["cool_down_until"] = cool_down_until
                    return ret
            except Exception:
                pass

        return payload

    def set_weekend_flattened(self, reason: str = "Friday 15:30 UTC pre-weekend flatten executed") -> Dict[str, Any]:
        """
        Updates system_state to 'WEEKEND_FLATTENED' for id=1.
        """
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        payload = {
            "id": 1,
            "status": "WEEKEND_FLATTENED",
            "reason": reason,
            "updated_at": now_iso,
        }

        try:
            response = (
                self.client.table("system_state")
                .update({
                    "status": "WEEKEND_FLATTENED",
                    "reason": reason,
                    "updated_at": now_iso,
                })
                .eq("id", 1)
                .execute()
            )
            if not response.data:
                response = self.client.table("system_state").upsert(payload).execute()
            if response.data and len(response.data) > 0:
                return response.data[0]
        except Exception as e:
            logger.warning(f"Failed to set WEEKEND_FLATTENED state in db: {e}")

        return payload

    def flatten_all_positions(
        self,
        executor: Optional[Any] = None,
        reason: str = "CIRCUIT_BREAKER_HARD_LIQUIDATION"
    ) -> Dict[str, int]:
        """
        Immediately flattens all open positions and cancels all pending orders.
        """
        if executor is not None and hasattr(executor, "flatten_all_positions"):
            res = executor.flatten_all_positions(reason=reason)
            return {
                "closed_positions": res.get("closed_positions", 0),
                "cancelled_orders": res.get("cancelled_orders", 0),
            }

        closed_count = 0
        cancelled_count = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            # 1. Close FILLED and PARTIALLY_FILLED trades
            filled_res = (
                self.client.table("trade_plans")
                .select("*")
                .in_("status", ["FILLED", "PARTIALLY_FILLED"])
                .execute()
            )
            filled_trades = filled_res.data or []
            for plan in filled_trades:
                plan_id = plan["id"]
                if executor is not None and hasattr(executor, "close_trade"):
                    entry_p = float(plan.get("entry_price") or 0.0)
                    executor.close_trade(plan, exit_price=entry_p, exit_reason=reason)
                else:
                    meta = plan.get("plan_metadata") or {}
                    meta.update({
                        "closed_at": now_iso,
                        "exit_reason": reason,
                        "hard_liquidation": True,
                    })
                    self.client.table("trade_plans").update({
                        "status": "CLOSED",
                        "updated_at": now_iso,
                        "plan_metadata": meta,
                    }).eq("id", plan_id).execute()
                closed_count += 1

            # 2. Cancel PENDING and TRIGGERED orders
            pending_res = (
                self.client.table("trade_plans")
                .select("*")
                .in_("status", ["PENDING", "TRIGGERED"])
                .execute()
            )
            pending_orders = pending_res.data or []
            for plan in pending_orders:
                plan_id = plan["id"]
                meta = plan.get("plan_metadata") or {}
                meta.update({
                    "cancelled_at": now_iso,
                    "reason": reason,
                    "cancel_reason": reason,
                })
                self.client.table("trade_plans").update({
                    "status": "CANCELLED",
                    "updated_at": now_iso,
                    "plan_metadata": meta,
                }).eq("id", plan_id).execute()
                cancelled_count += 1

        except Exception as e:
            logger.error(f"Error flattening positions in database: {e}")

        logger.critical(
            f"[HARD LIQUIDATION] Flattened {closed_count} open position(s) and "
            f"cancelled {cancelled_count} pending order(s). Reason: {reason}"
        )
        return {"closed_positions": closed_count, "cancelled_orders": cancelled_count}

    def trigger_intra_hour_circuit_breaker(
        self,
        reason: str = "Intra-hour 5% equity drop hard liquidation trigger",
        executor: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Hard Liquidation Trigger:
        1. Immediately flattens all positions at market and cancels all pending orders.
        2. Halts trading for 24 hours (cool_down_until = now + 24h).
        3. Sets system_state = 'CIRCUIT_BREAKER'.
        """
        logger.critical(
            f"[INTRA-HOUR CIRCUIT BREAKER] Hard liquidation triggered! Reason: {reason}. "
            f"Flattening all positions, cancelling pending orders, and halting trading for 24h."
        )
        # 1. Immediately flatten all positions
        self.flatten_all_positions(executor=executor, reason=reason)

        # 2 & 3. Halt trading for 24h and set system_state = 'CIRCUIT_BREAKER'
        return self.set_circuit_breaker(reason=reason)

    def check_intra_hour_drawdown(
        self,
        starting_equity: float,
        current_equity: float,
        time_elapsed_minutes: float = 60.0,
        executor: Optional[Any] = None,
    ) -> Tuple[bool, float, str]:
        """
        Evaluates intra-hour equity drawdown:
        If equity drops >= 5% in <= 60 minutes, triggers the hard liquidation circuit breaker:
        - Flattens all positions immediately
        - Halts trading for 24 hours
        - Sets system_state = 'CIRCUIT_BREAKER'
        Returns:
            (passed: bool, drop_pct: float, reason: str)
            passed is False if circuit breaker was triggered, True if within safe bounds.
        """
        if starting_equity <= 0:
            return True, 0.0, "Invalid starting equity"

        drop_pct = (starting_equity - current_equity) / starting_equity
        if drop_pct >= 0.05 and time_elapsed_minutes <= 60.0:
            reason = (
                f"Intra-Hour Hard Liquidation: Equity dropped {drop_pct * 100:.2f}% (>= 5.0%) "
                f"within {time_elapsed_minutes:.1f} minutes. All positions flattened, 24-hour halt engaged."
            )
            self.trigger_intra_hour_circuit_breaker(reason=reason, executor=executor)
            return False, drop_pct, reason

        return True, drop_pct, f"Intra-hour equity drawdown {drop_pct * 100:.2f}% within normal bounds"

    def get_cool_down_until(self) -> Optional[str]:
        """Queries system_state for cool_down_until timestamp."""
        try:
            response = (
                self.client.table("system_state")
                .select("cool_down_until")
                .eq("id", 1)
                .limit(1)
                .execute()
            )
            if response.data and len(response.data) > 0:
                return response.data[0].get("cool_down_until")
            return None
        except Exception:
            # Gracefully return None if column is not yet migrated on remote Supabase
            return None

    def is_in_cool_down(self) -> bool:
        """Returns True if cool_down_until is active and in the future."""
        ts = self.get_cool_down_until()
        if not ts:
            return False
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < dt
        except Exception:
            return False

    def set_running(self) -> Dict[str, Any]:
        """
        Resets system_state to 'RUNNING' for id=1.

        Returns:
            Dict[str, Any]: Updated system state record.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "id": 1,
            "status": "RUNNING",
            "reason": None,
            "updated_at": now_iso,
        }
        response = self.client.table("system_state").upsert(payload).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return payload

    def update_agent_health(
        self,
        agent_name: str,
        status: str,
        latency_ms: Optional[float] = None,
        error_msg: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record or update heartbeat and health status for a given agent.
        Upserts into `agent_health` table.

        Args:
            agent_name: Name of the agent (e.g. 'Scanner', 'TriggerPoller', 'PaperExecutor', 'MacroFetcher').
            status: Health status ('HEALTHY', 'WARNING', 'CRITICAL', 'OFFLINE').
            latency_ms: Execution or poll latency in milliseconds.
            error_msg: Error message string if an exception occurred, else None.

        Returns:
            Dict[str, Any]: Upserted record payload.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "agent_name": agent_name,
            "status": status,
            "latency_ms": float(latency_ms) if latency_ms is not None else None,
            "last_heartbeat": now_iso,
            "error_msg": str(error_msg) if error_msg is not None else None,
        }

        if not hasattr(self, "client") or self.client is None:
            logger.debug(f"[OFFLINE] update_agent_health: {payload}")
            return payload

        def _upsert():
            response = self.client.table("agent_health").upsert(payload).execute()
            if response.data and len(response.data) > 0:
                return response.data[0]
            return payload

        try:
            return retry_db_call(_upsert, max_retries=3, base_delay=0.5)
        except Exception as e:
            logger.error(f"Failed to update agent health for {agent_name}: {e}")
            return payload

    def get_agent_health(self, agent_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Fetches current health status for all agents or a specific agent.

        Args:
            agent_name: Optional filter by agent name.

        Returns:
            List[Dict[str, Any]]: List of health records.
        """
        if not hasattr(self, "client") or self.client is None:
            return []

        def _fetch():
            query = self.client.table("agent_health").select("*")
            if agent_name:
                query = query.eq("agent_name", agent_name)
            response = query.order("last_heartbeat", desc=True).execute()
            return response.data or []

        try:
            return retry_db_call(_fetch, max_retries=3, base_delay=0.5)
        except Exception as e:
            logger.error(f"Failed to fetch agent health: {e}")
            return []


    @staticmethod
    def _to_datetime(val: Union[datetime, str]) -> datetime:
        """Helper to parse datetime objects or ISO-8601 strings into timezone-aware datetimes."""
        if isinstance(val, datetime):
            if val.tzinfo is None:
                return val.replace(tzinfo=timezone.utc)
            return val
        if isinstance(val, str):
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        raise TypeError(f"Expected datetime or ISO string timestamp, got: {type(val)}")
