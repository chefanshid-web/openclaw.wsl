import os

base = os.path.expanduser('~/crypto-screener-v2')
os.makedirs(f"{base}/scripts", exist_ok=True)

obs_report_content = '''#!/usr/bin/env python3
"""
scripts/obs_report.py — Read-only observation report on forward data collection.

Hard requirements:
- Read-only sqlite access: sqlite3.connect("file:data/signals.db?mode=ro", uri=True)
- Deduplicate on (symbol, timeframe, event_time, close, volume, funding, z_funding,
  open_interest, oi_change_pct, taker_ratio, long_short_ratio, vol_ratio)
- No imports of protected src.* modules (except config for db_path or hardcoded)
- No network calls
- Exit code 0 on success
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List


def parse_iso(ts_str: str) -> datetime:
    # Normalize Z to +00:00 for fromisoformat
    norm = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(norm)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def calc_percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(math.floor(k))
    c = int(math.ceil(k))
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def extract_base_asset(symbol: str) -> str:
    s = symbol.upper()
    for suffix in [":USDT", "/USDT:USDT", "/USDT", "USDT", "BUSD", "USD"]:
        if s.endswith(suffix):
            return s[:-len(suffix)]
    return s


def generate_report(db_path: str = "data/signals.db") -> str:
    # Connect strictly read-only
    if not os.path.exists(db_path):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        db_path = os.path.join(repo_root, "data", "signals.db")

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    cur = conn.cursor()

    # Raw count
    cur.execute("SELECT COUNT(*) FROM observations")
    raw_count = cur.fetchone()[0]

    # Deduplicated dataset
    cols = [
        "symbol", "timeframe", "event_time", "close", "volume", "funding", "z_funding",
        "open_interest", "oi_change_pct", "taker_ratio", "long_short_ratio", "vol_ratio"
    ]
    cur.execute(f"""
        SELECT DISTINCT {', '.join(cols)}
        FROM observations
    """)
    distinct_rows: List[Dict[str, Any]] = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()

    distinct_count = len(distinct_rows)
    dup_count = raw_count - distinct_count
    dup_rate = (dup_count / raw_count * 100.0) if raw_count > 0 else 0.0

    lines: List[str] = []

    # 1. OBSERVATIONS
    lines.append("OBSERVATIONS")
    lines.append(f"  raw rows: {raw_count} | distinct bars: {distinct_count} | duplicate rate: {dup_rate:.2f}% ({dup_count} duplicates)")
    
    # Per-symbol observations breakdown
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for r in distinct_rows:
        by_symbol.setdefault(r["symbol"], []).append(r)

    # Sort symbols alphabetically
    all_symbols = sorted(by_symbol.keys())
    for sym in all_symbols:
        sym_rows = by_symbol[sym]
        dts = [parse_iso(r["event_time"]) for r in sym_rows]
        first_dt = min(dts) if dts else None
        last_dt = max(dts) if dts else None
        span_days = ((last_dt - first_dt).total_seconds() / 86400.0) if (first_dt and last_dt) else 0.0
        first_str = first_dt.isoformat() if first_dt else "N/A"
        last_str = last_dt.isoformat() if last_dt else "N/A"
        lines.append(f"  {sym:<8} distinct: {len(sym_rows):<4} first: {first_str}  last: {last_str}  span: {span_days:.2f}d")

    lines.append("")

    # 2. COVERAGE
    lines.append("COVERAGE")
    # Coverage of 15m bars and sequence gaps (>1 bar elapsed, i.e. delta > 15m)
    for sym in all_symbols:
        rows_15m = [r for r in by_symbol[sym] if r["timeframe"] == "15m"]
        # Unique bar timestamps for 15m
        unique_15m_dts = sorted(set(parse_iso(r["event_time"]) for r in rows_15m))
        count_15m = len(unique_15m_dts)
        
        gaps: List[str] = []
        for i in range(1, len(unique_15m_dts)):
            delta = unique_15m_dts[i] - unique_15m_dts[i - 1]
            step_min = delta.total_seconds() / 60.0
            if step_min > 15.0:
                missing = int(round(step_min / 15.0)) - 1
                gaps.append(f"[{unique_15m_dts[i-1].isoformat()} -> {unique_15m_dts[i].isoformat()} ({missing} bar(s) missed)]")

        gap_str = f"gaps: {len(gaps)}" if not gaps else f"gaps: {len(gaps)} {', '.join(gaps)}"
        lines.append(f"  {sym:<8} distinct 15m bars: {count_15m:<4} (rows: {len(rows_15m):<4}) | {gap_str}")

    lines.append("")

    # 3. FUNDING
    lines.append("FUNDING")
    lines.append("  per symbol: min / p05 / median / p95 / max in basis points (rate * 10000)")
    for sym in all_symbols:
        f_bps = [r["funding"] * 10000.0 for r in by_symbol[sym] if r["funding"] is not None]
        if not f_bps:
            lines.append(f"  {sym:<8} N/A (no funding data)")
        else:
            f_bps.sort()
            f_min = f_bps[0]
            f_p05 = calc_percentile(f_bps, 5)
            f_med = calc_percentile(f_bps, 50)
            f_p95 = calc_percentile(f_bps, 95)
            f_max = f_bps[-1]
            lines.append(f"  {sym:<8} min: {f_min:+7.4f} | p05: {f_p05:+7.4f} | median: {f_med:+7.4f} | p95: {f_p95:+7.4f} | max: {f_max:+7.4f} bps (n={len(f_bps)})")

    lines.append("")

    # 4. SYMBOL HYGIENE
    lines.append("SYMBOL HYGIENE")
    # Distinct symbol values with row count
    asset_groups: Dict[str, List[str]] = {}
    for sym in all_symbols:
        row_cnt = len(by_symbol[sym])
        lines.append(f"  symbol: {sym:<8} distinct rows: {row_cnt}")
        base = extract_base_asset(sym)
        asset_groups.setdefault(base, []).append(sym)

    # Flag multiple names for same asset
    hygiene_defects: List[str] = []
    for base, sym_list in sorted(asset_groups.items()):
        if len(sym_list) > 1:
            hygiene_defects.append(f"Asset '{base}' appears under multiple names: {', '.join(sym_list)}")
            lines.append(f"  [DEFECT] Asset '{base}' split across multiple names: {', '.join(sym_list)}")

    if not hygiene_defects:
        lines.append("  [OK] No mixed symbol naming detected.")

    lines.append("")

    # 5. FEATURE NULLS
    lines.append("FEATURE NULLS")
    lines.append(f"  null counts across {distinct_count} distinct rows:")
    check_cols = ["open_interest", "oi_change_pct", "taker_ratio", "long_short_ratio", "vol_ratio", "z_funding"]
    for col in check_cols:
        null_count = sum(1 for r in distinct_rows if r[col] is None)
        pct = (null_count / distinct_count * 100.0) if distinct_count > 0 else 0.0
        lines.append(f"  {col:<18} nulls: {null_count:<4} / {distinct_count:<4} ({pct:5.1f}%)")

    lines.append("")

    # 6. VERDICT
    lines.append("VERDICT")
    # Threshold check based on maximum distinct bars per symbol
    max_bars = max((len(rows) for rows in by_symbol.values()), default=0)
    if max_bars < 200:
        literal = "INSUFFICIENT - keep collecting"
    elif max_bars <= 2000:
        literal = "DESCRIPTIVE ONLY - too short for regime analysis"
    else:
        literal = "ANALYSABLE - but check regime coverage before concluding anything"

    defect_msgs = []
    if hygiene_defects:
        defect_msgs.append(f"mixed-symbol defect present ({', '.join(hygiene_defects)})")
    if dup_count > 0:
        defect_msgs.append(f"duplicate bars present ({dup_count} duplicate rows, {dup_rate:.2f}% rate)")

    if defect_msgs:
        defect_summary = " | Defects: " + "; ".join(defect_msgs)
    else:
        defect_summary = " | No duplicate or mixed-symbol defects present"

    verdict_line = f"  {literal}{defect_summary}"
    lines.append(verdict_line)

    return "\\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Report on market observations in data/signals.db (read-only).")
    parser.add_argument("--md", action="store_true", help="Write report to reports/observations_report.md")
    parser.add_argument("--db", default="data/signals.db", help="Path to sqlite database")
    args = parser.parse_args()

    report_text = generate_report(db_path=args.db)
    print(report_text)

    if args.md:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        rep_dir = os.path.join(repo_root, "reports")
        os.makedirs(rep_dir, exist_ok=True)
        out_path = os.path.join(rep_dir, "observations_report.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("# Observations Report\\n\\n```\\n" + report_text + "\\n```\\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

target_file = f"{base}/scripts/obs_report.py"
with open(target_file, "w", encoding="utf-8") as f:
    f.write(obs_report_content)

print(f"Created {target_file}")
