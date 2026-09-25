#!/usr/bin/env python3
"""Deterministic market-state builder for Jev shadow scoring.

build_state(symbol, closes, trades, now_ms) -> dict

Assumptions (match scripts/jev_scorer.py):
- `closes` are 1-minute closes, evenly spaced 60s apart, most recent at `now_ms`.
- `trades` are (ts_ms, side, usd) with side in {"buy","sell"} (case-insensitive).

Float features are left unrounded (deterministic IEEE ops). Only
`last_60s_price_path_pct_from_start` is rounded to 4 decimals.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

STEP_MS = 60_000
WINDOW_30S_MS = 30_000
WINDOW_60S_MS = 60_000
LAG_60S = 1  # closes
LAG_300S = 5  # closes
PATH_ROUND = 4


def _fmt_usd(usd: float) -> str:
    if usd == int(usd):
        return str(int(usd))
    return f"{usd:.4f}".rstrip("0").rstrip(".")


def _pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


def build_state(
    symbol: str,
    closes: Sequence[float],
    trades: Sequence[Tuple[int, str, float]],
    now_ms: int,
) -> dict:
    """Build the Jev state dict. Deterministic; same inputs -> same output."""
    closes = list(closes)
    # Sort trades by ts; keep side lowercased. Newest last.
    norm_trades: List[Tuple[int, str, float]] = []
    for ts, side, usd in trades:
        norm_trades.append((int(ts), str(side).lower(), float(usd)))
    norm_trades.sort(key=lambda t: t[0])

    n = len(closes)
    last = closes[-1] if n else 0.0

    if n >= 1 + LAG_60S:
        return_60s_pct = _pct_change(last, closes[-1 - LAG_60S])
    else:
        return_60s_pct = 0.0
    if n >= 1 + LAG_300S:
        return_300s_pct = _pct_change(last, closes[-1 - LAG_300S])
    else:
        return_300s_pct = 0.0

    # 5m window = last 5 closes (5 one-minute points including the latest)
    window = closes[-5:] if n else []
    if window:
        high = max(window)
        low = min(window)
        vs_5m_high_pct = _pct_change(last, high)
        vs_5m_low_pct = _pct_change(last, low)
    else:
        vs_5m_high_pct = 0.0
        vs_5m_low_pct = 0.0

    # Price path over closes inside the last 60s, pct from the first in-window close
    # close_ts[i] = now_ms - (n-1-i)*STEP_MS  ->  in window iff i >= n-2
    if n == 0:
        path: List[float] = []
    else:
        start_idx = max(0, n - 2)  # 60s / 60s step = 1 lag back
        window_closes = closes[start_idx:]
        base = window_closes[0]
        path = [round(_pct_change(c, base), PATH_ROUND) for c in window_closes]

    # Trade features
    usd_values = [u for _, _, u in norm_trades]
    if usd_values:
        ordered = sorted(usd_values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            median_usd = ordered[mid]
        else:
            median_usd = (ordered[mid - 1] + ordered[mid]) / 2.0
    else:
        median_usd = 0.0
    oversized_threshold = 3.0 * median_usd

    last_60s = [t for t in norm_trades if t[0] >= now_ms - WINDOW_60S_MS]
    buys_60s = sum(u for _, side, u in last_60s if side == "buy")
    sells_60s = sum(u for _, side, u in last_60s if side == "sell")
    flow_total = buys_60s + sells_60s
    buy_flow_share = (buys_60s / flow_total) if flow_total > 0 else 0.0

    oversized_buy_count_60s = sum(
        1 for _, side, u in last_60s if side == "buy" and u > oversized_threshold
    )
    oversized_sell_count_60s = sum(
        1 for _, side, u in last_60s if side == "sell" and u > oversized_threshold
    )

    cutoff = now_ms - WINDOW_30S_MS
    last_30s = [t for t in norm_trades if t[0] >= cutoff]
    older = [t for t in norm_trades if t[0] < cutoff]
    rate_last = len(last_30s) / 0.5  # trades/min over the 30s window
    if older:
        span_min = (cutoff - older[0][0]) / float(STEP_MS)
        rate_base = (len(older) / span_min) if span_min > 0 else 0.0
    else:
        rate_base = 0.0
    if rate_base > 0:
        trade_rate_ratio = rate_last / rate_base
    else:
        trade_rate_ratio = 0.0

    last_10 = norm_trades[-10:]
    last_10_trades_side_usd = [f"{side},{_fmt_usd(u)}" for _, side, u in last_10]

    return {
        "symbol": symbol,
        "asof_ms": int(now_ms),
        "features": {
            "return_60s_pct": return_60s_pct,
            "return_300s_pct": return_300s_pct,
            "buy_flow_share": buy_flow_share,
            "trade_rate_ratio": trade_rate_ratio,
            "oversized_buy_count_60s": oversized_buy_count_60s,
            "oversized_sell_count_60s": oversized_sell_count_60s,
            "vs_5m_high_pct": vs_5m_high_pct,
            "vs_5m_low_pct": vs_5m_low_pct,
        },
        "last_60s_price_path_pct_from_start": path,
        "last_10_trades_side_usd": last_10_trades_side_usd,
    }
