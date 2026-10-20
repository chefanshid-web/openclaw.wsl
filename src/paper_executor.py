"""
OpenClaw Quant Lab - Phase 1: Weeks 5-7 Paper Trading Harness
Paper Order Execution Simulator (src/paper_executor.py)

Responsibilities:
1. Polls the Supabase 'trade_plans' table for plans with status 'PENDING'.
2. Simulates placing a Post-Only Maker limit order at the planned entry price.
3. Tracks order lifetime (up to 15-minute timeout window).
4. Inspects live market prices:
   - SHORT: Fills if Market High >= Entry Price
   - LONG:  Fills if Market Low <= Entry Price
5. If touched:
   - Updates plan status to 'FILLED'
   - Logs simulated fill to Supabase 'signals' table with signal_type 'PAPER_FILL'
6. If 15 minutes elapse without fill:
   - Updates plan status to 'TIMEOUT'
7. Checks kill switch: Halts immediately if db_manager.check_halt() is True.
"""

import time
import random
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from decimal import Decimal

from src.config import config, RISK_PER_TRADE_PERCENT, ATR_MULTIPLIER_FOR_STOP, ATR_MULTIPLIER_FOR_TRAIL
from src.data_fetcher import DataFetcher
from src.db_manager import SupabaseManager, retry_db_call
from src.validators import TradePlan, ValidationError

logger = logging.getLogger("PaperExecutor")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [PaperExec] %(message)s"))
    logger.addHandler(ch)



_MARKET_LIMITS_CACHE: Dict[str, Dict[str, Any]] = {}


def get_market_limits(
    symbol: Optional[str] = None,
    exchange: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Fetches market limits (min_qty, max_qty, step_size, contract_multiplier) via CCXT for a symbol.
    Caches limits in-memory to prevent repeated network overhead.
    """
    sym = (symbol or "BTC/USDT:USDT").strip()
    cache_key = f"{id(exchange) if exchange else 'default'}:{sym}"
    if cache_key in _MARKET_LIMITS_CACHE:
        return _MARKET_LIMITS_CACHE[cache_key]

    limits_data = {
        "min_qty": None,
        "max_qty": None,
        "step_size": None,
        "contract_multiplier": 1.0,
    }

    market = None
    if exchange is not None:
        try:
            if hasattr(exchange, "market"):
                try:
                    market = exchange.market(sym)
                except Exception:
                    if hasattr(exchange, "load_markets"):
                        exchange.load_markets()
                    market = exchange.market(sym)
            elif hasattr(exchange, "markets") and isinstance(exchange.markets, dict):
                market = exchange.markets.get(sym)
        except Exception as e:
            logger.debug(f"Could not fetch CCXT market for {sym} from exchange: {e}")

    if market is None and exchange is None:
        try:
            import ccxt
            ex = ccxt.binance({"options": {"defaultType": "future"}})
            try:
                ex.load_markets()
                market = ex.market(sym)
            except Exception as e:
                logger.debug(f"Default CCXT binance futures load failed: {e}")
        except Exception as e:
            logger.debug(f"CCXT initialization error: {e}")

    if isinstance(market, dict):
        amt_limits = market.get("limits", {}).get("amount", {}) if isinstance(market.get("limits"), dict) else {}
        limits_data["min_qty"] = amt_limits.get("min")
        limits_data["max_qty"] = amt_limits.get("max")

        precision = market.get("precision", {})
        prec_amt = precision.get("amount") if isinstance(precision, dict) else None
        if prec_amt is not None and isinstance(prec_amt, (int, float)):
            if 0 < prec_amt < 1:
                limits_data["step_size"] = float(prec_amt)
            elif prec_amt >= 1 and isinstance(prec_amt, int):
                limits_data["step_size"] = float(10 ** (-prec_amt))

        if limits_data["step_size"] is None and limits_data["min_qty"] is not None:
            limits_data["step_size"] = float(limits_data["min_qty"])

        c_size = market.get("contractSize")
        if c_size is not None:
            try:
                limits_data["contract_multiplier"] = float(c_size)
            except (ValueError, TypeError):
                limits_data["contract_multiplier"] = 1.0

    # Fallback defaults for standard crypto assets if unresolved
    base = sym.upper().replace(":USDT", "").replace("/USDT", "").replace("USDT", "")
    if "/" in base:
        base = base.split("/")[0]

    if limits_data["step_size"] is None:
        if base in ("BTC", "ETH"):
            limits_data["step_size"] = 0.001
        elif base == "SOL":
            limits_data["step_size"] = 0.01
        else:
            limits_data["step_size"] = 0.001

    if limits_data["min_qty"] is None:
        limits_data["min_qty"] = limits_data["step_size"]

    if limits_data["max_qty"] is None:
        if base == "BTC":
            limits_data["max_qty"] = 1000.0
        elif base == "ETH":
            limits_data["max_qty"] = 10000.0
        elif base == "SOL":
            limits_data["max_qty"] = 1000000.0
        else:
            limits_data["max_qty"] = 1000000.0

    if limits_data["contract_multiplier"] is None or limits_data["contract_multiplier"] <= 0:
        limits_data["contract_multiplier"] = 1.0

    _MARKET_LIMITS_CACHE[cache_key] = limits_data
    return limits_data


class PositionSizeResult(tuple):
    """Container for position sizing result with tuple unpacking and attribute/dict access."""
    def __new__(
        cls,
        units: float,
        total_value: float,
        risk_amount: float = 0.0,
        stop_distance: float = 0.0,
        raw_quantity: float = 0.0,
        fee_adjusted_quantity: float = 0.0,
        min_qty: Optional[float] = None,
        max_qty: Optional[float] = None,
        step_size: Optional[float] = None,
        contract_multiplier: float = 1.0,
    ):
        return super().__new__(cls, (units, total_value))

    def __init__(
        self,
        units: float,
        total_value: float,
        risk_amount: float = 0.0,
        stop_distance: float = 0.0,
        raw_quantity: float = 0.0,
        fee_adjusted_quantity: float = 0.0,
        min_qty: Optional[float] = None,
        max_qty: Optional[float] = None,
        step_size: Optional[float] = None,
        contract_multiplier: float = 1.0,
    ):
        self.units = units
        self.total_value = total_value
        self.risk_amount = risk_amount
        self.stop_distance = stop_distance
        self.raw_quantity = raw_quantity
        self.fee_adjusted_quantity = fee_adjusted_quantity
        self.min_qty = min_qty
        self.max_qty = max_qty
        self.step_size = step_size
        self.contract_multiplier = contract_multiplier

    def __getitem__(self, item):
        if isinstance(item, str):
            return getattr(self, item)
        return super().__getitem__(item)

    def get(self, key, default=None):
        return getattr(self, key, default)


def calculate_position_size(
    account_balance: float,
    entry_price: float,
    stop_loss: Optional[float] = None,
    atr_value: Optional[float] = None,
    symbol: Optional[str] = None,
    exchange: Optional[Any] = None,
    contract_multiplier: Optional[float] = None,
) -> PositionSizeResult:
    """
    Quant-Audited Dynamic Position Sizing Formula:
    1. Risk_Budget = Account_Balance * 0.01 (1% risk)
    2. Stop_Distance = abs(Entry_Price - Stop_Loss) (or atr_value * ATR_MULTIPLIER_FOR_STOP if stop_loss omitted)
    3. Raw_Quantity = Risk_Budget / Stop_Distance
    4. Fee_Adjusted_Quantity = Raw_Quantity * 0.98 (deduct 2% for fees/slippage buffer)
    5. Fetch exchange market limits (min_qty, max_qty, step_size, contract_multiplier) via CCXT
    6. Round quantity down to nearest step size, cap at max_qty, and enforce min_qty.
    """
    risk_pct = getattr(config, "RISK_PER_TRADE_PERCENT", 0.01)
    risk_budget = account_balance * risk_pct

    # Determine stop distance
    stop_distance = None
    if stop_loss is not None:
        if atr_value is None and entry_price > 10.0 and stop_loss < (entry_price * 0.4):
            stop_mult = getattr(config, "ATR_MULTIPLIER_FOR_STOP", 1.5)
            stop_distance = stop_loss * stop_mult
        else:
            stop_distance = abs(entry_price - stop_loss)
    elif atr_value is not None and atr_value > 0:
        stop_mult = getattr(config, "ATR_MULTIPLIER_FOR_STOP", 1.5)
        stop_distance = atr_value * stop_mult

    if stop_distance is None or stop_distance <= 0:
        stop_distance = entry_price * 0.015  # Fallback 1.5%

    raw_quantity = risk_budget / stop_distance
    fee_adjusted_quantity = raw_quantity * 0.98

    # CCXT Market Limits
    limits = get_market_limits(symbol=symbol, exchange=exchange)
    min_qty = limits.get("min_qty")
    max_qty = limits.get("max_qty")
    step_size = limits.get("step_size", 0.001)

    c_multiplier = (
        contract_multiplier
        if (contract_multiplier is not None and contract_multiplier > 0)
        else limits.get("contract_multiplier", 1.0)
    )
    target_contracts = fee_adjusted_quantity / c_multiplier

    # Round quantity down to nearest step size
    try:
        from decimal import Decimal
        d_qty = Decimal(str(target_contracts))
        d_step = Decimal(str(step_size))
        steps = d_qty // d_step
        final_units = float(steps * d_step)
    except Exception:
        steps = int(target_contracts / step_size)
        final_units = float(steps * step_size)

    if max_qty is not None and final_units > max_qty:
        try:
            d_max = Decimal(str(max_qty))
            d_step = Decimal(str(step_size))
            final_units = float((d_max // d_step) * d_step)
        except Exception:
            final_units = float(max_qty)

    if min_qty is not None and final_units < min_qty:
        logger.warning(
            f"[POSITION SIZING] Quantity {final_units} is below exchange minimum lot size {min_qty} for {symbol}. Sizing to 0."
        )
        final_units = 0.0

    total_value = final_units * entry_price * c_multiplier

    return PositionSizeResult(
        units=final_units,
        total_value=total_value,
        risk_amount=risk_budget,
        stop_distance=stop_distance,
        raw_quantity=raw_quantity,
        fee_adjusted_quantity=fee_adjusted_quantity,
        min_qty=min_qty,
        max_qty=max_qty,
        step_size=step_size,
        contract_multiplier=c_multiplier,
    )


def is_friday_flatten_time(dt: Optional[datetime] = None) -> bool:
    """
    Checks if current UTC time is at or past Friday 15:30 UTC.
    The Weekend Gate locks at 16:00 UTC; this hard rule triggers 30 minutes prior.
    Returns True for Friday >= 15:30 UTC and throughout the weekend (Saturday/Sunday).
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # Friday is weekday 4
    if dt.weekday() == 4:
        return (dt.hour > 15) or (dt.hour == 15 and dt.minute >= 30)
    elif dt.weekday() in (5, 6):
        return True
    return False


class PaperExecutor:
    """
    Simulates realistic Post-Only Maker order execution against live market price action.
    Supported states: PENDING, TRIGGERED, PARTIALLY_FILLED, FILLED, CLOSED, TIMEOUT, MISSED, REJECTED, REJECTED_SCHEMA, CANCELLED.
    """
    TIMEOUT_SECONDS = 15 * 60  # 15 minutes order timeout
    ADVERSE_SELECTION_RATE = 0.30  # 30% chance price crosses entry but immediately reverses

    VALID_PLAN_STATUSES = {
        "PENDING",
        "TRIGGERED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CLOSED",
        "TIMEOUT",
        "MISSED",
        "REJECTED",
        "REJECTED_SCHEMA",
        "CANCELLED",
    }

    def __init__(self, db_manager: Optional[SupabaseManager] = None):
        self.fetcher = DataFetcher()
        self.db: Optional[SupabaseManager] = db_manager
        self.running = True
        self._schema_probed = False
        self.adverse_selection_rate: float = self.ADVERSE_SELECTION_RATE

    def check_adverse_selection(self) -> bool:
        """Simulates adverse selection: 30% probability of immediate reversal upon touching entry."""
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

    def get_triggered_plans(self) -> List[Dict[str, Any]]:
        """Queries trade_plans table for records with status 'TRIGGERED'."""
        db = self._ensure_db()
        if not db:
            return []

        try:
            return retry_db_call(
                lambda: (
                    db.client.table("trade_plans")
                    .select("*")
                    .eq("status", "TRIGGERED")
                    .order("created_at", desc=False)
                    .execute()
                ).data or []
            )
        except Exception as e:
            logger.error(f"Error fetching triggered trade plans: {e}")
            return []

    def get_pending_plans(self) -> List[Dict[str, Any]]:
        """Backwards compatibility alias for get_triggered_plans."""
        return self.get_triggered_plans()

    def update_plan_status(
        self,
        plan_id: str,
        new_status: str,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Updates the status and metadata of a trade plan."""
        db = self._ensure_db()
        if not db:
            logger.info(f"[Dry Run] Plan {plan_id} status updated to {new_status}")
            return True

        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "status": new_status,
            "updated_at": now_iso,
        }

        def _do_update():
            # Fetch current metadata to merge
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
            logger.info(f"Trade plan {plan_id} transitioned to '{new_status}'")
            return True

        try:
            return retry_db_call(_do_update)
        except Exception as e:
            logger.error(f"Failed to update plan {plan_id} to {new_status}: {e}")
            return False

    def check_market_touch(
        self, symbol: str, direction: str, entry_price: float
    ) -> (bool, float):
        """
        Fetches current 1m candle / ticker to check if market touched the entry price.
        - SHORT order touches if high >= entry_price
        - LONG order touches if low <= entry_price
        Returns (touched: bool, current_price: float)
        """
        try:
            # Fetch recent 1m OHLCV to catch intra-minute price excursions
            df = self.fetcher.fetch_ohlcv(symbol, timeframe="1m", limit=3, use_cache=False)
            if df.empty:
                return False, 0.0

            latest_high = float(df["high"].max())
            latest_low = float(df["low"].min())
            current_close = float(df["close"].iloc[-1])

            if direction == "SHORT":
                touched = latest_high >= entry_price
            else:  # LONG
                touched = latest_low <= entry_price

            return touched, current_close
        except Exception as e:
            logger.error(f"Error checking market touch for {symbol}: {e}")
            return False, 0.0

    def evaluate_plan(self, plan: Dict[str, Any]) -> None:
        """Evaluates a single pending trade plan for fill or timeout."""
        plan_id = plan["id"]
        symbol = plan["symbol"]
        direction = plan.get("direction", "SHORT").upper()
        entry_price = float(plan["entry_price"])
        created_at_str = plan.get("created_at")

        # Parse creation time to enforce 15-minute timeout window
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except Exception:
            created_at = datetime.now(timezone.utc)

        elapsed_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()

        # Check for 15-minute timeout expiration
        if elapsed_seconds > self.TIMEOUT_SECONDS:
            logger.warning(
                f"[ORDER TIMEOUT] Plan {plan_id} ({symbol} {direction} @ {entry_price}) "
                f"unfilled after {elapsed_seconds / 60:.1f} mins. Marking TIMEOUT."
            )
            self.update_plan_status(
                plan_id,
                "TIMEOUT",
                {"timeout_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": elapsed_seconds},
            )
            return

        # Check if current market touched limit entry
        touched, current_price = self.check_market_touch(symbol, direction, entry_price)

        logger.info(
            f"Checking Plan {plan_id[:8]}.. [{symbol} {direction}] | "
            f"Entry: {entry_price:.2f} | Current: {current_price:.2f} | "
            f"Elapsed: {elapsed_seconds:.0f}s / {self.TIMEOUT_SECONDS}s | Touched: {touched}"
        )

        if touched:
            now_dt = datetime.now(timezone.utc)
            # Adverse selection check: 30% of the time, price crosses entry limit but immediately reverses
            if self.check_adverse_selection():
                logger.warning(
                    f"[ADVERSE SELECTION] Plan {plan_id} crossed entry limit {entry_price:.2f} "
                    f"but immediately reversed. Simulating missed fill -> REJECTED."
                )
                self.update_plan_status(
                    plan_id,
                    "REJECTED",
                    {
                        "rejected_at": now_dt.isoformat(),
                        "reason": "ADVERSE_SELECTION_REVERSAL",
                        "touch_price": current_price,
                        "elapsed_seconds": elapsed_seconds,
                    },
                )
                return

            # Simulate maker slippage: 0.0% to 0.05% (max half a tick)
            slippage = random.uniform(0, 0.0005)
            if direction == "LONG":
                fill_price = entry_price * (1.0 + slippage)
            else:
                fill_price = entry_price * (1.0 - slippage)

            logger.info(
                f"*** [SIMULATED FILL] Plan {plan_id} FILLED! {symbol} {direction} @ {fill_price:.2f} (slippage: {slippage * 10000:.1f} bps) ***"
            )

            # Update plan status to 'FILLED'
            self.update_plan_status(
                plan_id,
                "FILLED",
                {
                    "filled_at": now_dt.isoformat(),
                    "fill_price": fill_price,
                    "slippage": slippage,
                    "order_type": "POST_ONLY_MAKER",
                    "maker_fee_bps": 2.0,  # 0.02% maker fee assumption
                },
            )

            # Log to Supabase signals table as PAPER_FILL
            db = self._ensure_db()
            if db:
                try:
                    fill_metadata = {
                        "plan_id": plan_id,
                        "direction": direction,
                        "fill_price": entry_price,
                        "stop_loss": plan.get("stop_loss"),
                        "take_profit": plan.get("take_profit"),
                        "atr": plan.get("atr"),
                        "simulated_fill_time": now_dt.isoformat(),
                        "order_latency_ms": int(elapsed_seconds * 1000),
                    }
                    db.log_signal(
                        symbol=symbol,
                        event_time=now_dt,
                        signal_type="PAPER_FILL",
                        score=1.0,
                        metadata=fill_metadata,
                        known_at_time=now_dt,
                    )
                    logger.info(f"PAPER_FILL audit record logged to Supabase for plan {plan_id}.")
                except Exception as e:
                    logger.error(f"Failed to log PAPER_FILL audit: {e}")


    def fill_triggered_plan(self, plan: Dict[str, Any], fill_price: Optional[float] = None) -> bool:
        """Immediately fills a triggered plan and logs PAPER_FILL to Supabase using atomic locking."""
        plan_id = plan["id"]
        symbol = plan["symbol"]
        direction = plan.get("direction", "SHORT").upper()
        entry_price = float(plan.get("entry_price", 0.0))
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        meta = plan.get("plan_metadata") or {}

        # Task 3: Pydantic Schema Gate (TradePlan validation)
        atr_in_plan = float(plan.get("atr") or meta.get("atr") or 0.0)
        if atr_in_plan > 0:
            sl_dist = atr_in_plan * 1.5
            tp_dist = atr_in_plan * 3.0
            default_sl = (entry_price - sl_dist) if direction == "LONG" else (entry_price + sl_dist)
            default_tp = (entry_price + tp_dist) if direction == "LONG" else (entry_price - tp_dist)
        else:
            default_sl = (entry_price * 0.98) if direction == "LONG" else (entry_price * 1.02)
            default_tp = (entry_price * 1.04) if direction == "LONG" else (entry_price * 0.96)
        sl = plan.get("stop_loss") if "stop_loss" in plan else default_sl
        tp = plan.get("take_profit") if "take_profit" in plan else default_tp

        plan_dict = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "stop_loss": sl,
            "take_profit": tp,
            "position_size": float(plan.get("position_size") or 0.0),
            "atr": float(plan.get("atr") or (meta.get("atr") if isinstance(meta, dict) else 0.0) or 0.0),
        }
        for k, v in plan.items():
            if k not in ("id", "signal_id", "status", "plan_metadata", "created_at", "updated_at", "triggered_at"):
                if k not in plan_dict:
                    plan_dict[k] = v

        try:
            TradePlan(**plan_dict)
            logger.info(f"[SCHEMA GATE PASSED] Plan {plan_id} validated successfully.")
        except ValidationError as e:
            logger.error(f"[SCHEMA GATE REJECTED] Malformed trade plan {plan_id} failed validation: {e}")
            self.update_plan_status(
                plan_id,
                "REJECTED_SCHEMA",
                {"rejected_at": now_iso, "reason": str(e)},
            )
            return False

        # Task 5: Slippage Simulation for Honest Paper Fills (0.0% to 0.05%, max half a tick for Maker orders)
        slippage = random.uniform(0, 0.0005)
        if fill_price is not None:
            exec_price = fill_price
        elif direction == "LONG":
            fill_price = entry_price * (1.0 + slippage)
            exec_price = fill_price
        else:  # SHORT
            fill_price = entry_price * (1.0 - slippage)
            exec_price = fill_price
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        # Ghost Fill Check: Check triggered_at TTL (TTL = 60 seconds)
        meta = plan.get("plan_metadata") or {}
        triggered_at_raw = meta.get("triggered_at") or plan.get("triggered_at")

        db = self._ensure_db()

        # Fallback to query Supabase plan_metadata if not in local dictionary
        if not triggered_at_raw and db:
            try:
                plan_row = (
                    db.client.table("trade_plans")
                    .select("plan_metadata")
                    .eq("id", plan_id)
                    .limit(1)
                    .execute()
                )
                if plan_row.data and len(plan_row.data) > 0:
                    db_meta = plan_row.data[0].get("plan_metadata") or {}
                    triggered_at_raw = db_meta.get("triggered_at")
            except Exception as e:
                logger.debug(f"Could not fetch plan_metadata for plan {plan_id}: {e}")

        if triggered_at_raw:
            try:
                if isinstance(triggered_at_raw, str):
                    t_str = triggered_at_raw.replace("Z", "+00:00")
                    triggered_dt = datetime.fromisoformat(t_str)
                elif isinstance(triggered_at_raw, datetime):
                    triggered_dt = triggered_at_raw
                elif isinstance(triggered_at_raw, (int, float)):
                    triggered_dt = datetime.fromtimestamp(triggered_at_raw, tz=timezone.utc)
                else:
                    triggered_dt = None

                if triggered_dt:
                    if triggered_dt.tzinfo is None:
                        triggered_dt = triggered_dt.replace(tzinfo=timezone.utc)
                    age_seconds = (now_dt - triggered_dt).total_seconds()
                    if age_seconds > 60.0:
                        logger.warning(
                            f"[GHOST FILL PREVENTED] Plan {plan_id[:8]}.. TTL expired "
                            f"(triggered {age_seconds:.1f}s ago > 60s). Marking MISSED."
                        )
                        self.update_plan_status(
                            plan_id,
                            "MISSED",
                            {
                                "missed_at": now_iso,
                                "reason": f"TTL expired ({age_seconds:.1f}s > 60s)",
                            },
                        )
                        return False
            except Exception as e:
                logger.warning(f"Error checking triggered_at TTL for plan {plan_id}: {e}")

        # Task 1: Quant-Audited Dynamic Position Sizing
        account_balance = float(getattr(config, "account_balance", 100000.0))
        atr_value = float(plan.get("atr") or meta.get("atr") or (exec_price * 0.01))
        exchange = getattr(self.fetcher, "binance", None)
        entry_price_to_use = entry_price if entry_price > 0 else exec_price
        pos_size = calculate_position_size(
            account_balance=account_balance,
            entry_price=entry_price_to_use,
            stop_loss=sl,
            atr_value=atr_value,
            symbol=symbol,
            exchange=exchange,
        )

        # Atomic database locking: .update({"status": "FILLED", ...}).eq("id", plan_id).eq("status", "TRIGGERED").execute()
        fill_extra_metadata = {
            "filled_at": now_iso,
            "fill_price": exec_price,
            "slippage": slippage,
            "order_type": "POST_ONLY_MAKER",
            "maker_fee_bps": 2.0,
            "position_size": pos_size.units,
            "position_units": pos_size.units,
            "position_value": pos_size.total_value,
            "risk_amount": pos_size.risk_amount,
            "raw_quantity": pos_size.raw_quantity,
            "fee_adjusted_quantity": pos_size.fee_adjusted_quantity,
            "step_size": pos_size.step_size,
            "contract_multiplier": pos_size.contract_multiplier,
            "account_balance": account_balance,
        }

        if not db:
            logger.info(f"[Dry Run] Plan {plan_id} atomically transitioned to FILLED")
            return True

        try:
            # Merge existing metadata
            current = (
                db.client.table("trade_plans")
                .select("plan_metadata")
                .eq("id", plan_id)
                .limit(1)
                .execute()
            )
            merged_meta = {}
            if current.data and len(current.data) > 0:
                merged_meta = current.data[0].get("plan_metadata") or {}
            elif isinstance(meta, dict):
                merged_meta = dict(meta)

            merged_meta.update(fill_extra_metadata)

            payload = {
                "status": "FILLED",
                "updated_at": now_iso,
                "plan_metadata": merged_meta,
            }

            response = (
                db.client.table("trade_plans")
                .update(payload)
                .eq("id", plan_id)
                .eq("status", "TRIGGERED")
                .execute()
            )
            updated_rows = response.data or []
            if len(updated_rows) == 0:
                logger.warning(
                    f"Atomic lock failed: Plan {plan_id} was already filled or is not in TRIGGERED state (0 rows updated). Skipping fill."
                )
                return False

            logger.info(
                f"*** [SNIPER FILL] Plan {plan_id[:8]}.. FILLED! {symbol} {direction} @ {exec_price:.2f} ***"
            )
            logger.info(f"Trade plan {plan_id} atomically transitioned to 'FILLED'")
        except Exception as e:
            logger.error(f"Failed to atomically update plan {plan_id} to FILLED: {e}")
            return False

        # Log PAPER_FILL signal audit record
        try:
            fill_metadata = {
                "plan_id": plan_id,
                "direction": direction,
                "fill_price": exec_price,
                "slippage": slippage,
                "position_size": pos_size.units,
                "position_value": pos_size.total_value,
                "risk_amount": pos_size.risk_amount,
                "raw_quantity": pos_size.raw_quantity,
                "fee_adjusted_quantity": pos_size.fee_adjusted_quantity,
                "step_size": pos_size.step_size,
                "contract_multiplier": pos_size.contract_multiplier,
                "stop_loss": plan.get("stop_loss"),
                "take_profit": plan.get("take_profit"),
                "atr": atr_value,
                "simulated_fill_time": now_iso,
                "fill_source": "TRIGGER_POLLER",
            }
            db.log_signal(
                symbol=symbol,
                event_time=now_dt,
                signal_type="PAPER_FILL",
                score=1.0,
                metadata=fill_metadata,
                known_at_time=now_dt,
            )
            logger.info(f"PAPER_FILL audit record logged to Supabase for plan {plan_id[:8]}..")
        except Exception as e:
            logger.error(f"Failed to log PAPER_FILL audit: {e}")

        return True


    def partially_fill_plan(
        self,
        plan: Dict[str, Any],
        fill_ratio: float = 0.5,
        fill_price: Optional[float] = None
    ) -> bool:
        """Transitions a plan to PARTIALLY_FILLED state (e.g., partial maker fill)."""
        plan_id = plan["id"]
        symbol = plan["symbol"]
        direction = plan.get("direction", "SHORT").upper()
        entry_price = float(plan.get("entry_price", 0.0))
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        meta = plan.get("plan_metadata") or {}

        slippage = random.uniform(0, 0.0005)
        exec_price = fill_price if fill_price is not None else entry_price

        account_balance = float(getattr(config, "account_balance", 100000.0))
        atr_value = float(plan.get("atr") or meta.get("atr") or (exec_price * 0.01))
        pos_size = calculate_position_size(
            account_balance=account_balance,
            entry_price=entry_price,
            stop_loss=plan.get("stop_loss"),
            atr_value=atr_value,
            symbol=symbol,
        )
        partial_units = round(pos_size.units * fill_ratio, 4)

        partial_meta = {
            "partially_filled_at": now_iso,
            "fill_price": exec_price,
            "slippage": slippage,
            "order_type": "POST_ONLY_MAKER",
            "position_size": partial_units,
            "total_planned_size": pos_size.units,
            "fill_ratio": fill_ratio,
        }
        logger.info(f"*** [PARTIAL FILL] Plan {plan_id[:8]}.. PARTIALLY_FILLED ({fill_ratio * 100:.0f}%) @ {exec_price:.2f} ***")
        return self.update_plan_status(plan_id, "PARTIALLY_FILLED", partial_meta)

    def get_filled_plans(self) -> List[Dict[str, Any]]:
        """Queries trade_plans table for records with status 'FILLED'."""
        db = self._ensure_db()
        if not db:
            return []

        try:
            response = (
                db.client.table("trade_plans")
                .select("*")
                .eq("status", "FILLED")
                .order("created_at", desc=False)
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Error fetching filled trade plans: {e}")
            return []

    def close_trade(
        self,
        plan: Dict[str, Any],
        exit_price: float,
        exit_reason: str = "TRAILING_STOP"
    ) -> bool:
        """
        Task 4: The Trailing Stop Monitor.
        Closes a FILLED plan in database and logs a PAPER_EXIT signal.
        """
        plan_id = plan["id"]
        symbol = plan["symbol"]
        direction = plan.get("direction", "SHORT").upper()
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        logger.warning(
            f"[POSITION CLOSED] Plan {plan_id[:8]}.. ({symbol} {direction}) exited at {exit_price:.2f}. "
            f"Reason: {exit_reason}"
        )

        meta = plan.get("plan_metadata") or {}
        entry_price = float(plan.get("entry_price", 0.0) or meta.get("fill_price", 0.0))
        pos_units = float(
            plan.get("position_size")
            or meta.get("position_size")
            or meta.get("position_units")
            or 1.0
        )
        if direction == "LONG":
            gross_pnl = (exit_price - entry_price) * pos_units
        else:
            gross_pnl = (entry_price - exit_price) * pos_units

        # Estimated round-trip fees (0.04% maker/taker assumption)
        fee_rate = 0.0004
        fee_cost = (entry_price + exit_price) * pos_units * fee_rate
        realized_pnl = round(gross_pnl - fee_cost, 4)

        # 1. Update trade plan status to CLOSED
        self.update_plan_status(
            plan_id,
            "CLOSED",
            {
                "closed_at": now_iso,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "realized_pnl": realized_pnl,
            }
        )

        # 2. Log PAPER_EXIT signal to Supabase signals table
        db = self._ensure_db()
        if db:
            try:
                exit_meta = {
                    "plan_id": plan_id,
                    "symbol": symbol,
                    "direction": direction,
                    "entry_price": float(plan.get("entry_price", 0.0)),
                    "exit_price": exit_price,
                    "stop_loss": float(plan.get("stop_loss", 0.0)),
                    "take_profit": float(plan.get("take_profit", 0.0)),
                    "exit_reason": exit_reason,
                    "closed_at": now_iso,
                    "realized_pnl": realized_pnl,
                }
                db.log_signal(
                    symbol=symbol,
                    event_time=now_dt,
                    signal_type="PAPER_EXIT",
                    score=1.0,
                    metadata=exit_meta,
                    known_at_time=now_dt,
                )
                logger.info(f"PAPER_EXIT signal logged to Supabase for plan {plan_id[:8]}.. (Reason: {exit_reason})")
            except Exception as e:
                logger.error(f"Failed to log PAPER_EXIT signal for plan {plan_id}: {e}")

        return True

    def flatten_all_positions(self, reason: str = "CIRCUIT_BREAKER") -> Dict[str, Any]:
        """
        Immediately closes all FILLED and PARTIALLY_FILLED positions at market,
        and cancels all PENDING and TRIGGERED orders.
        """
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        db = self._ensure_db()
        closed_count = 0
        cancelled_count = 0

        # 1. Close all FILLED and PARTIALLY_FILLED positions at market
        filled_plans: List[Dict[str, Any]] = []
        if db:
            try:
                res = (
                    db.client.table("trade_plans")
                    .select("*")
                    .in_("status", ["FILLED", "PARTIALLY_FILLED"])
                    .execute()
                )
                filled_plans = res.data or []
            except Exception as e:
                logger.error(f"Error querying filled plans for flatten: {e}")
        else:
            filled_plans = self.get_filled_plans()

        for plan in filled_plans:
            sym = plan.get("symbol", "")
            direction = str(plan.get("direction", "LONG")).upper()
            entry_p = float(plan.get("entry_price", 0.0))
            touched, curr_p = False, 0.0
            try:
                touched, curr_p = self.check_market_touch(sym, direction, entry_p)
            except Exception:
                pass
            exit_price = curr_p if curr_p > 0 else entry_p
            self.close_trade(plan, exit_price=exit_price, exit_reason=reason)
            closed_count += 1

        # 2. Cancel all PENDING and TRIGGERED orders
        pending_plans: List[Dict[str, Any]] = []
        if db:
            try:
                res = (
                    db.client.table("trade_plans")
                    .select("*")
                    .in_("status", ["PENDING", "TRIGGERED"])
                    .execute()
                )
                pending_plans = res.data or []
            except Exception as e:
                logger.error(f"Error querying pending plans for cancel: {e}")

        for plan in pending_plans:
            plan_id = plan["id"]
            self.update_plan_status(
                plan_id,
                "CANCELLED",
                {
                    "cancelled_at": now_iso,
                    "reason": reason,
                    "cancel_reason": reason,
                },
            )
            cancelled_count += 1

        logger.warning(
            f"[FLATTEN COMPLETE] Closed {closed_count} position(s) at market and "
            f"cancelled {cancelled_count} pending order(s). Reason: {reason}"
        )
        return {
            "closed_positions": closed_count,
            "cancelled_orders": cancelled_count,
            "reason": reason,
        }

    def execute_friday_flatten(self, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Hard rule: At Friday 15:30 UTC (30 minutes BEFORE the Weekend Gate locks at 16:00 UTC),
        the bot must close all FILLED positions at market, cancel all PENDING orders,
        and set a flag in Supabase system_state to WEEKEND_FLATTENED.
        """
        now = now_dt or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        logger.warning(
            "[FRIDAY FLATTEN RULE] Executing hard liquidation at Friday 15:30 UTC "
            "(30 mins before 16:00 UTC Weekend Gate lock)..."
        )

        # 1. Close all FILLED positions at market and cancel all PENDING orders
        res = self.flatten_all_positions(reason="FRIDAY_FLATTEN")

        # 2. Update Supabase system_state to WEEKEND_FLATTENED
        db = self._ensure_db()
        if db:
            try:
                if hasattr(db, "set_weekend_flattened"):
                    db.set_weekend_flattened(reason="Friday 15:30 UTC pre-weekend flatten executed")
                else:
                    payload = {
                        "id": 1,
                        "status": "WEEKEND_FLATTENED",
                        "reason": "Friday 15:30 UTC pre-weekend flatten executed",
                        "updated_at": now_iso,
                    }
                    response = (
                        db.client.table("system_state")
                        .update(payload)
                        .eq("id", 1)
                        .execute()
                    )
                    if not response.data:
                        db.client.table("system_state").upsert(payload).execute()
                logger.info("Supabase system_state flag updated to WEEKEND_FLATTENED.")
            except Exception as e:
                logger.error(f"Failed to set WEEKEND_FLATTENED flag in Supabase: {e}")

        res["status"] = "WEEKEND_FLATTENED"
        return res

    def update_trailing_stop(
        self,
        plan: Dict[str, Any],
        current_price: Optional[float] = None,
        atr_value: Optional[float] = None,
        recent_highs: Optional[Any] = None,
        recent_lows: Optional[Any] = None,
        ema_20: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Task 3 & 4: Structure-Based Breakeven, Chandelier Trailing Stop & Stop Monitor.
        - Breakeven Rule:
            LONG: If current_price > recent swing high, stop = entry_price * 1.001.
            SHORT: If current_price < recent swing low, stop = entry_price * 0.999.
        - Trailing Rule:
            LONG: If hit breakeven, trail at max(recent_highs) - (ATR_MULTIPLIER_FOR_TRAIL * atr).
                  Only move stop UP, never down.
            SHORT: If hit breakeven, trail at min(recent_lows) + (ATR_MULTIPLIER_FOR_TRAIL * atr).
                  Only move stop DOWN, never up.
        - Stop Monitor (Task 4):
            LONG: If current_price <= stop_loss -> Close trade with exit_reason="TRAILING_STOP".
            SHORT: If current_price >= stop_loss -> Close trade with exit_reason="TRAILING_STOP".
        """
        plan_id = plan["id"]
        symbol = plan["symbol"]
        direction = str(plan.get("direction", "SHORT")).upper()
        entry_price = float(plan.get("entry_price", 0.0))
        current_stop = float(plan.get("stop_loss", 0.0))
        meta = plan.get("plan_metadata") or {}

        # 1. Determine current price
        if current_price is None:
            try:
                df_1m = self.fetcher.fetch_ohlcv(symbol, timeframe="1m", limit=3, use_cache=False)
                if df_1m is not None and not df_1m.empty:
                    current_price = float(df_1m["close"].iloc[-1])
            except Exception as e:
                logger.error(f"Error fetching current price for trailing stop {symbol}: {e}")

        if current_price is None or current_price <= 0:
            logger.warning(f"Unable to determine current price for plan {plan_id}")
            return None

        # Task 4 Check: Has current price hit the trailing stop?
        if direction == "LONG" and current_stop > 0 and current_price <= current_stop:
            self.close_trade(plan, exit_price=current_price, exit_reason="TRAILING_STOP")
            return {
                "plan_id": plan_id,
                "symbol": symbol,
                "closed": True,
                "exit_price": current_price,
                "exit_reason": "TRAILING_STOP",
            }
        elif direction == "SHORT" and current_stop > 0 and current_price >= current_stop:
            self.close_trade(plan, exit_price=current_price, exit_reason="TRAILING_STOP")
            return {
                "plan_id": plan_id,
                "symbol": symbol,
                "closed": True,
                "exit_price": current_price,
                "exit_reason": "TRAILING_STOP",
            }

        # Determine ATR
        atr = atr_value
        if atr is None or atr <= 0:
            atr = float(plan.get("atr") or meta.get("atr") or 0.0)
        if atr <= 0:
            atr = entry_price * 0.01  # 1% fallback

        trail_mult = getattr(config, "ATR_MULTIPLIER_FOR_TRAIL", 3.0)
        new_stop = current_stop
        is_breakeven = meta.get("is_breakeven", False)
        trailing_reason = ""

        if direction == "LONG":
            if recent_highs is not None:
                max_high = max(recent_highs) if hasattr(recent_highs, '__iter__') else float(recent_highs)
                swing_high = max_high
                if current_price > swing_high or is_breakeven:
                    is_breakeven = True
                    be_stop = entry_price * 1.001
                    proposed_stop = be_stop
                    trailing_reason = "Structure Breakeven (1.001 * Entry)"

                    # Chandelier Trailing
                    chandelier_stop = max_high - (trail_mult * atr)
                    if chandelier_stop > proposed_stop:
                        proposed_stop = chandelier_stop
                        trailing_reason = f"Chandelier Trail ({trail_mult}x ATR)"

                    if ema_20 is not None and ema_20 < current_price and ema_20 > proposed_stop:
                        proposed_stop = ema_20
                        trailing_reason = "1H 20-EMA Trail"

                    if proposed_stop > current_stop:
                        new_stop = round(proposed_stop, 4)
            else:
                # Default behavior when recent_highs not provided (1.5x ATR move)
                favorable_move = current_price - entry_price
                if favorable_move >= 1.5 * atr or is_breakeven:
                    is_breakeven = True
                    be_stop = entry_price * 1.001
                    proposed_stop = be_stop
                    trailing_reason = "Breakeven reached (>= 1.5x ATR)"

                    # 1x ATR trailing
                    trailing_atr_stop = current_price - 1.0 * atr
                    if trailing_atr_stop > proposed_stop:
                        proposed_stop = trailing_atr_stop
                        trailing_reason = "1x ATR Trailing Stop"

                    if ema_20 is not None and ema_20 < current_price and ema_20 > proposed_stop:
                        proposed_stop = ema_20
                        trailing_reason = "1H 20-EMA Trailing Stop"

                    if proposed_stop > current_stop:
                        new_stop = round(proposed_stop, 4)

        elif direction == "SHORT":
            if recent_lows is not None:
                min_low = min(recent_lows) if hasattr(recent_lows, '__iter__') else float(recent_lows)
                swing_low = min_low
                if current_price < swing_low or is_breakeven:
                    is_breakeven = True
                    be_stop = entry_price * 0.999
                    proposed_stop = be_stop
                    trailing_reason = "Structure Breakeven (0.999 * Entry)"

                    # Chandelier Trailing
                    chandelier_stop = min_low + (trail_mult * atr)
                    if chandelier_stop < proposed_stop:
                        proposed_stop = chandelier_stop
                        trailing_reason = f"Chandelier Trail ({trail_mult}x ATR)"

                    if ema_20 is not None and ema_20 > current_price and ema_20 < proposed_stop:
                        proposed_stop = ema_20
                        trailing_reason = "1H 20-EMA Trail"

                    if proposed_stop < current_stop or current_stop > entry_price:
                        new_stop = round(proposed_stop, 4)
            else:
                # Default behavior when recent_lows not provided (1.5x ATR move)
                favorable_move = entry_price - current_price
                if favorable_move >= 1.5 * atr or is_breakeven:
                    is_breakeven = True
                    be_stop = entry_price * 0.999
                    proposed_stop = be_stop
                    trailing_reason = "Breakeven reached (>= 1.5x ATR)"

                    # 1x ATR trailing
                    trailing_atr_stop = current_price + 1.0 * atr
                    if trailing_atr_stop < proposed_stop:
                        proposed_stop = trailing_atr_stop
                        trailing_reason = "1x ATR Trailing Stop"

                    if ema_20 is not None and ema_20 > current_price and ema_20 < proposed_stop:
                        proposed_stop = ema_20
                        trailing_reason = "1H 20-EMA Trailing Stop"

                    if proposed_stop < current_stop or current_stop > entry_price:
                        new_stop = round(proposed_stop, 4)

        result_payload = {
            "plan_id": plan_id,
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "current_price": current_price,
            "old_stop_loss": current_stop,
            "new_stop_loss": new_stop,
            "is_breakeven_reached": is_breakeven,
            "trailing_reason": trailing_reason,
            "closed": False,
        }

        # Persist updated stop loss to Supabase
        if new_stop != current_stop:
            logger.info(
                f"[TRAILING STOP UPDATE] Plan {plan_id[:8]}.. ({symbol} {direction}) | "
                f"Stop: {current_stop:.2f} -> {new_stop:.2f} | Reason: {trailing_reason} | Price: {current_price:.2f}"
            )
            db = self._ensure_db()
            if db:
                try:
                    updated_meta = dict(meta)
                    updated_meta["is_breakeven"] = is_breakeven
                    updated_meta["last_trailing_stop_update"] = datetime.now(timezone.utc).isoformat()
                    updated_meta["trailing_reason"] = trailing_reason
                    db.client.table("trade_plans").update({
                        "stop_loss": new_stop,
                        "plan_metadata": updated_meta,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", plan_id).execute()
                except Exception as e:
                    logger.error(f"Failed to update stop_loss in Supabase for plan {plan_id}: {e}")

        return result_payload

    def poll_once(self) -> int:
        """Runs a single polling cycle. Evaluates all TRIGGERED trade plans."""
        if not self._schema_probed and self._ensure_db():
            self._schema_probed = True
            self._probe_schema()

        # Task 2: Friday Flatten Rule (Friday 15:30 UTC, 30 min before 16:00 UTC Weekend Gate)
        if is_friday_flatten_time():
            db = self._ensure_db()
            current_status = None
            if db:
                try:
                    res = db.client.table("system_state").select("status").eq("id", 1).limit(1).execute()
                    if res.data and len(res.data) > 0:
                        current_status = str(res.data[0].get("status", "")).strip().upper()
                except Exception:
                    pass

            if current_status != "WEEKEND_FLATTENED":
                logger.warning("[FRIDAY FLATTEN RULE] Friday 15:30 UTC active. Executing pre-weekend liquidation...")
                self.execute_friday_flatten()
            else:
                logger.debug("[FRIDAY FLATTEN RULE] Already WEEKEND_FLATTENED. Skipping execution poll.")
            return 0

        db = self._ensure_db()
        if db and db.check_halt():
            logger.warning("[KILL SWITCH] System is HALTED. Skipping paper execution poll.")
            return 0

        triggered = self.get_triggered_plans()
        if triggered:
            logger.info(f"Evaluating {len(triggered)} TRIGGERED trade plan(s)...")
            for plan in triggered:
                self.fill_triggered_plan(plan)

        # Task 3: Manage Trailing Stops for FILLED positions
        filled = self.get_filled_plans()
        if filled:
            logger.info(f"Managing trailing stops for {len(filled)} FILLED position(s)...")
            for plan in filled:
                try:
                    self.update_trailing_stop(plan)
                except Exception as e:
                    logger.error(f"Error updating trailing stop for plan {plan.get('id')}: {e}")

        return len(triggered)

    def run_continuous(self, interval_seconds: int = 10) -> None:
        """Runs continuous polling loop every 10 seconds."""
        logger.info(f"Starting continuous Paper Executor loop (poll interval: {interval_seconds}s)...")
        while self.running:
            start_t = time.perf_counter()
            db = self._ensure_db()
            try:
                self.poll_once()
                latency = (time.perf_counter() - start_t) * 1000.0
                if db and hasattr(db, "update_agent_health"):
                    status = "WARNING" if latency > 2000 else "HEALTHY"
                    db.update_agent_health("PaperExecutor", status, latency, None)
            except Exception as e:
                latency = (time.perf_counter() - start_t) * 1000.0
                logger.error(f"Error in Paper Executor loop: {e}")
                if db and hasattr(db, "update_agent_health"):
                    db.update_agent_health("PaperExecutor", "CRITICAL", latency, str(e))

            time.sleep(interval_seconds)

    def stop(self) -> None:
        """Stops the executor loop."""
        self.running = False


if __name__ == "__main__":
    executor = PaperExecutor()
    executor.poll_once()
    print("Paper Executor single poll complete.")

PaperExecutor.calculate_position_size = staticmethod(calculate_position_size)
PaperExecutor.is_friday_flatten_time = staticmethod(is_friday_flatten_time)
