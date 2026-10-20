"""
OpenClaw Quant Lab - Phase 1 & 2 Paper Trading Harness
Trade Plan Scorer (src/scorer.py)

Responsibilities:
1. Ensure the 'trade_plans' table exists in Supabase.
2. Read the latest signal from the Supabase 'signals' table.
3. Enforce Multiplicative Confluence & Circuit Breaker Gate:
   - Check Macro validity first (is_macro_valid, weekend ban, telemetry offline).
   - If macro is invalid (-999 or -1), immediately abort and return None.
   - Calculate multi-factor confluence score (Technicals, Macro, Funding/OI).
4. Calculate dynamic ATR risk parameters (SL: 1.5 ATR, TP: 3.0 ATR).
5. Save generated TradePlan into Supabase 'trade_plans' table with status 'PENDING'.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
from src.config import config, MACRO_TIMEFRAME
from src.db_manager import SupabaseManager, retry_db_call
from src.validators import TradePlan, ValidationError
from src.paper_executor import calculate_position_size

logger = logging.getLogger("Scorer")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [Scorer] %(message)s"))
    logger.addHandler(ch)



def filter_short_by_ema(
    plan: Optional[Dict[str, Any]],
    symbol: Optional[str] = None,
    data_fetcher: Optional[Any] = None
) -> Optional[Dict[str, Any]]:
    """
    Trend Filter:
    - Do NOT use requests. Uses existing DataFetcher to fetch 4H data.
    - If the plan is SHORT and the current price is ABOVE the 4H EMA 200, reject the plan (returns None).
    - Otherwise, returns the plan.
    """
    if plan is None:
        return None

    direction = str(plan.get("direction") or (plan.get("metadata") or {}).get("direction", "")).upper()
    if direction != "SHORT":
        return plan

    sym = symbol or plan.get("symbol")
    if not sym:
        logger.warning("filter_short_by_ema: No symbol provided.")
        return plan

    entry_price = float(
        plan.get("entry_price")
        or plan.get("close")
        or (plan.get("metadata") or {}).get("close")
        or (plan.get("plan_metadata") or {}).get("entry_price")
        or 0.0
    )
    if entry_price <= 0:
        logger.warning(f"filter_short_by_ema: Invalid entry price ({entry_price}) for {sym}")
        return plan

    fetcher = data_fetcher
    if fetcher is None:
        from src.data_fetcher import DataFetcher
        fetcher = DataFetcher()

    try:
        from src.config import config
        macro_tf = getattr(config, "MACRO_TIMEFRAME", "4h")
        df_4h = fetcher.fetch_ohlcv(sym, timeframe=macro_tf, limit=250, use_cache=True)
        if df_4h is not None and not df_4h.empty and len(df_4h) >= 15:
            ema_200 = float(df_4h["close"].ewm(span=200, adjust=False).mean().iloc[-1])
            if entry_price > ema_200:
                logger.warning(
                    f"[PLAN ABORTED] Trend Filter: Current price {entry_price:.2f} is ABOVE 200 EMA ({ema_200:.2f}) on {macro_tf}. "
                    f"Rejecting SHORT setup (do not short into a bull market)."
                )
                return None
            else:
                logger.info(
                    f"[TREND FILTER PASSED] Short setup validated: entry {entry_price:.2f} <= 200 EMA {ema_200:.2f} on {macro_tf}"
                )
    except Exception as e:
        logger.warning(f"Error checking 200 EMA trend filter for {sym}: {e}")

    return plan


MAJOR_CRYPTO_ASSETS = {"BTC", "ETH", "SOL"}


def extract_base_symbol(symbol: str) -> str:
    s = (symbol or "").upper().replace(":USDT", "").replace("/USDT", "").replace("USDT", "")
    if "/" in s:
        s = s.split("/")[0]
    return s.strip()


def is_highly_correlated(sym1: str, dir1: str, sym2: str, dir2: str) -> bool:
    """
    Checks if two trade setups are highly correlated:
    - Same asset in any direction is either stacked or conflicting.
    - Same direction across major crypto assets (e.g., LONG BTC and LONG ETH, or SHORT BTC and SHORT ETH).
    """
    base1 = extract_base_symbol(sym1)
    base2 = extract_base_symbol(sym2)
    d1 = dir1.upper()
    d2 = dir2.upper()

    if not base1 or not base2:
        return False

    if base1 == base2:
        return True

    if base1 in MAJOR_CRYPTO_ASSETS and base2 in MAJOR_CRYPTO_ASSETS:
        if d1 == d2:
            return True

    return False


class TradeScorer:
    """
    Deterministic scoring and trade planning engine.
    Converts raw liquidation signals into structured, risk-defined TradePlan objects
    with non-linear macro & derivative confluence gating.
    """

    TRADE_PLANS_DDL = """-- OpenClaw Quant Lab: Trade Plans Schema
CREATE TABLE IF NOT EXISTS trade_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id UUID,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    stop_loss DOUBLE PRECISION NOT NULL,
    take_profit DOUBLE PRECISION NOT NULL,
    atr DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, TRIGGERED, PARTIALLY_FILLED, FILLED, CLOSED, TIMEOUT, MISSED, REJECTED, REJECTED_SCHEMA
    plan_metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trade_plans_status ON trade_plans (status);
CREATE INDEX IF NOT EXISTS idx_trade_plans_symbol ON trade_plans (symbol, created_at DESC);
"""

    def __init__(
        self,
        db_manager: Optional[SupabaseManager] = None,
        macro_fetcher: Optional[Any] = None,
        data_fetcher: Optional[Any] = None,
    ):
        self.db: Optional[SupabaseManager] = db_manager
        self.macro_fetcher = macro_fetcher
        self.data_fetcher = data_fetcher

    def _ensure_data_fetcher(self) -> Any:
        if self.data_fetcher is not None:
            return self.data_fetcher
        try:
            from src.data_fetcher import DataFetcher
            self.data_fetcher = DataFetcher()
            return self.data_fetcher
        except Exception as e:
            logger.warning(f"DataFetcher initialization failed: {e}")
            return None

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

    def _ensure_macro_fetcher(self) -> Any:
        """Lazy-initialize or return MacroFetcher."""
        if self.macro_fetcher is not None:
            return self.macro_fetcher
        try:
            from src.macro_fetcher import MacroFetcher
            self.macro_fetcher = MacroFetcher(db_manager=self.db)
            return self.macro_fetcher
        except Exception as e:
            logger.error(f"Failed to initialize MacroFetcher: {e}")
            raise

    def init_trade_plans_table(self) -> str:
        """Attempts to create the trade_plans table via Supabase RPC or provides DDL."""
        db = self._ensure_db()
        if not db:
            return self.TRADE_PLANS_DDL

        try:
            db.client.rpc("exec_sql", {"sql": self.TRADE_PLANS_DDL}).execute()
            logger.info("Successfully verified/created trade_plans table via Supabase RPC.")
        except Exception as e:
            logger.info("Direct SQL execution via RPC not enabled. Ensure table is created in Supabase SQL editor:")
            logger.info(self.TRADE_PLANS_DDL)

        return self.TRADE_PLANS_DDL

    def get_latest_signal(self) -> Optional[Dict[str, Any]]:
        """
        Retrieves the most recent trade signal from Supabase signals table.
        Excludes 'PAPER_FILL' records so it only processes real market triggers.
        """
        db = self._ensure_db()
        if not db:
            return None

        try:
            response = (
                db.client.table("signals")
                .select("*")
                .neq("signal_type", "PAPER_FILL")
                .order("event_time", desc=True)
                .limit(1)
                .execute()
            )
            if response.data and len(response.data) > 0:
                return response.data[0]
            logger.info("No trading signals found in Supabase signals table.")
            return None
        except Exception as e:
            logger.error(f"Error fetching latest signal: {e}")
            return None

    def calculate_confluence_score(
        self,
        tech_score: float,
        macro_result: Dict[str, Any],
        derivative_result: Dict[str, Any],
        symbol: Optional[str] = None,
        macro_fetcher: Optional[Any] = None,
    ) -> Tuple[bool, float, str]:
        """
        Calculates actionable trade confluence.
        Enforces binary pre-condition GATES before performing factor math.
        
        Returns:
            (is_approved: bool, final_confluence_score: float, rejection_reason: str)
        """
        # ==========================================================
        # GATE 1: Circuit Breaker & Weekend Invariant Check
        # ==========================================================
        macro_score = macro_result.get("score", -1)
        is_macro_valid = macro_result.get("is_valid", False)

        if not is_macro_valid or macro_score < 0:
            reason = macro_result.get("reason", "Macro gate rejected.")
            logger.warning(f"[TRADE REJECTED] Invariant Gate Block: {reason}")
            # Immediate short-circuit: Math is bypassed completely
            return False, 0.0, f"Macro Invariant Failed: {reason}"

        # ==========================================================
        # GATE 2: Relative Strength Rotation Gate (All Coin Theory)
        # ==========================================================
        fetcher = macro_fetcher or getattr(self, "macro_fetcher", None)
        rotation_regime = None
        if fetcher is not None and hasattr(fetcher, "get_eth_btc_trend") and callable(getattr(fetcher, "get_eth_btc_trend")):
            try:
                rotation_regime = fetcher.get_eth_btc_trend()
            except Exception as e:
                logger.warning(f"Error checking ETH/BTC trend: {e}")
                rotation_regime = None

        if rotation_regime is None and isinstance(macro_result, dict):
            rotation_regime = macro_result.get("rotation_regime") or macro_result.get("metadata", {}).get("eth_btc_trend")

        if rotation_regime is None:
            rotation_regime = "ALTSEASON"

        self._last_rotation_regime = rotation_regime
        if isinstance(macro_result, dict):
            macro_result["rotation_regime"] = rotation_regime

        target_sym = symbol or derivative_result.get("symbol") or (macro_result.get("symbol") if isinstance(macro_result, dict) else None)
        if target_sym is not None and rotation_regime == "BTC_DOMINANCE":
            # Check if symbol is BTC (e.g. BTC/USDT, BTC/USDT:USDT, BTCUSDT)
            is_btc = target_sym.startswith("BTC/") or target_sym.startswith("BTCUSDT") or target_sym == "BTC"
            if not is_btc:
                reason = f"Rotation Filter: {target_sym} rejected during BTC_DOMINANCE (All Coin Theory)"
                logger.warning(f"[TRADE REJECTED] {reason}")
                return False, 0.0, reason

        # ==========================================================
        # GATE 3: Minimum Technical Threshold Gate
        # ==========================================================
        if tech_score < 60.0:
            return False, tech_score, f"Technical setup score ({tech_score}) below minimum execution floor (60.0)"

        # ==========================================================
        # FACTOR CONFLUENCE CALCULATION (Only executed if gates pass)
        # ==========================================================
        # Normalization: tech_score (0-100), macro_score (0-10 -> 0-100)
        norm_macro = macro_score * 10.0  # Scales 0, 5, 10 to 0, 50, 100

        # Funding rate penalty/boost:
        funding = derivative_result.get("funding_rate", 0.0)
        oi_change = derivative_result.get("oi_change_24h_pct", 0.0)

        # Baseline weighted average across valid factors
        weights = {"tech": 0.60, "macro": 0.25, "oi": 0.15}
        
        # Derivative score contribution: expanding OI with favorable funding
        deriv_score = 50.0  # Neutral base
        if oi_change > 2.0:
            deriv_score += 20.0
        elif oi_change < -2.0:
            deriv_score -= 20.0

        confluence_score = (
            (weights["tech"] * tech_score) +
            (weights["macro"] * norm_macro) +
            (weights["oi"] * deriv_score)
        )

        # Final score filter for TradePlan generation
        if confluence_score >= 70.0:
            return True, round(confluence_score, 2), "Confluence requirements satisfied"
        else:
            return False, round(confluence_score, 2), f"Confluence score ({confluence_score:.1f}) below threshold (70.0)"

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """Queries Supabase trade_plans table for all open trades (PENDING, TRIGGERED, FILLED, PARTIALLY_FILLED)."""
        db = self._ensure_db()
        if not db:
            return []
        try:
            def _fetch():
                tbl = db.client.table("trade_plans").select("*")
                if hasattr(tbl, "in_"):
                    return tbl.in_("status", ["PENDING", "TRIGGERED", "FILLED", "PARTIALLY_FILLED"]).execute()
                return tbl.execute()

            response = retry_db_call(_fetch)
            data = response.data or []
            return [t for t in data if t.get("status") in ("PENDING", "TRIGGERED", "FILLED", "PARTIALLY_FILLED")]
        except Exception as e:
            logger.error(f"Error fetching open trades: {e}")
            return []

    def check_portfolio_exposure(
        self,
        symbol: str,
        direction: str,
        open_trades: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, str]:
        """
        Enforces Portfolio Exposure Cap:
        - Max 2 open trades allowed across portfolio.
        - Rejects new trade if highly correlated with an existing open trade (e.g., LONG BTC and LONG ETH).
        """
        if open_trades is None:
            open_trades = self.get_open_trades()

        # Rule 1: Max 2 open trades
        if len(open_trades) >= 2:
            reason = f"Portfolio exposure cap reached: {len(open_trades)} active trades open (max 2 allowed)"
            logger.warning(f"[EXPOSURE GATE REJECTED] {reason}")
            return False, reason

        # Rule 2: High correlation check
        for trade in open_trades:
            trade_sym = trade.get("symbol", "")
            trade_dir = trade.get("direction", "")
            if is_highly_correlated(symbol, direction, trade_sym, trade_dir):
                reason = (
                    f"High correlation conflict: New {direction} {symbol} correlates with "
                    f"active {trade_dir} {trade_sym}"
                )
                logger.warning(f"[CORRELATION GATE REJECTED] {reason}")
                return False, reason

        return True, "Portfolio exposure and correlation checks passed"

    def get_closed_trades_today(self) -> List[Dict[str, Any]]:
        """Queries Supabase for trades closed today (UTC calendar day)."""
        db = self._ensure_db()
        if not db:
            return []
        try:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()

            def _fetch():
                tbl = db.client.table("trade_plans").select("*").eq("status", "CLOSED")
                if hasattr(tbl, "gte"):
                    return tbl.gte("updated_at", today_start).execute()
                return tbl.execute()

            response = retry_db_call(_fetch)
            data = response.data or []
            today_date = datetime.now(timezone.utc).date()
            closed_today = []
            for t in data:
                if t.get("status") != "CLOSED":
                    continue
                meta = t.get("plan_metadata") or {}
                ts_str = meta.get("closed_at") or t.get("updated_at") or t.get("created_at")
                if ts_str:
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if dt.date() == today_date:
                            closed_today.append(t)
                            continue
                    except Exception:
                        pass
                closed_today.append(t)
            return closed_today
        except Exception as e:
            logger.error(f"Error fetching closed trades for today: {e}")
            return []

    def check_realized_drawdown_circuit_breaker(
        self,
        account_balance: Optional[float] = None,
        closed_trades: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, str]:
        """
        Risk Circuit Breaker:
        Calculates daily drawdown based on REALIZED equity only.
        Floating PnL on open trades (PENDING, TRIGGERED, FILLED) is strictly EXCLUDED.
        If cumulative realized loss today exceeds daily floor (2% of account_balance),
        triggers db.set_halt() and returns (False, reason).
        """
        bal = account_balance or float(getattr(config, "account_balance", 100000.0))
        floor_limit_pct = float(getattr(config, "daily_floor_limit", 0.02))
        max_allowed_loss = bal * floor_limit_pct

        if closed_trades is None:
            closed_trades = self.get_closed_trades_today()

        total_realized_pnl = 0.0
        for trade in closed_trades:
            meta = trade.get("plan_metadata") or {}
            if "realized_pnl" in meta:
                total_realized_pnl += float(meta["realized_pnl"])
            else:
                entry_p = float(trade.get("entry_price") or meta.get("fill_price") or 0.0)
                exit_p = float(meta.get("exit_price") or trade.get("exit_price") or 0.0)
                pos_u = float(trade.get("position_size") or meta.get("position_size") or meta.get("position_units") or 0.0)
                direction = str(trade.get("direction", "LONG")).upper()
                if entry_p > 0 and exit_p > 0 and pos_u > 0:
                    if direction == "LONG":
                        pnl = (exit_p - entry_p) * pos_u
                    else:
                        pnl = (entry_p - exit_p) * pos_u
                    total_realized_pnl += pnl

        if total_realized_pnl < 0 and abs(total_realized_pnl) >= max_allowed_loss:
            loss_pct = abs(total_realized_pnl) / bal
            reason = (
                f"Risk Circuit Breaker Tripped: Realized daily drawdown of "
                f"${abs(total_realized_pnl):.2f} ({loss_pct * 100:.2f}%) exceeds "
                f"the {floor_limit_pct * 100:.1f}% daily floor limit (${max_allowed_loss:.2f})"
            )
            logger.critical(f"[CIRCUIT BREAKER] {reason}")
            db = self._ensure_db()
            if db:
                try:
                    db.set_halt(reason=reason)
                except Exception as e:
                    logger.error(f"Failed to set db halt: {e}")
            return False, reason

        return True, f"Realized daily PnL is ${total_realized_pnl:.2f} (limit: -${max_allowed_loss:.2f})"

    def check_intra_hour_circuit_breaker(
        self,
        starting_equity: Optional[float] = None,
        current_equity: Optional[float] = None,
        time_elapsed_minutes: float = 60.0,
    ) -> Tuple[bool, str]:
        """
        Hard Liquidation Circuit Breaker:
        If equity drops >= 5% in <= 60 minutes, the system immediately flattens all positions,
        halts trading for 24 hours, and sets system_state = 'CIRCUIT_BREAKER'.
        """
        bal = starting_equity or float(getattr(config, "account_balance", 100000.0))
        curr = current_equity if current_equity is not None else bal
        drop_pct = (bal - curr) / bal if bal > 0 else 0.0

        if drop_pct >= 0.05 and time_elapsed_minutes <= 60.0:
            reason = (
                f"Intra-Hour Hard Liquidation: Equity dropped {drop_pct * 100:.2f}% (>= 5.0%) "
                f"in {time_elapsed_minutes:.1f} minutes. All positions flattened, 24h halt engaged."
            )
            logger.critical(f"[CIRCUIT BREAKER HARD LIQUIDATION] {reason}")
            db = self._ensure_db()
            if db:
                if hasattr(db, "trigger_intra_hour_circuit_breaker"):
                    db.trigger_intra_hour_circuit_breaker(reason=reason)
                elif hasattr(db, "set_circuit_breaker"):
                    db.set_circuit_breaker(reason=reason)
            return False, reason
        return True, "Intra-hour equity within safety limits"

    def calculate_trade_plan(
        self,
        signal: Dict[str, Any],
        macro_fetcher: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Calculates TradePlan metrics from signal metadata and macro/derivative confluence.
        If macro gate fails (weekend ban or offline telemetry), returns None immediately.
        """
        # Gate 0: System State Kill Switch / Circuit Breaker Check
        db = self._ensure_db()
        if db and db.check_halt():
            logger.warning("[PLAN ABORTED] System is HALTED / CIRCUIT_BREAKER / WEEKEND_FLATTENED.")
            return None

        # Gate 0A: Risk Circuit Breaker (Realized Drawdown Floor)
        passed_circuit, cb_reason = self.check_realized_drawdown_circuit_breaker()
        if not passed_circuit:
            logger.warning(f"[PLAN ABORTED] Circuit Breaker: {cb_reason}")
            return None

        # Gate 0B: Portfolio Exposure Cap & Correlation Gate
        symbol = signal.get("symbol", "BTC/USDT:USDT")
        meta = signal.get("metadata") or {}
        direction = str(meta.get("direction", "SHORT")).upper()
        passed_exposure, exp_reason = self.check_portfolio_exposure(symbol, direction)
        if not passed_exposure:
            logger.warning(f"[PLAN ABORTED] Exposure Cap: {exp_reason}")
            return None

        tech_score = float(signal.get("score", 0.0))

        fetcher = macro_fetcher or self._ensure_macro_fetcher()

        # Fetch Macro & Derivative Context
        macro_result = fetcher.get_macro_risk()
        deriv_result = fetcher.get_funding_and_oi(symbol)

        # Apply Multiplicative Confluence Gate
        approved, final_score, reason = self.calculate_confluence_score(
            tech_score=tech_score,
            macro_result=macro_result,
            derivative_result=deriv_result,
            symbol=symbol,
            macro_fetcher=fetcher,
        )

        if not approved:
            logger.info(f"[PLAN ABORTED] Symbol: {symbol} | Reason: {reason}")
            # Supabase insertion is never called; prevents database clutter
            return None

        # Build execution order parameters only for approved setups
        meta = signal.get("metadata") or {}
        signal_id = signal.get("id")

        entry_price = float(meta.get("close", 0.0))
        atr = float(meta.get("atr", 0.0))
        direction = str(meta.get("direction", "SHORT")).upper()

        if entry_price <= 0 or atr <= 0:
            logger.error(f"Invalid entry_price ({entry_price}) or ATR ({atr}) in signal metadata: {meta}")
            return None

        # Task 3: Trend Filter (200-period EMA on MACRO_TIMEFRAME (4H) chart for SHORT setups)
        if direction == "SHORT":
            data_fetcher = self._ensure_data_fetcher()
            if data_fetcher is not None:
                try:
                    macro_tf = getattr(config, "MACRO_TIMEFRAME", "4h")
                    df_macro = data_fetcher.fetch_ohlcv(symbol, timeframe=macro_tf, limit=250, use_cache=True)
                    if df_macro is not None and not df_macro.empty:
                        ema_200 = float(df_macro["close"].ewm(span=200, adjust=False).mean().iloc[-1])
                        if entry_price > ema_200:
                            logger.warning(
                                f"[PLAN ABORTED] Trend Filter: Current price {entry_price:.2f} is ABOVE 200 EMA ({ema_200:.2f}) on {macro_tf}. "
                                f"Rejecting SHORT setup (do not short into a bull market)."
                            )
                            return None
                        else:
                            logger.info(
                                f"[TREND FILTER PASSED] Short setup validated: entry {entry_price:.2f} <= 200 EMA {ema_200:.2f} on {macro_tf}"
                            )
                except Exception as e:
                    logger.warning(f"Error checking 200 EMA trend filter for {symbol}: {e}")

        sl_distance = atr * 1.5
        tp_distance = atr * 3.0

        if direction == "SHORT":
            stop_loss = round(entry_price + sl_distance, 4)
            take_profit = round(entry_price - tp_distance, 4)
        else:  # LONG
            stop_loss = round(entry_price - sl_distance, 4)
            take_profit = round(entry_price + tp_distance, 4)

        account_balance = float(getattr(config, "account_balance", 100000.0))
        pos_size = calculate_position_size(
            account_balance=account_balance,
            entry_price=entry_price,
            stop_loss=stop_loss,
            atr_value=atr,
            symbol=symbol,
        )
        position_size = pos_size.units

        now_iso = datetime.now(timezone.utc).isoformat()
        plan = {
            "signal_id": signal_id,
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "position_size": position_size,
            "atr": atr,
            "status": "PENDING",
            "plan_metadata": {
                "sl_distance": sl_distance,
                "tp_distance": tp_distance,
                "reward_risk_ratio": 2.0,
                "order_type": "LIMIT_POST_ONLY",
                "source_signal_score": tech_score,
                "confluence_score": final_score,
                "confluence_reason": reason,
                "macro_score": macro_result.get("score"),
                "rotation_regime": getattr(self, "_last_rotation_regime", "ALTSEASON"),
                "funding_rate": deriv_result.get("funding_rate"),
                "oi_change_24h_pct": deriv_result.get("oi_change_24h_pct"),
                "raw_quantity": pos_size.raw_quantity,
                "fee_adjusted_quantity": pos_size.fee_adjusted_quantity,
                "step_size": pos_size.step_size,
                "contract_multiplier": pos_size.contract_multiplier,
                "risk_amount": pos_size.risk_amount,
                "source_event_time": signal.get("event_time"),
                "calculated_at": now_iso,
            },
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        return filter_short_by_ema(plan, symbol, data_fetcher=self._ensure_data_fetcher())

    def save_trade_plan(self, plan: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Inserts trade plan into Supabase 'trade_plans' table.
        Prevents duplicate plans for the same signal_id.
        """
        if not plan:
            return None

        # Task 2: Pydantic Schema Gate (TradePlan validation)
        # Extract plan_dict for TradePlan validation, preserving any unauthorized extra fields to enforce extra = "forbid"
        plan_dict = {
            "symbol": plan.get("symbol"),
            "direction": plan.get("direction"),
            "entry_price": plan.get("entry_price"),
            "stop_loss": plan.get("stop_loss"),
            "take_profit": plan.get("take_profit"),
            "position_size": float(plan.get("position_size") or 0.0),
            "atr": float(plan.get("atr") or (plan.get("plan_metadata") or {}).get("atr") or 0.0),
        }
        for k, v in plan.items():
            if k not in ("id", "signal_id", "status", "plan_metadata", "created_at", "updated_at", "triggered_at"):
                if k not in plan_dict:
                    plan_dict[k] = v

        try:
            TradePlan(**plan_dict)
            logger.info(f"[SCHEMA GATE PASSED] Plan for {plan_dict.get('symbol')} validated successfully.")
        except ValidationError as e:
            logger.error(f"[SCHEMA GATE REJECTED] Malformed trade plan failed Pydantic validation: {e}")
            return None

        db = self._ensure_db()
        if not db:
            logger.info(f"[Dry Run] Generated TradePlan: {plan}")
            return plan

        signal_id = plan.get("signal_id")
        if signal_id:
            try:
                existing = (
                    db.client.table("trade_plans")
                    .select("id, status")
                    .eq("signal_id", signal_id)
                    .execute()
                )
                if existing.data and len(existing.data) > 0:
                    logger.info(f"TradePlan already exists for signal_id {signal_id}. Skipping duplicate.")
                    return existing.data[0]
            except Exception as e:
                logger.warning(f"Error checking existing trade plan: {e}")

        try:
            response = retry_db_call(lambda: db.client.table("trade_plans").insert(plan).execute())
            if response.data and len(response.data) > 0:
                saved = response.data[0]
                logger.info(
                    f"[PLAN CREATED] {plan['symbol']} {plan['direction']} @ {plan['entry_price']} | "
                    f"SL: {plan['stop_loss']} | TP: {plan['take_profit']} | ID: {saved.get('id')}"
                )
                return saved
            return plan
        except Exception as e:
            logger.error(f"Failed to insert trade plan into Supabase: {e}")
            return None

    def process_latest(self, macro_fetcher: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        """Orchestrates fetching latest signal, planning, and saving."""
        signal = self.get_latest_signal()
        if not signal:
            logger.info("No signal available to score.")
            return None

        plan = self.calculate_trade_plan(signal, macro_fetcher=macro_fetcher)
        if not plan:
            return None

        return self.save_trade_plan(plan)


if __name__ == "__main__":
    scorer = TradeScorer()
    scorer.init_trade_plans_table()
    plan = scorer.process_latest()
    print("Scorer execution complete.")

TradeScorer.filter_short_by_ema = staticmethod(filter_short_by_ema)
TradeScorer.is_highly_correlated = staticmethod(is_highly_correlated)
TradeScorer.extract_base_symbol = staticmethod(extract_base_symbol)
