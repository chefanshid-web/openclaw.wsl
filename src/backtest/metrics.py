"""
OpenClaw Quant Lab - Backtest & Execution Metrics
File: src/backtest/metrics.py

Provides rigorous statistical evaluation for trading strategies:
1. Bootstrap Confidence Intervals for Sharpe Ratio and Win Rate.
2. Net Expectancy calculation with transaction friction.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import numpy as np
import pandas as pd


def _extract_returns_and_pnls(
    trades: Sequence[Any],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Extracts returns (pct), realized PnLs (USD), and cost (USD) from various trade representations:
    - Sequence of numeric returns or PnLs (float, int)
    - Sequence of dicts (with keys 'return', 'returns', 'realized_pnl', 'pnl', 'fee_cost', 'cost')
    - pandas Series / DataFrame

    Returns:
        (returns_array, pnl_array, costs_array)
    """
    if trades is None:
        return np.array([]), np.array([]), np.array([])

    if isinstance(trades, (pd.Series, pd.DataFrame)):
        if isinstance(trades, pd.DataFrame):
            col = "return" if "return" in trades.columns else ("realized_pnl" if "realized_pnl" in trades.columns else trades.columns[0])
            raw_vals = trades[col].dropna().values
            return np.asarray(raw_vals, dtype=float), np.asarray(raw_vals, dtype=float), np.zeros(len(raw_vals))
        raw_vals = trades.dropna().values
        return np.asarray(raw_vals, dtype=float), np.asarray(raw_vals, dtype=float), np.zeros(len(raw_vals))

    returns_list: List[float] = []
    pnls_list: List[float] = []
    costs_list: List[float] = []

    for t in trades:
        if isinstance(t, (int, float, np.number)):
            val = float(t)
            returns_list.append(val)
            pnls_list.append(val)
            costs_list.append(0.0)
        elif isinstance(t, dict):
            # Check for realized pnl or return in dict or plan_metadata
            meta = t.get("plan_metadata") if isinstance(t.get("plan_metadata"), dict) else {}

            pnl = t.get("realized_pnl")
            if pnl is None:
                pnl = t.get("pnl")
            if pnl is None:
                pnl = meta.get("realized_pnl")
            if pnl is None:
                pnl = meta.get("pnl")

            ret = t.get("return")
            if ret is None:
                ret = t.get("returns")
            if ret is None:
                ret = meta.get("return")
            if ret is None:
                ret = meta.get("returns")

            cost = t.get("fee_cost")
            if cost is None:
                cost = t.get("cost")
            if cost is None:
                cost = meta.get("fee_cost")
            if cost is None:
                cost = meta.get("cost")

            cost_val = float(cost) if cost is not None else 0.0

            if ret is not None:
                ret_val = float(ret)
                pnl_val = float(pnl) if pnl is not None else ret_val
            elif pnl is not None:
                pnl_val = float(pnl)
                ret_val = pnl_val
            else:
                # Outcome fallback
                outcome = str(t.get("outcome") or meta.get("outcome") or "").upper()
                status = str(t.get("status") or "").upper()
                if outcome in ("WIN", "TAKE_PROFIT", "TP"):
                    ret_val = 1.0
                    pnl_val = 1.0
                elif outcome in ("LOSS", "STOP_LOSS", "SL") or status == "STOP_LOSS_HIT":
                    ret_val = -1.0
                    pnl_val = -1.0
                else:
                    continue

            returns_list.append(ret_val)
            pnls_list.append(pnl_val)
            costs_list.append(cost_val)

    return (
        np.asarray(returns_list, dtype=float),
        np.asarray(pnls_list, dtype=float),
        np.asarray(costs_list, dtype=float),
    )


def calculate_sharpe_ratio(
    returns: np.ndarray,
    risk_free_rate: float = 0.0,
    annualize_factor: Optional[float] = None,
) -> float:
    """
    Computes Sharpe Ratio with Bessel correction (ddof=1).
    By default annualizes using the CPCV project benchmark (sqrt(252 * 24 * 4)).
    """
    ret = np.asarray(returns, dtype=float)
    if len(ret) < 2:
        return 0.0
    excess = ret - risk_free_rate
    std = float(np.std(excess, ddof=1))
    if std <= 1e-12 or np.isnan(std):
        return 0.0

    if annualize_factor is None:
        factor = float(np.sqrt(252 * 24 * 4))
    else:
        factor = float(annualize_factor)

    return float(np.mean(excess) / std * factor)


def calculate_bootstrap_confidence(
    trades: Sequence[Any],
    n_bootstraps: int = 1000,
    random_seed: Optional[int] = None,
    annualize_sharpe: bool = True,
    annualize_factor: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Resamples trade returns with replacement n_bootstraps times and computes
    the 5th and 95th percentiles of the Sharpe Ratio and Win Rate.

    Args:
        trades: A sequence of trade returns (floats), trade dictionaries, or trade objects.
        n_bootstraps: Number of bootstrap iterations (default: 1000).
        random_seed: Optional seed for reproducible bootstrapping.
        annualize_sharpe: Whether to annualize the Sharpe ratio.
        annualize_factor: Custom annualization factor (default: sqrt(252 * 24 * 4)).

    Returns:
        Dictionary containing:
        - sharpe_ci: Tuple (5th_percentile, 95th_percentile)
        - win_rate_ci: Tuple (5th_percentile, 95th_percentile) in percent [0, 100]
        - sharpe_p5, sharpe_p95: 5th and 95th percentiles for Sharpe
        - win_rate_p5, win_rate_p95: 5th and 95th percentiles for Win Rate (%)
        - sharpe: Dict with detailed statistics
        - win_rate: Dict with detailed statistics
        - n_bootstraps: Number of iterations executed
        - n_samples: Number of trade samples
    """
    returns, _, _ = _extract_returns_and_pnls(trades)
    n_samples = len(returns)

    # Empty / Insufficient data guard
    if n_samples == 0:
        return {
            "sharpe_ci": (0.0, 0.0),
            "win_rate_ci": (0.0, 0.0),
            "sharpe_5th": 0.0,
            "sharpe_95th": 0.0,
            "win_rate_5th": 0.0,
            "win_rate_95th": 0.0,
            "sharpe_p5": 0.0,
            "sharpe_p95": 0.0,
            "win_rate_p5": 0.0,
            "win_rate_p95": 0.0,
            "sharpe_mean": 0.0,
            "win_rate_mean": 0.0,
            "sharpe": {
                "mean": 0.0,
                "p5": 0.0,
                "p95": 0.0,
                "ci_5th": 0.0,
                "ci_95th": 0.0,
                "ci": (0.0, 0.0),
            },
            "win_rate": {
                "mean": 0.0,
                "p5": 0.0,
                "p95": 0.0,
                "ci_5th": 0.0,
                "ci_95th": 0.0,
                "ci": (0.0, 0.0),
            },
            "n_bootstraps": n_bootstraps,
            "n_samples": 0,
        }

    rng = np.random.default_rng(random_seed)
    factor = annualize_factor if annualize_factor is not None else (np.sqrt(252 * 24 * 4) if annualize_sharpe else 1.0)

    # Vectorized bootstrap resampling
    # Shape: (n_bootstraps, n_samples)
    indices = rng.integers(0, n_samples, size=(n_bootstraps, n_samples))
    boot_samples = returns[indices]

    # Win rate per bootstrap sample (percentage 0-100)
    boot_wins = (boot_samples > 0).sum(axis=1)
    boot_win_rates = (boot_wins / float(n_samples)) * 100.0

    # Sharpe ratio per bootstrap sample
    if n_samples >= 2:
        means = np.mean(boot_samples, axis=1)
        stds = np.std(boot_samples, axis=1, ddof=1)
        # Avoid division by zero
        valid = stds > 1e-12
        boot_sharpes = np.zeros(n_bootstraps, dtype=float)
        boot_sharpes[valid] = (means[valid] / stds[valid]) * factor
    else:
        boot_sharpes = np.zeros(n_bootstraps, dtype=float)

    # Compute 5th and 95th percentiles
    sharpe_p5 = float(np.percentile(boot_sharpes, 5))
    sharpe_p95 = float(np.percentile(boot_sharpes, 95))
    win_rate_p5 = float(np.percentile(boot_win_rates, 5))
    win_rate_p95 = float(np.percentile(boot_win_rates, 95))

    sharpe_mean = float(np.mean(boot_sharpes))
    win_rate_mean = float(np.mean(boot_win_rates))

    return {
        "sharpe_ci": (sharpe_p5, sharpe_p95),
        "win_rate_ci": (win_rate_p5, win_rate_p95),
        "sharpe_5th": sharpe_p5,
        "sharpe_95th": sharpe_p95,
        "win_rate_5th": win_rate_p5,
        "win_rate_95th": win_rate_p95,
        "sharpe_p5": sharpe_p5,
        "sharpe_p95": sharpe_p95,
        "win_rate_p5": win_rate_p5,
        "win_rate_p95": win_rate_p95,
        "sharpe_mean": sharpe_mean,
        "win_rate_mean": win_rate_mean,
        "sharpe": {
            "mean": sharpe_mean,
            "p5": sharpe_p5,
            "p95": sharpe_p95,
            "ci_5th": sharpe_p5,
            "ci_95th": sharpe_p95,
            "ci": (sharpe_p5, sharpe_p95),
        },
        "win_rate": {
            "mean": win_rate_mean,
            "p5": win_rate_p5,
            "p95": win_rate_p95,
            "ci_5th": win_rate_p5,
            "ci_95th": win_rate_p95,
            "ci": (win_rate_p5, win_rate_p95),
        },
        "n_bootstraps": n_bootstraps,
        "n_samples": n_samples,
    }


def calculate_expectancy(
    win_rate: float,
    avg_win_usd: float,
    loss_rate: Optional[float] = None,
    avg_loss_usd: float = 0.0,
    avg_costs_usd: float = 0.0,
) -> float:
    """
    Calculates Net Expectancy per trade:
    Expectancy = (Win_Rate * Avg_Win_USD) - (Loss_Rate * Avg_Loss_USD) - Avg_Costs_USD

    Args:
        win_rate: Probability of winning trade (either decimal [0, 1] or percentage [0, 100]).
        avg_win_usd: Average dollar gain on winning trades.
        loss_rate: Optional probability of losing trade. If None, defaults to 1.0 - win_rate.
        avg_loss_usd: Average dollar loss on losing trades (passed as positive or negative value).
        avg_costs_usd: Average transaction friction (commissions, slippage, funding) in USD.

    Returns:
        Net expected dollar outcome per trade.
    """
    # Normalize win_rate to [0, 1]
    p_win = float(win_rate) / 100.0 if float(win_rate) > 1.0 else float(win_rate)
    p_win = max(0.0, min(1.0, p_win))

    if loss_rate is None:
        p_loss = 1.0 - p_win
    else:
        p_loss = float(loss_rate) / 100.0 if float(loss_rate) > 1.0 else float(loss_rate)
        p_loss = max(0.0, min(1.0, p_loss))

    avg_win = float(avg_win_usd)
    avg_loss = abs(float(avg_loss_usd))
    costs = float(avg_costs_usd)

    expectancy = (p_win * avg_win) - (p_loss * avg_loss) - costs
    return float(expectancy)
