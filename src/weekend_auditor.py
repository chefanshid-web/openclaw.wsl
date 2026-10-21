"""
OpenClaw Quant Lab - Phase 4: The Weekend Auditor
File: src/weekend_auditor.py

Responsibilities:
1. The Time Gate:
   - Evaluates datetime.now(timezone.utc).
   - Only runs on Saturdays. Exits with 'Weekend Auditor only runs on Saturdays' otherwise.
2. The Data Gatherer:
   - Connects to Supabase.
   - Fetches trade_plans from the last 7 days where status is 'FILLED' or 'TIMEOUT'.
   - Fetches signals from the last 7 days where signal_type is 'PAPER_FILL'.
   - Computes basic stats: Total Trades, Win Rate, Total PnL (if available), Average Score of winners vs losers.
3. The Analysis (The 60-70 Misses):
   - Queries trade_plans from the last 7 days where status is 'PENDING' or 'TIMEOUT',
     with source_signal_score between 60 and 69 ('Near Misses').
   - Prints count of Near Misses.
4. The Output File:
   - Generates markdown report: reports/weekend_audit_YYYY-MM-DD.md
   - Sections: Weekly Stats, The 'Missed Trades' (Score 60-69), The 'Bad Trades' (Score > 70 that lost),
     and '## Proposed Rule' with '*Pending Hunter Agent Review*'.
5. The Backtest Gate (The Lazy Fix):
   - test_proposed_rule(rule_description: str) placeholder function.
"""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

# Ensure project root is in sys.path for direct script execution
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.db_manager import SupabaseManager
from src.backtest.metrics import (
    calculate_bootstrap_confidence,
    calculate_expectancy,
)

logger = logging.getLogger("WeekendAuditor")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [WeekendAuditor] %(message)s"))
    logger.addHandler(ch)


def is_saturday(dt: Optional[datetime] = None) -> bool:
    """
    Checks whether the specified datetime (or current UTC) falls on Saturday.
    In Python's weekday(): Monday is 0, Saturday is 5, Sunday is 6.
    """
    now_utc = dt or datetime.now(timezone.utc)
    return now_utc.weekday() == 5


def test_proposed_rule(rule_description: str) -> None:
    """
    Validates a proposed LLM rule against historical data before deployment.
    Lazy fix placeholder - to be connected to src/backtest/cpcv.py.
    """
    print(
        "Warning: Any rule proposed by the LLM must be validated here against "
        "3 months of historical data before being added to library_active.md."
    )

# Prevent pytest from collecting this helper function as a test case
test_proposed_rule.__test__ = False


class WeekendAuditor:
    """
    Phase 4: Weekend Auditor Engine.
    Runs every Saturday to audit the past 7 days of paper execution,
    analyze near misses (scores 60-69) and bad trades (score > 70 that lost),
    and output weekly performance audit markdown reports.
    """

    def __init__(
        self,
        db_manager: Optional[SupabaseManager] = None,
        reports_dir: str = "reports",
    ):
        self.db: Optional[SupabaseManager] = db_manager
        self.reports_dir: str = reports_dir

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
        """Probes trade_plans table to log column and metadata keys for runtime observability."""
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

    def gather_data(self, as_of_date: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Fetches trade plans and signals from the last 7 days from Supabase.
        """
        now_dt = as_of_date or datetime.now(timezone.utc)
        cutoff_dt = now_dt - timedelta(days=7)
        cutoff_iso = cutoff_dt.isoformat()

        db = self._ensure_db()
        if not db:
            logger.warning("No database connection available for gather_data.")
            return {
                "all_plans": [],
                "filled_or_timeout_plans": [],
                "paper_fills": [],
            }
        self._probe_schema()

        all_plans: List[Dict[str, Any]] = []
        try:
            res_plans = (
                db.client.table("trade_plans")
                .select("*")
                .gte("created_at", cutoff_iso)
                .order("created_at", desc=True)
                .execute()
            )
            all_plans = res_plans.data or []
        except Exception as e:
            logger.error(f"Error fetching trade plans from Supabase: {e}")

        # Filter for status in ('FILLED', 'TIMEOUT')
        filled_or_timeout = [
            p for p in all_plans if p.get("status") in ("FILLED", "TIMEOUT")
        ]

        paper_fills: List[Dict[str, Any]] = []
        try:
            res_signals = (
                db.client.table("signals")
                .select("*")
                .gte("event_time", cutoff_iso)
                .eq("signal_type", "PAPER_FILL")
                .order("event_time", desc=True)
                .execute()
            )
            paper_fills = res_signals.data or []
        except Exception as e:
            logger.error(f"Error fetching signals from Supabase: {e}")

        return {
            "all_plans": all_plans,
            "filled_or_timeout_plans": filled_or_timeout,
            "paper_fills": paper_fills,
        }

    def find_missed_trades(self, plans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Identifies 'Near Misses': trade_plans from the last 7 days where status is
        'PENDING' or 'TIMEOUT', but source_signal_score is between 60 and 69.
        """
        missed: List[Dict[str, Any]] = []
        for plan in plans:
            status = plan.get("status")
            if status not in ("PENDING", "TIMEOUT"):
                continue

            meta = plan.get("plan_metadata") or {}
            score = meta.get("source_signal_score")
            if score is None:
                score = meta.get("score")

            if score is not None:
                try:
                    val = float(score)
                    if 60.0 <= val <= 69.999:
                        missed.append(plan)
                except (ValueError, TypeError):
                    continue
        return missed

    def find_bad_trades(self, plans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Identifies 'Bad Trades': trades with score > 70 that resulted in a loss.
        """
        bad_trades: List[Dict[str, Any]] = []
        for plan in plans:
            meta = plan.get("plan_metadata") or {}
            score = meta.get("confluence_score")
            if score is None:
                score = meta.get("source_signal_score")
            if score is None:
                score = meta.get("score")

            score_val: Optional[float] = None
            if score is not None:
                try:
                    score_val = float(score)
                except (ValueError, TypeError):
                    pass

            if score_val is not None and score_val >= 70.0:
                pnl = meta.get("realized_pnl")
                if pnl is None:
                    pnl = meta.get("pnl")
                outcome = str(meta.get("outcome") or "").upper()
                status = str(plan.get("status") or "").upper()

                is_loss = False
                if pnl is not None:
                    try:
                        is_loss = float(pnl) < 0.0
                    except (ValueError, TypeError):
                        pass
                elif outcome in ("LOSS", "STOP_LOSS", "SL"):
                    is_loss = True
                elif status == "STOP_LOSS_HIT":
                    is_loss = True

                if is_loss:
                    bad_trades.append(plan)

        return bad_trades

    def calculate_stats(
        self,
        filled_plans: List[Dict[str, Any]],
        paper_fills: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Calculates stats: Total Trades, Win Rate with 95% Bootstrap CI,
        Net Expectancy, Total PnL, Average Score of winners vs losers.
        """
        total_trades = len(filled_plans) if filled_plans else len(paper_fills)

        winner_scores: List[float] = []
        loser_scores: List[float] = []
        winner_pnls: List[float] = []
        loser_pnls: List[float] = []
        trade_returns: List[float] = []
        costs_list: List[float] = []
        total_pnl: float = 0.0
        has_pnl_data: bool = False

        for plan in filled_plans:
            meta = plan.get("plan_metadata") or {}
            score = meta.get("confluence_score") or meta.get("source_signal_score")
            score_val: Optional[float] = None
            if score is not None:
                try:
                    score_val = float(score)
                except (ValueError, TypeError):
                    pass

            pnl = meta.get("realized_pnl")
            if pnl is None:
                pnl = meta.get("pnl")
            outcome = str(meta.get("outcome") or "").upper()

            # Cost / fee extraction
            fee = meta.get("fee_cost")
            if fee is None:
                fee = meta.get("cost")
            if fee is None:
                fee = meta.get("fees")
            if fee is None:
                fee = plan.get("fee_cost")
            if fee is None:
                fee = plan.get("cost")
            if fee is not None:
                try:
                    costs_list.append(abs(float(fee)))
                except (ValueError, TypeError):
                    pass

            if pnl is not None:
                try:
                    pnl_num = float(pnl)
                    total_pnl += pnl_num
                    has_pnl_data = True
                    trade_returns.append(pnl_num)
                    if pnl_num > 0:
                        winner_pnls.append(pnl_num)
                        if score_val is not None:
                            winner_scores.append(score_val)
                    elif pnl_num < 0:
                        loser_pnls.append(abs(pnl_num))
                        if score_val is not None:
                            loser_scores.append(score_val)
                except (ValueError, TypeError):
                    pass
            elif outcome in ("WIN", "TAKE_PROFIT", "TP"):
                trade_returns.append(1.0)
                winner_pnls.append(1.0)
                if score_val is not None:
                    winner_scores.append(score_val)
            elif outcome in ("LOSS", "STOP_LOSS", "SL"):
                trade_returns.append(-1.0)
                loser_pnls.append(1.0)
                if score_val is not None:
                    loser_scores.append(score_val)

        closed_evaluated = len(winner_scores) + len(loser_scores)
        win_count = len(winner_scores)
        loss_count = len(loser_scores)

        # Point estimates
        win_rate_pct = (win_count / closed_evaluated * 100.0) if closed_evaluated > 0 else 0.0
        loss_rate_pct = (loss_count / closed_evaluated * 100.0) if closed_evaluated > 0 else 0.0
        win_rate_str = (
            f"{win_rate_pct:.1f}%"
            if closed_evaluated > 0
            else "N/A (No closed trades with outcome)"
        )
        total_pnl_str = (
            f"{total_pnl:+.2f} USDT"
            if has_pnl_data
            else "N/A (Paper execution tracking; position exits pending)"
        )
        avg_winner_score_str = (
            f"{sum(winner_scores) / len(winner_scores):.1f}"
            if winner_scores
            else "N/A"
        )
        avg_loser_score_str = (
            f"{sum(loser_scores) / len(loser_scores):.1f}"
            if loser_scores
            else "N/A"
        )

        # Task 1: Bootstrap Confidence Intervals
        bootstrap_res: Optional[Dict[str, Any]] = None
        win_rate_ci: Optional[Tuple[float, float]] = None
        sharpe_ci: Optional[Tuple[float, float]] = None
        if trade_returns:
            bootstrap_res = calculate_bootstrap_confidence(trade_returns, n_bootstraps=1000)
            win_rate_ci = bootstrap_res.get("win_rate_ci")
            sharpe_ci = bootstrap_res.get("sharpe_ci")

        if closed_evaluated > 0 and win_rate_ci:
            win_rate_display = f"{win_rate_pct:.1f}% (95% CI: [{win_rate_ci[0]:.1f}%, {win_rate_ci[1]:.1f}%])"
        else:
            win_rate_display = win_rate_str

        # Task 2: Net Expectancy calculation
        # Expectancy = (Win_Rate * Avg_Win_USD) - (Loss_Rate * Avg_Loss_USD) - Avg_Costs_USD
        avg_win_usd = (sum(winner_pnls) / len(winner_pnls)) if winner_pnls else 0.0
        avg_loss_usd = (sum(loser_pnls) / len(loser_pnls)) if loser_pnls else 0.0
        avg_costs_usd = (sum(costs_list) / len(costs_list)) if costs_list else 0.0

        if closed_evaluated > 0:
            expectancy = calculate_expectancy(
                win_rate=win_rate_pct,
                avg_win_usd=avg_win_usd,
                loss_rate=loss_rate_pct,
                avg_loss_usd=avg_loss_usd,
                avg_costs_usd=avg_costs_usd,
            )
            expectancy_str = f"{expectancy:+.2f} USD"
            has_negative_expectancy = expectancy < 0.0
        else:
            expectancy = None
            expectancy_str = "N/A (No closed trades with outcome)"
            has_negative_expectancy = False

        return {
            "total_trades": total_trades,
            "win_rate": win_rate_str,
            "win_rate_display": win_rate_display,
            "win_rate_ci": win_rate_ci,
            "sharpe_ci": sharpe_ci,
            "bootstrap_confidence": bootstrap_res,
            "expectancy": expectancy,
            "expectancy_str": expectancy_str,
            "has_negative_expectancy": has_negative_expectancy,
            "avg_win_usd": avg_win_usd,
            "avg_loss_usd": avg_loss_usd,
            "avg_costs_usd": avg_costs_usd,
            "total_pnl": total_pnl_str,
            "avg_winner_score": avg_winner_score_str,
            "avg_loser_score": avg_loser_score_str,
            "winner_count": len(winner_scores),
            "loser_count": len(loser_scores),
        }

    def generate_report(
        self,
        stats: Dict[str, Any],
        missed_trades: List[Dict[str, Any]],
        bad_trades: List[Dict[str, Any]],
        as_of_date: Optional[datetime] = None,
    ) -> str:
        """
        Writes markdown report to reports/weekend_audit_YYYY-MM-DD.md.
        """
        now_dt = as_of_date or datetime.now(timezone.utc)
        date_str = now_dt.strftime("%Y-%m-%d")
        os.makedirs(self.reports_dir, exist_ok=True)
        filename = f"weekend_audit_{date_str}.md"
        filepath = os.path.join(self.reports_dir, filename)

        win_rate_rep = stats.get("win_rate_display")
        if not win_rate_rep:
            base_wr = stats.get("win_rate", "N/A")
            ci = stats.get("win_rate_ci")
            if ci and base_wr != "N/A" and "(95% CI:" not in str(base_wr):
                win_rate_rep = f"{base_wr} (95% CI: [{ci[0]:.1f}%, {ci[1]:.1f}%])"
            else:
                win_rate_rep = base_wr

        expectancy_val = stats.get("expectancy")
        expectancy_str = stats.get("expectancy_str")
        if expectancy_str is None:
            if expectancy_val is not None:
                expectancy_str = f"{expectancy_val:+.2f} USD"
            else:
                expectancy_str = "N/A"

        has_neg_exp = stats.get("has_negative_expectancy", False)
        if expectancy_val is not None and expectancy_val < 0.0:
            has_neg_exp = True

        # Print massive warning if strategy has negative expectancy
        if has_neg_exp:
            print("\n" + "=" * 70)
            print("🚨 STRATEGY HAS NEGATIVE EXPECTANCY. DO NOT DEPLOY LIVE CAPITAL.")
            print("STRATEGY HAS NEGATIVE EXPECTANCY. DO NOT TRADE.")
            print("=" * 70 + "\n")

        lines = [
            f"# Weekend Audit Report: {date_str}",
            "",
            f"- **Generated At**: {now_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"- **Audit Period**: Last 7 Days ({(now_dt - timedelta(days=7)).strftime('%Y-%m-%d')} to {date_str})",
            "",
        ]

        if has_neg_exp:
            lines.extend([
                "> [!CAUTION]",
                "> ## 🚨 STRATEGY HAS NEGATIVE EXPECTANCY. DO NOT DEPLOY LIVE CAPITAL.",
                "> **STRATEGY HAS NEGATIVE EXPECTANCY. DO NOT TRADE.**",
                f"> **Net Expectancy**: `{expectancy_str}`. Mathematical expectation is negative after friction and win/loss payoff ratios.",
                "",
            ])

        lines.extend([
            "## Weekly Stats",
            f"- **Total Trades**: {stats.get('total_trades', 0)}",
            f"- **Win Rate**: {win_rate_rep}",
            f"- **Net Expectancy**: {expectancy_str}",
            f"- **Total PnL**: {stats.get('total_pnl', 'N/A')}",
            f"- **Average Score (Winners)**: {stats.get('avg_winner_score', 'N/A')}",
            f"- **Average Score (Losers)**: {stats.get('avg_loser_score', 'N/A')}",
            "",
            '## The "Missed Trades" (Score 60-69)',
        ])

        if missed_trades:
            lines.append(f"Total Near Misses: {len(missed_trades)}\n")
            lines.append("| Symbol | Direction | Entry | Score | Status | Created At |")
            lines.append("|---|---|---|---|---|---|")
            for m in missed_trades:
                meta = m.get("plan_metadata") or {}
                s = meta.get("source_signal_score", meta.get("score", "N/A"))
                created = str(m.get("created_at") or "")[:19]
                lines.append(
                    f"| {m.get('symbol')} | {m.get('direction')} | {m.get('entry_price')} | {s} | {m.get('status')} | {created} |"
                )
        else:
            lines.append("No near misses (score 60-69) recorded this week.")

        lines.append("")
        lines.append('## The "Bad Trades" (Score > 70 that lost)')

        if bad_trades:
            lines.append(f"Total Bad Trades: {len(bad_trades)}\n")
            lines.append("| Symbol | Direction | Entry | Score | PnL | Outcome | Created At |")
            lines.append("|---|---|---|---|---|---|---|")
            for b in bad_trades:
                meta = b.get("plan_metadata") or {}
                s = meta.get("confluence_score", meta.get("source_signal_score", "N/A"))
                pnl = meta.get("realized_pnl", meta.get("pnl", "N/A"))
                outcome = meta.get("outcome", b.get("status"))
                created = str(b.get("created_at") or "")[:19]
                lines.append(
                    f"| {b.get('symbol')} | {b.get('direction')} | {b.get('entry_price')} | {s} | {pnl} | {outcome} | {created} |"
                )
        else:
            lines.append("No bad trades (score > 70 that lost) recorded this week.")

        lines.append("")
        lines.append("## Proposed Rule")
        lines.append("*Pending Hunter Agent Review*")
        lines.append("")

        content = "\n".join(lines)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

    def run_audit(
        self,
        force: bool = False,
        as_of_date: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        Executes the weekend audit workflow.
        Returns path to the report file if run, or None if skipped by the time gate.
        """
        now_dt = as_of_date or datetime.now(timezone.utc)
        if not force and now_dt.weekday() != 5:
            print("Weekend Auditor only runs on Saturdays")
            return None

        print(f"=== Starting Weekend Audit ({now_dt.strftime('%Y-%m-%d UTC')}) ===")
        data = self.gather_data(as_of_date=now_dt)
        all_plans = data.get("all_plans", [])
        filled_plans = [p for p in all_plans if p.get("status") == "FILLED"]
        paper_fills = data.get("paper_fills", [])

        stats = self.calculate_stats(filled_plans, paper_fills)
        missed_trades = self.find_missed_trades(all_plans)
        bad_trades = self.find_bad_trades(all_plans)

        print(f"Near Misses (Score 60-69): {len(missed_trades)}")
        print(f"Bad Trades (Score > 70 Lost): {len(bad_trades)}")
        print(f"Total Trades Evaluated: {stats.get('total_trades', 0)}")
        print(f"Win Rate: {stats.get('win_rate_display', stats.get('win_rate'))}")
        print(f"Net Expectancy: {stats.get('expectancy_str', 'N/A')}")
        if stats.get("has_negative_expectancy"):
            print("🚨 STRATEGY HAS NEGATIVE EXPECTANCY. DO NOT DEPLOY LIVE CAPITAL.")
            print("STRATEGY HAS NEGATIVE EXPECTANCY. DO NOT TRADE.")

        report_path = self.generate_report(stats, missed_trades, bad_trades, as_of_date=now_dt)
        print(f"Audit report generated: {report_path}")
        return report_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OpenClaw Weekend Auditor (Phase 4)")
    parser.add_argument("--force", action="store_true", help="Force run even if not Saturday")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    if not args.force and now_utc.weekday() != 5:
        print("Weekend Auditor only runs on Saturdays")
        sys.exit(0)

    auditor = WeekendAuditor()
    report_file = auditor.run_audit(force=True)
    if report_file:
        print(f"Weekend Audit complete. Report written to: {report_file}")
