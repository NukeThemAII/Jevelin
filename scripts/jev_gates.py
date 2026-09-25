#!/usr/bin/env python3
"""JevGate decision layer: turn jev_scorer verdicts into trade actions.

Stdlib only. Pure function, no IO. Jev never places orders; this module only
returns a deterministic action dict ("enter" | "exit" | "skip") that the
executor may act on. Fail-open means: on missing/bad data we never trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

NUMERIC_KEYS = ("pump_0_100", "dump_0_100", "exhaustion_prob", "whipsaw_prob", "confidence")
REQUIRED_KEYS = NUMERIC_KEYS + ("phase",)


@dataclass(frozen=True)
class RiskConfig:
    max_position_fraction: float = 0.20
    entry_min_pump: float = 60.0
    entry_max_whipsaw: float = 0.5
    entry_max_exhaustion: float = 0.6
    min_confidence: float = 0.6
    exit_min_dump: float = 60.0
    exit_exhaustion: float = 0.8
    cooldown_seconds: int = 900
    daily_loss_limit_pct: float = 5.0


@dataclass
class PortfolioState:
    has_position: bool
    equity_usd: float
    daily_pnl_pct: float
    last_entry_ts_ms: Optional[int] = None


def _result(action: str, reason: str, size_fraction: float = 0.0,
            vetoed_by: Optional[str] = None) -> dict:
    return {
        "action": action,
        "reason": reason,
        "size_fraction": float(size_fraction),
        "vetoed_by": vetoed_by,
    }


def _skip(reason: str, vetoed_by: Optional[str]) -> dict:
    return _result("skip", reason, 0.0, vetoed_by)


def _as_float(value) -> Optional[float]:
    """Finite float or None (bools and non-numerics rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def decide(verdict: dict, pf: PortfolioState, cfg: RiskConfig, now_ms: int) -> dict:
    """Map a Jev verdict + portfolio state to an action. Never raises."""
    try:
        return _decide(verdict, pf, cfg, now_ms)
    except Exception as exc:  # defensive: never raise into the trading loop
        return _skip(f"malformed: {type(exc).__name__}: {exc}", "malformed")


def _decide(verdict, pf: PortfolioState, cfg: RiskConfig, now_ms: int) -> dict:
    if not isinstance(verdict, dict):
        return _skip("malformed: verdict is not a dict", "malformed")

    # 1) fail-open: never trade on missing data
    if verdict.get("ok") is not True or verdict.get("confidence") is None:
        err = verdict.get("error")
        return _skip(f"no verdict ({err})" if err else "no verdict", "no_verdict")

    # malformed: missing keys / non-numeric values
    missing = [k for k in REQUIRED_KEYS if k not in verdict]
    if missing:
        return _skip(f"malformed: missing keys {missing}", "malformed")
    vals = {}
    for k in NUMERIC_KEYS:
        f = _as_float(verdict[k])
        if f is None:
            return _skip(f"malformed: {k} not a finite number", "malformed")
        vals[k] = f
    if not isinstance(verdict["phase"], str):
        return _skip("malformed: phase not a string", "malformed")
    phase = verdict["phase"]
    conf = vals["confidence"]

    # EXIT path (exits proceed even under daily loss kill)
    if pf.has_position:
        if vals["dump_0_100"] >= cfg.exit_min_dump:
            return _result("exit", f"dump {vals['dump_0_100']} >= {cfg.exit_min_dump}")
        if vals["exhaustion_prob"] >= cfg.exit_exhaustion:
            return _result(
                "exit", f"exhaustion {vals['exhaustion_prob']} >= {cfg.exit_exhaustion}")
        return _skip("hold", None)

    # ENTER path
    if pf.daily_pnl_pct <= -cfg.daily_loss_limit_pct:
        return _skip(
            f"daily_loss_kill: daily pnl {pf.daily_pnl_pct}% <= -{cfg.daily_loss_limit_pct}%",
            "daily_loss_kill")
    if phase == "capitulation":
        return _skip("capitulation: phase blocks entries", "capitulation")
    if vals["pump_0_100"] < cfg.entry_min_pump:
        return _skip(f"low_pump: {vals['pump_0_100']} < {cfg.entry_min_pump}", "low_pump")
    if vals["whipsaw_prob"] > cfg.entry_max_whipsaw:
        return _skip(
            f"high_whipsaw: {vals['whipsaw_prob']} > {cfg.entry_max_whipsaw}", "high_whipsaw")
    if vals["exhaustion_prob"] > cfg.entry_max_exhaustion:
        return _skip(
            f"high_exhaustion: {vals['exhaustion_prob']} > {cfg.entry_max_exhaustion}",
            "high_exhaustion")
    if conf < cfg.min_confidence:
        return _skip(f"low_confidence: {conf} < {cfg.min_confidence}", "low_confidence")
    if pf.last_entry_ts_ms is not None:
        elapsed = now_ms - pf.last_entry_ts_ms
        if elapsed < cfg.cooldown_seconds * 1000:
            return _skip(
                f"cooldown: {elapsed}ms since last entry < {cfg.cooldown_seconds * 1000}ms",
                "cooldown")

    size = round(cfg.max_position_fraction * conf, 4)
    return _result(
        "enter",
        f"all gates passed: pump={vals['pump_0_100']} whipsaw={vals['whipsaw_prob']} "
        f"exhaustion={vals['exhaustion_prob']} confidence={conf}",
        size,
        None,
    )
