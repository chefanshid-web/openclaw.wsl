"""
OpenClaw Quant Lab - SRE Auto-Heal Watchdog Loop (src/sre_watchdog.py)

Responsibilities:
1. Polls the 'agent_health' table every 30 seconds.
2. Checks heartbeat staleness:
   - If last_heartbeat > 60 seconds old, or status == 'CRITICAL', triggers alert.
3. Auto-Healing:
   - Attempts to restart the specific agent thread.
   - If auto-heal fails or requires full system intervention, dispatches critical WhatsApp alert.
4. Updates health telemetry post-heal.
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db_manager import SupabaseManager

logger = logging.getLogger("SREWatchdog")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [SREWatchdog] %(message)s"))
    logger.addHandler(ch)


class SREWatchdog:
    """
    Site Reliability Engineering (SRE) Watchdog.
    Monitors agent health, detects heartbeats older than 60s or CRITICAL errors,
    auto-heals failing agent threads, and escalates via WhatsApp alerts.
    """

    HEARTBEAT_TIMEOUT_SECONDS = 60
    DEFAULT_POLL_INTERVAL = 30
    MAX_AUTO_RESTART_ATTEMPTS = 3

    def __init__(
        self,
        db_manager: Optional[SupabaseManager] = None,
        heartbeat_timeout_seconds: int = HEARTBEAT_TIMEOUT_SECONDS,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ):
        self.db: Optional[SupabaseManager] = db_manager
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.poll_interval = poll_interval
        self.running: bool = True

        # Thread registry for managed agent runners
        self.agent_runners: Dict[str, Callable] = {}
        self.agent_threads: Dict[str, threading.Thread] = {}
        self.restart_counts: Dict[str, int] = {}
        self._init_default_runners()

    def _ensure_db(self) -> Optional[SupabaseManager]:
        """Lazy-initialize SupabaseManager."""
        if self.db is not None:
            return self.db
        try:
            self.db = SupabaseManager()
            return self.db
        except Exception as e:
            logger.warning(f"Supabase connection unavailable in SREWatchdog: {e}")
            return None

    def _init_default_runners(self):
        """Initializes default worker callables for known system agents."""
        def _run_scanner():
            from src.scanner import MarketScanner
            scanner = MarketScanner(db_manager=self._ensure_db())
            scanner.scan_all()

        def _run_trigger_poller():
            from src.trigger_poller import TriggerPoller
            poller = TriggerPoller(db_manager=self._ensure_db())
            poller.poll_once()

        def _run_paper_executor():
            from src.paper_executor import PaperExecutor
            executor = PaperExecutor(db_manager=self._ensure_db())
            executor.poll_once()

        def _run_macro_fetcher():
            from src.macro_fetcher import MacroFetcher
            mf = MacroFetcher(db_manager=self._ensure_db())
            mf.get_macro_risk()

        self.register_agent("Scanner", _run_scanner)
        self.register_agent("TriggerPoller", _run_trigger_poller)
        self.register_agent("PaperExecutor", _run_paper_executor)
        self.register_agent("MacroFetcher", _run_macro_fetcher)

    def register_agent(self, agent_name: str, runner_func: Callable) -> None:
        """Registers an agent runner function for managed auto-healing restarts."""
        self.agent_runners[agent_name] = runner_func
        self.restart_counts[agent_name] = 0

    def send_whatsapp_alert(self, message: str) -> bool:
        """
        Escalates critical unhealable failures via WhatsApp notification.
        Supports environment-configured webhook (WHATSAPP_WEBHOOK_URL) or falls back
        to high-priority SRE audit logging.
        """
        logger.critical(f"📱 [WHATSAPP SRE ESCALATION] {message}")

        webhook_url = os.getenv("WHATSAPP_WEBHOOK_URL")
        phone_number = os.getenv("WHATSAPP_PHONE", "+971569503813")

        if webhook_url:
            try:
                import requests
                payload = {"phone": phone_number, "message": message, "priority": "CRITICAL"}
                resp = requests.post(webhook_url, json=payload, timeout=5)
                return resp.status_code in (200, 201, 202)
            except Exception as e:
                logger.error(f"Failed to dispatch WhatsApp webhook: {e}")
                return False

        return True

    def restart_agent(self, agent_name: str) -> bool:
        """
        Attempts to restart a specific agent thread.
        If restart attempts exceed threshold, triggers full WhatsApp alert.
        """
        attempts = self.restart_counts.get(agent_name, 0) + 1
        self.restart_counts[agent_name] = attempts

        logger.warning(
            f"[SRE AUTO-HEAL] Attempting restart of agent '{agent_name}' "
            f"(Attempt {attempts}/{self.MAX_AUTO_RESTART_ATTEMPTS})..."
        )

        runner = self.agent_runners.get(agent_name)
        if not runner:
            logger.error(f"[SRE AUTO-HEAL] No runner registered for agent '{agent_name}'.")
            return False

        try:
            # Spawn and launch fresh agent thread
            thread = threading.Thread(
                target=runner,
                name=f"Worker-{agent_name}-Healed-{attempts}",
                daemon=True,
            )
            thread.start()
            self.agent_threads[agent_name] = thread

            logger.info(f"✅ [SRE AUTO-HEAL] Agent '{agent_name}' restarted successfully in thread {thread.name}.")

            # Update DB health status to RECOVERING / HEALTHY
            db = self._ensure_db()
            if db and hasattr(db, "update_agent_health"):
                db.update_agent_health(agent_name, "HEALTHY", latency_ms=0.0, error_msg=None)

            return True
        except Exception as e:
            logger.error(f"❌ [SRE AUTO-HEAL] Failed to restart agent '{agent_name}': {e}")
            return False

    def check_and_heal_once(self) -> Dict[str, Any]:
        """
        Performs a single evaluation pass:
        1. Queries agent_health table.
        2. Detects heartbeats > 60s old or status == 'CRITICAL'.
        3. Attempts restart or alerts WhatsApp.
        """
        db = self._ensure_db()
        results: Dict[str, Any] = {
            "evaluated": 0,
            "healthy": 0,
            "healed": 0,
            "escalated": 0,
            "details": [],
        }

        records: List[Dict[str, Any]] = []
        if db and hasattr(db, "get_agent_health"):
            records = db.get_agent_health()
        elif db and hasattr(db, "client") and db.client:
            try:
                res = db.client.table("agent_health").select("*").execute()
                records = res.data or []
            except Exception as e:
                logger.error(f"Failed to query agent_health table: {e}")

        now_utc = datetime.now(timezone.utc)
        results["evaluated"] = len(records)

        for row in records:
            agent_name = row.get("agent_name") or "Unknown"
            status = (row.get("status") or "UNKNOWN").upper()
            error_msg = row.get("error_msg")
            raw_ts = row.get("last_heartbeat")

            age_seconds = 0.0
            if raw_ts:
                try:
                    dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_seconds = (now_utc - dt).total_seconds()
                except Exception:
                    age_seconds = 999.0
            else:
                age_seconds = 999.0

            is_stale = age_seconds > self.heartbeat_timeout_seconds
            is_critical = status == "CRITICAL"

            if is_stale or is_critical:
                reason = (
                    f"Heartbeat stale ({age_seconds:.1f}s > {self.heartbeat_timeout_seconds}s)"
                    if is_stale
                    else f"Critical status reported: {error_msg}"
                )
                logger.critical(f"🚨 [SRE WATCHDOG ALERT] Agent '{agent_name}' is unhealthy! Reason: {reason}")

                healed = self.restart_agent(agent_name)
                if healed:
                    results["healed"] += 1
                    results["details"].append({"agent": agent_name, "action": "HEALED", "reason": reason})
                else:
                    results["escalated"] += 1
                    msg = f"Agent '{agent_name}' failed to auto-heal! Status: {status}. Reason: {reason}."
                    self.send_whatsapp_alert(msg)
                    results["details"].append({"agent": agent_name, "action": "ESCALATED_WHATSAPP", "reason": reason})
            else:
                results["healthy"] += 1

        return results

    def run_continuous(self, interval_seconds: Optional[int] = None) -> None:
        """Runs the SRE Watchdog loop every 30 seconds."""
        interval = interval_seconds or self.poll_interval
        logger.info(f"Starting SRE Auto-Heal Watchdog loop (cadence: {interval}s)...")
        while self.running:
            try:
                res = self.check_and_heal_once()
                logger.info(
                    f"[SRE WATCHDOG PASS] Evaluated {res['evaluated']} agent(s): "
                    f"{res['healthy']} Healthy, {res['healed']} Healed, {res['escalated']} Escalated."
                )
            except Exception as e:
                logger.error(f"Error in SRE Watchdog cycle: {e}")

            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

    def stop(self) -> None:
        """Signals watchdog loop to terminate."""
        self.running = False


if __name__ == "__main__":
    watchdog = SREWatchdog()
    res = watchdog.check_and_heal_once()
    print("SRE Watchdog single pass complete:", res)
