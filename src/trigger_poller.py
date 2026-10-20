"""
OpenClaw Quant Desk - Phase 3: The Sniper Watchlist Engine
File: src/trigger_poller.py

Responsibilities:
1. Poll Supabase 'trade_plans' table every 10 seconds for rows where status = 'PENDING'.
2. Fetch live price for each pending plan symbol using CCXT (Binance).
3. The Trigger Logic:
   - For LONG: current_price <= entry_price (or crosses entry trigger zone)
   - For SHORT: current_price >= entry_price (or crosses entry trigger zone)
   - If triggered: Update status to 'TRIGGERED' and send to PaperExecutor for immediate fill.
4. The Timeout Logic:
   - If plan sits in 'PENDING' for > 15 minutes (900 seconds), update status to 'TIMEOUT'
     to prevent stale orders.
"""

import time
import random
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import ccxt
from src.db_manager import SupabaseManager
from src.paper_executor import PaperExecutor

logger = logging.getLogger("TriggerPoller")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [TriggerPoller] %(message)s"))
    logger.addHandler(ch)


class TriggerPoller:
    """
    Sniper Watchlist Poller.
    Monitors PENDING trade plans, triggers entry upon price touch,
    and expires stale plans after 15 minutes.
    """

    TIMEOUT_SECONDS = 900  # 15 minutes max lifetime for pending plans
    ADVERSE_SELECTION_RATE = 0.30  # 30% chance price crosses entry limit but immediately reverses

    def __init__(
        self,
        db_manager: Optional[SupabaseManager] = None,
        executor: Optional[PaperExecutor] = None,
        exchange: Optional[ccxt.Exchange] = None,
    ):
        self.db: Optional[SupabaseManager] = db_manager
        self.executor: PaperExecutor = executor or PaperExecutor(db_manager=self.db)
        self.exchange: ccxt.Exchange = exchange or ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        self.running: bool = True
        self._schema_probed = False
        self.adverse_selection_rate: float = self.ADVERSE_SELECTION_RATE

    def check_adverse_selection(self) -> bool:
        """Simulates adverse selection: 30% chance price crosses entry limit but immediately reverses."""
        rate = getattr(self, "adverse_selection_rate", self.ADVERSE_SELECTION_RATE)
        if rate <= 0:
            return False
        return random.random() < rate

    def _ensure_db(self) -> Optional[SupabaseManager]:
        """Lazy-initialize or return SupabaseManager."""
        if self.db is not None:
            return self.db
        try:
            self.db = SupabaseManager()
            return self.db
        except Exception as e:
            logger.warning(f"Supabase connection unavailable: {e}")
            return None

    def _probe_schema(self):
        """Probes and logs trade_plans and plan_metadata keys."""
        db = self._ensure_db()
        if not db:
            return
        try:
            res = db.client.table("trade_plans").select("*").limit(1).execute()
            if res.data:
                logger.info(f"trade_plans keys: {list(res.data[0].keys())}")
                logger.info(f"plan_metadata keys: {list((res.data[0].get('plan_metadata') or {}).keys())}")
        except Exception as e:
            logger.debug(f"Schema probe skipped: {e}")

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalizes symbol for Binance futures (e.g., BTC/USDT -> BTC/USDT:USDT)."""
        if ":" not in symbol and "/" in symbol:
            parts = symbol.split("/")
            return f"{parts[0]}/{parts[1]}:{parts[1]}"
        return symbol

    def get_pending_plans(self) -> List[Dict[str, Any]]:
        """Queries trade_plans table for records with status 'PENDING'."""
        db = self._ensure_db()
        if not db:
            return []

        try:
            response = (
                db.client.table("trade_plans")
                .select("*")
                .eq("status", "PENDING")
                .order("created_at", desc=False)
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Error fetching pending trade plans: {e}")
            return []

    @staticmethod
    def is_ticker_fresh(
        ticker: Dict[str, Any],
        max_age_seconds: float = 10.0,
        current_time_sec: Optional[float] = None,
    ) -> bool:
        """
        Validates tick data freshness:
        If CCXT fetch_ticker() timestamp is older than max_age_seconds (10s), returns False.
        If timestamp is omitted/None (e.g. in older unit test mocks), returns True for backwards compatibility.
        """
        if not isinstance(ticker, dict):
            return False

        ticker_ts = ticker.get("timestamp")
        if ticker_ts is None and "datetime" in ticker and ticker.get("datetime"):
            try:
                dt_str = str(ticker["datetime"]).replace("Z", "+00:00")
                dt = datetime.fromisoformat(dt_str)
                ticker_ts = dt.timestamp() * 1000.0
            except Exception:
                pass

        if ticker_ts is None:
            return True

        try:
            now_sec = current_time_sec if current_time_sec is not None else time.time()
            ts_val = float(ticker_ts)
            ts_sec = (ts_val / 1000.0) if ts_val > 1e11 else ts_val
            age_seconds = now_sec - ts_sec
            if age_seconds > max_age_seconds:
                return False
            return True
        except (ValueError, TypeError):
            return True

    def fetch_current_price(self, symbol: str) -> float:
        """
        Fetches live ticker price using CCXT Binance with strict freshness check.
        If ticker timestamp is older than 10 seconds (e.g. exchange API hung),
        logs an error, skips trigger evaluation, and returns 0.0 (no trades executed).
        """
        norm_symbol = self._normalize_symbol(symbol)
        try:
            ticker = self.exchange.fetch_ticker(norm_symbol)
            if not self.is_ticker_fresh(ticker, max_age_seconds=10.0):
                ticker_ts = ticker.get("timestamp")
                ts_val = float(ticker_ts) if ticker_ts is not None else 0.0
                ts_sec = (ts_val / 1000.0) if ts_val > 1e11 else ts_val
                age_seconds = time.time() - ts_sec
                logger.error(
                    f"[STALE TICKER ERROR] CCXT fetch_ticker() timestamp for {norm_symbol} is "
                    f"{age_seconds:.2f}s old (> 10s cutoff). Exchange API may be hung. "
                    f"Skipping trigger evaluation and executing no trades."
                )
                return 0.0

            price = float(ticker.get("last") or ticker.get("close") or 0.0)
            return price
        except Exception as e:
            logger.warning(f"Error fetching ticker price for {norm_symbol}: {e}")
            return 0.0

    def update_plan_status(
        self,
        plan_id: str,
        new_status: str,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Updates plan status and merges metadata in Supabase."""
        db = self._ensure_db()
        if not db:
            logger.info(f"[Dry Run] Plan {plan_id} status updated to '{new_status}'")
            return True

        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "status": new_status,
            "updated_at": now_iso,
        }

        try:
            # Fetch existing metadata to merge
            current = (
                db.client.table("trade_plans")
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

            db.client.table("trade_plans").update(payload).eq("id", plan_id).execute()
            logger.info(f"Plan {plan_id} transitioned to '{new_status}'")
            return True
        except Exception as e:
            logger.error(f"Failed to update plan {plan_id} status to '{new_status}': {e}")
            return False

    def check_trigger_condition(
        self, direction: str, current_price: float, entry_price: float
    ) -> bool:
        """
        Trigger condition check:
        - LONG order: market drops to or below entry limit (current_price <= entry_price)
        - SHORT order: market rises to or above entry limit (current_price >= entry_price)
        """
        if direction == "LONG":
            return current_price <= entry_price
        else:  # SHORT
            return current_price >= entry_price

    def evaluate_plan(self, plan: Dict[str, Any]) -> str:
        """
        Evaluates a single PENDING trade plan.
        Returns:
            'TRIGGERED' if triggered and sent to executor
            'TIMEOUT' if expired after 15 minutes
            'PENDING' if still waiting
            'ERROR' if evaluation failed
        """
        plan_id = plan.get("id")
        symbol = plan.get("symbol")
        direction = str(plan.get("direction", "SHORT")).upper()
        entry_price = float(plan.get("entry_price", 0.0))
        created_at_str = plan.get("created_at")

        # Task 4: Cool-Down Timer Check
        db = self._ensure_db()
        if db:
            cool_down_raw = None
            if hasattr(db, "get_cool_down_until"):
                cool_down_raw = db.get_cool_down_until()
            else:
                try:
                    res = db.client.table("system_state").select("cool_down_until").eq("id", 1).limit(1).execute()
                    if res.data and len(res.data) > 0:
                        cool_down_raw = res.data[0].get("cool_down_until")
                except Exception:
                    pass

            if cool_down_raw:
                try:
                    cool_dt = datetime.fromisoformat(str(cool_down_raw).replace("Z", "+00:00"))
                    if cool_dt.tzinfo is None:
                        cool_dt = cool_dt.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) < cool_dt:
                        logger.warning(
                            f"[COOL DOWN ACTIVE] Ignoring plan {plan_id} due to active cool-down until {cool_down_raw}."
                        )
                        return "COOL_DOWN"
                except Exception as e:
                    logger.debug(f"Failed to parse cool_down_until: {e}")

        # 1. Check for 15-minute Timeout
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except Exception:
            created_at = datetime.now(timezone.utc)

        elapsed_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()

        if elapsed_seconds > self.TIMEOUT_SECONDS:
            logger.warning(
                f"[TIMEOUT] Plan {plan_id[:8]}.. ({symbol} {direction} @ {entry_price:.2f}) "
                f"unfilled after {elapsed_seconds / 60:.1f} mins. Marking TIMEOUT."
            )
            self.update_plan_status(
                plan_id,
                "TIMEOUT",
                {
                    "timeout_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": elapsed_seconds,
                    "reason": "Exceeded 15-minute sniper window",
                },
            )
            return "TIMEOUT"

        # 2. Fetch Live Price
        current_price = self.fetch_current_price(symbol)
        if current_price <= 0:
            logger.error(
                f"[STALE/INVALID TICKER] Could not retrieve valid fresh price for {symbol}. "
                f"Skipping trigger evaluation and executing zero trades."
            )
            return "ERROR"

        # 3. Check Trigger Condition
        triggered = self.check_trigger_condition(direction, current_price, entry_price)

        logger.info(
            f"Sniper Watch: {symbol} {direction} | Entry: {entry_price:.2f} | "
            f"Live: {current_price:.2f} | Elapsed: {elapsed_seconds:.0f}s/{self.TIMEOUT_SECONDS}s | "
            f"Triggered: {triggered}"
        )

        if triggered:
            now_iso = datetime.now(timezone.utc).isoformat()
            # Adverse selection check: 30% chance price crosses entry limit but immediately reverses
            if self.check_adverse_selection():
                logger.warning(
                    f"[ADVERSE SELECTION] Plan {plan_id[:8]}.. {symbol} {direction} crossed entry "
                    f"@ {entry_price:.2f} but immediately reversed. Simulating missed fill -> REJECTED."
                )
                self.update_plan_status(
                    plan_id,
                    "REJECTED",
                    {
                        "rejected_at": now_iso,
                        "reason": "ADVERSE_SELECTION_REVERSAL",
                        "trigger_price": current_price,
                        "elapsed_seconds": elapsed_seconds,
                    },
                )
                return "REJECTED"

            logger.info(
                f"*** [SNIPER TRIGGERED] Plan {plan_id[:8]}.. {symbol} {direction} HIT @ {current_price:.2f}! ***"
            )

            # Ensure in-memory plan_metadata includes triggered_at
            if "plan_metadata" not in plan or not isinstance(plan.get("plan_metadata"), dict):
                plan["plan_metadata"] = {}
            plan["plan_metadata"]["triggered_at"] = now_iso
            plan["plan_metadata"]["trigger_price"] = current_price
            plan["plan_metadata"]["elapsed_seconds"] = elapsed_seconds

            # Update status to TRIGGERED
            self.update_plan_status(
                plan_id,
                "TRIGGERED",
                {
                    "triggered_at": now_iso,
                    "trigger_price": current_price,
                    "elapsed_seconds": elapsed_seconds,
                },
            )

            # Send to PaperExecutor for immediate fill
            if hasattr(self.executor, "fill_triggered_plan"):
                self.executor.fill_triggered_plan(plan, fill_price=current_price)
            elif hasattr(self.executor, "fill_plan"):
                self.executor.fill_plan(plan, fill_price=current_price)
            else:
                self.executor.evaluate_plan(plan)

            return "TRIGGERED"

        return "PENDING"

    def poll_once(self) -> Dict[str, int]:
        """Runs a single polling sweep over all PENDING plans."""
        if not self._schema_probed and self._ensure_db():
            self._schema_probed = True
            self._probe_schema()

        db = self._ensure_db()
        if db and db.check_halt():
            logger.warning("[KILL SWITCH] System is HALTED in Supabase. Skipping poller sweep.")
            return {"evaluated": 0, "triggered": 0, "timeout": 0}

        # Task 4: Cool-Down Timer Check
        if db:
            cool_down_raw = None
            if hasattr(db, "get_cool_down_until"):
                cool_down_raw = db.get_cool_down_until()
            else:
                try:
                    res = db.client.table("system_state").select("cool_down_until").eq("id", 1).limit(1).execute()
                    if res.data and len(res.data) > 0:
                        cool_down_raw = res.data[0].get("cool_down_until")
                except Exception:
                    pass

            if cool_down_raw:
                try:
                    cool_dt = datetime.fromisoformat(str(cool_down_raw).replace("Z", "+00:00"))
                    if cool_dt.tzinfo is None:
                        cool_dt = cool_dt.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) < cool_dt:
                        logger.warning(
                            f"[COOL DOWN ACTIVE] System in cool-down until {cool_down_raw}. Ignoring all plans."
                        )
                        return {"evaluated": 0, "triggered": 0, "timeout": 0}
                except Exception as e:
                    logger.debug(f"Failed to parse cool_down_until: {e}")

        pending = self.get_pending_plans()
        if not pending:
            return {"evaluated": 0, "triggered": 0, "timeout": 0}

        logger.info(f"Sniper Poller checking {len(pending)} PENDING plan(s)...")
        counts = {"evaluated": len(pending), "triggered": 0, "timeout": 0}

        for plan in pending:
            result = self.evaluate_plan(plan)
            if result == "TRIGGERED":
                counts["triggered"] += 1
            elif result == "TIMEOUT":
                counts["timeout"] += 1

        return counts

    def run_continuous(self, interval_seconds: int = 10) -> None:
        """Runs the continuous polling loop every 10 seconds."""
        logger.info(f"Starting Sniper Trigger Poller loop (cadence: {interval_seconds}s)...")
        while self.running:
            start_t = time.perf_counter()
            db = self._ensure_db()
            try:
                self.poll_once()
                latency = (time.perf_counter() - start_t) * 1000.0
                if db and hasattr(db, "update_agent_health"):
                    status = "WARNING" if latency > 2000 else "HEALTHY"
                    db.update_agent_health("TriggerPoller", status, latency, None)
            except Exception as e:
                latency = (time.perf_counter() - start_t) * 1000.0
                logger.error(f"Error in Trigger Poller loop: {e}")
                if db and hasattr(db, "update_agent_health"):
                    db.update_agent_health("TriggerPoller", "CRITICAL", latency, str(e))

            # Sleep in 1-second chunks for rapid shutdown response
            for _ in range(interval_seconds):
                if not self.running:
                    break
                time.sleep(1)

    def stop(self) -> None:
        """Stops the poller loop."""
        self.running = False


if __name__ == "__main__":
    poller = TriggerPoller()
    res = poller.poll_once()
    print("Trigger Poller single sweep complete:", res)
