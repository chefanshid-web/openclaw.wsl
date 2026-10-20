"""
OpenClaw Quant Lab - Master Runner Daemon (run_lab.py)
Phases 1-3: Scanner, Macro/Scorer, Sniper Poller, Paper Execution

Cadence:
1. Continuous background worker: TriggerPoller checks PENDING plans every 10 seconds.
2. Continuous background worker: PaperExecutor executes simulated fills every 10 seconds.
3. Scheduled cycle (every 15 minutes):
   - Check Macro Status & Weekend Ban via MacroFetcher.
   - Run 4H MarketScanner across universe.
   - Run TradeScorer with Macro Confluence Gating to populate trade plans.
"""

import sys
import os
import time
import signal
import threading
import logging
import hashlib
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from src.db_manager import SupabaseManager
from src.macro_fetcher import MacroFetcher
from src.scanner import MarketScanner
from src.scorer import TradeScorer
from src.paper_executor import PaperExecutor
from src.trigger_poller import TriggerPoller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("MasterRunner")

# Global stop flag
SHUTDOWN_EVENT = threading.Event()


def signal_handler(signum, frame):
    """Graceful shutdown signal trap."""
    logger.warning("Shutdown signal received. Stopping OpenClaw Quant Lab...")
    SHUTDOWN_EVENT.set()


def paper_executor_thread(executor: PaperExecutor):
    """Background worker polling trade_plans every 10 seconds."""
    logger.info("Paper Executor background thread started.")
    while not SHUTDOWN_EVENT.is_set():
        if check_architect_flag():
            break
        try:
            executor.poll_once()
        except Exception as e:
            logger.error(f"Unhandled error in Paper Executor thread: {e}")

        for _ in range(10):
            if SHUTDOWN_EVENT.is_set():
                break
            time.sleep(1)

    logger.info("Paper Executor thread finished.")


def trigger_poller_thread(poller: TriggerPoller):
    """Background worker polling sniper entry triggers every 10 seconds."""
    logger.info("Sniper Trigger Poller background thread started.")
    while not SHUTDOWN_EVENT.is_set():
        if check_architect_flag():
            break
        try:
            poller.poll_once()
        except Exception as e:
            logger.error(f"Unhandled error in Trigger Poller thread: {e}")

        for _ in range(10):
            if SHUTDOWN_EVENT.is_set():
                break
            time.sleep(1)

    logger.info("Trigger Poller thread finished.")



PROJECT_ROOT = Path(__file__).resolve().parent
APPROVED_HASH_FILE = PROJECT_ROOT / "approved_hash.txt"
GEMINI_FLAG_FILE = PROJECT_ROOT / "GEMINI_FLAG.md"
SRC_DIR = PROJECT_ROOT / "src"


def calculate_src_hash(src_dir: Optional[Path] = None) -> str:
    """
    Calculates a deterministic SHA-256 hash of all files in the src/ directory.
    Ignores __pycache__ and byte-compiled files (.pyc, .pyo).
    """
    target_dir = Path(src_dir) if src_dir else SRC_DIR
    sha = hashlib.sha256()

    for root, dirs, files in os.walk(target_dir):
        dirs.sort()
        files.sort()
        for filename in files:
            if filename.endswith((".pyc", ".pyo")) or "__pycache__" in root:
                continue
            file_path = os.path.join(root, filename)
            rel_path = os.path.relpath(file_path, target_dir).replace("\\", "/")
            sha.update(rel_path.encode("utf-8"))
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(65536):
                        sha.update(chunk)
            except Exception as e:
                logger.warning(f"Error hashing file {file_path}: {e}")

    return sha.hexdigest()


def save_approved_hash(hash_val: Optional[str] = None, output_file: Optional[Path] = None) -> str:
    """Calculates SHA-256 hash of src/ (if not provided) and saves it to approved_hash.txt."""
    if hash_val is None:
        hash_val = calculate_src_hash()
    target_file = Path(output_file) if output_file else APPROVED_HASH_FILE
    target_file.write_text(hash_val.strip(), encoding="utf-8")
    logger.info(f"Approved code hash saved to {target_file}: {hash_val}")
    return hash_val


def verify_code_integrity() -> Tuple[bool, str]:
    """
    Verifies code integrity:
    1. Checks if GEMINI_FLAG.md exists.
    2. Checks if approved_hash.txt exists and matches current SHA-256 of src/.
    Returns (is_valid: bool, reason: str).
    """
    if GEMINI_FLAG_FILE.exists():
        return False, f"Architect flag detected ({GEMINI_FLAG_FILE.name}). Trading halted until reviewed."

    if not APPROVED_HASH_FILE.exists():
        return False, f"Approved code hash file ({APPROVED_HASH_FILE.name}) missing. Code is unverified."

    approved_hash = APPROVED_HASH_FILE.read_text(encoding="utf-8").strip()
    current_hash = calculate_src_hash()

    if current_hash != approved_hash:
        return False, (
            f"Code hash mismatch! Current hash ({current_hash}) does not match "
            f"approved hash ({approved_hash}). Unauthorized code modifications detected."
        )

    return True, "Code integrity verified against approved hash."


def check_architect_flag() -> bool:
    """
    Integrity Gate:
    Checks if GEMINI_FLAG.md exists OR if current code hash does not match approved_hash.txt.
    Returns True if compromised/flagged (halting execution), False if verified.
    """
    is_valid, reason = verify_code_integrity()
    if not is_valid:
        red_start = "\033[91m\033[1m"
        red_end = "\033[0m"
        warning = (
            f"\n{red_start}===========================================================================\n"
            f"   HALT: CODE INTEGRITY / ARCHITECT FLAG VIOLATION!\n"
            f"   {reason}\n"
            f"==========================================================================={red_end}\n"
        )
        print(warning)
        logger.critical(f"HALT: {reason}")
        return True
    return False

def run_lab(scan_interval_minutes: int = 15):
    """Main orchestration routine."""
    if check_architect_flag():
        return
    print("=" * 75)
    print("   OPENCLAW QUANT LAB - 24/7 PAPER TRADING DESK (PHASES 1-3)   ")
    print("=" * 75)

    # 1. Initialize Shared Cloud DB Manager
    try:
        db = SupabaseManager()
        logger.info("[+] SupabaseManager initialized successfully.")
    except Exception as e:
        logger.warning(f"[!] Supabase initialization failed ({e}). Proceeding in offline/dry-run mode.")
        db = None

    # 2. Instantiate Components
    macro_fetcher = MacroFetcher(db_manager=db)
    scanner = MarketScanner(db_manager=db)
    scorer = TradeScorer(db_manager=db, macro_fetcher=macro_fetcher)
    executor = PaperExecutor(db_manager=db)
    poller = TriggerPoller(db_manager=db, executor=executor)

    # 3. Ensure Trade Plans Schema is Ready
    scorer.init_trade_plans_table()

    # 4. Launch Background Workers (10s cadence)
    executor_worker = threading.Thread(
        target=paper_executor_thread, args=(executor,), daemon=True, name="PaperExecutorWorker"
    )
    executor_worker.start()

    poller_worker = threading.Thread(
        target=trigger_poller_thread, args=(poller,), daemon=True, name="TriggerPollerWorker"
    )
    poller_worker.start()

    # 5. Main Loop: Macro Check -> Scanner -> Scorer every 15 minutes
    interval_seconds = scan_interval_minutes * 60
    logger.info(f"Scanner/Scorer scheduled cadence: every {scan_interval_minutes} minutes.")

    while not SHUTDOWN_EVENT.is_set():
        if check_architect_flag():
            break
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.info(f"--- [CYCLE START] {now_str} ---")

            # -------------------------------------------------------------
            # STEP 1: MACRO REGIME & WEEKEND BAN CHECK
            # -------------------------------------------------------------
            macro_risk = macro_fetcher.get_macro_risk()
            macro_score = macro_risk.get("score")
            is_macro_valid = macro_risk.get("is_valid", False)
            macro_reason = macro_risk.get("reason", "")

            if macro_score == -999 or macro_fetcher.is_weekend():
                macro_regime_str = "WEEKEND HALT"
            elif macro_score == -1 or not is_macro_valid:
                macro_regime_str = "TELEMETRY OFFLINE"
            elif macro_score == 10:
                macro_regime_str = "RISK ON"
            elif macro_score == 0:
                macro_regime_str = "RISK OFF"
            else:
                macro_regime_str = "NEUTRAL"

            print("\n" + "=" * 60)
            print(f"   [MACRO REGIME]: {macro_regime_str}")
            print(f"   Macro Risk Score: {macro_score}")
            print(f"   Status / Reason : {macro_reason}")
            print("=" * 60 + "\n")

            # Check Global Kill Switch
            if db and db.check_halt():
                logger.warning("[KILL SWITCH] System is HALTED in Supabase. Skipping scan cycle.")
            elif not is_macro_valid or macro_score < 0:
                logger.warning(
                    f"[HALT ACTIVE] {macro_regime_str}: Market trading suppressed. "
                    f"Scanning for telemetry observations only (no new orders)."
                )
                logger.info("Step A: Executing MarketScanner for telemetry observation...")
                scanner.scan_all()

                logger.info("Step B: Executing TradeScorer (Macro Invariant Gate will enforce block)...")
                scorer.process_latest(macro_fetcher=macro_fetcher)
            else:
                # Normal operational cycle
                logger.info("Step A: Executing MarketScanner across universe...")
                scanner.scan_all()

                logger.info("Step B: Executing TradeScorer with Macro Confluence...")
                scorer.process_latest(macro_fetcher=macro_fetcher)

            logger.info(f"Cycle completed. Next scan in {scan_interval_minutes} minutes.")

        except Exception as e:
            logger.error(f"Error during scan/score cycle: {e}")

        # Wait until next 15-minute interval, checking for shutdown
        countdown = interval_seconds
        while countdown > 0 and not SHUTDOWN_EVENT.is_set():
            time.sleep(1)
            countdown -= 1

    logger.info("OpenClaw Quant Lab shut down gracefully.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    run_lab()
