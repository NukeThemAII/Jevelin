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

from jev_config import bitmask_for

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
            failed: Optional[list] = None, decision_id: Optional[str] = None) -> dict:
    """Decision record: vetoed_by lists EVERY failing gate (F-P1-4)."""
    failed = list(failed or [])
    return {
        "action": action,
        "reason": reason,
        "size_fraction": float(size_fraction),
        "vetoed_by": failed,
        "veto_bitmask": bitmask_for(failed),
        "decision_id": decision_id,
    }


def _skip(reason: str, failed: list, decision_id: Optional[str] = None) -> dict:
    return _result("skip", reason, 0.0, failed, decision_id)


def _as_float(value) -> Optional[float]:
    """Finite float or None (bools and non-numerics rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def decide(verdict: dict, pf: PortfolioState, cfg: RiskConfig, now_ms: int,
           decision_id: Optional[str] = None) -> dict:
    """Map a Jev verdict + portfolio state to an action. Never raises."""
    try:
        did = decision_id
        if did is None and isinstance(verdict, dict):
            did = verdict.get("decision_id")
        return _decide(verdict, pf, cfg, now_ms, did)
    except Exception as exc:  # defensive: never raise into the trading loop
        return _skip(f"malformed: {type(exc).__name__}: {exc}", ["malformed"], decision_id)


def _decide(verdict, pf: PortfolioState, cfg: RiskConfig, now_ms: int,
            decision_id: Optional[str] = None) -> dict:
    if not isinstance(verdict, dict):
        return _skip("malformed: verdict is not a dict", ["malformed"], decision_id)

    # 1) fail-open: never trade on missing data
    if verdict.get("ok") is not True or verdict.get("confidence") is None:
        err = verdict.get("error")
        return _skip(f"no verdict ({err})" if err else "no verdict", ["no_verdict"],
                     decision_id)

    # malformed: missing keys / non-numeric values
    missing = [k for k in REQUIRED_KEYS if k not in verdict]
    if missing:
        return _skip(f"malformed: missing keys {missing}", ["malformed"], decision_id)
    vals = {}
    for k in NUMERIC_KEYS:
        f = _as_float(verdict[k])
        if f is None:
            return _skip(f"malformed: {k} not a finite number", ["malformed"], decision_id)
        vals[k] = f
    if not isinstance(verdict["phase"], str):
        return _skip("malformed: phase not a string", ["malformed"], decision_id)
    phase = verdict["phase"]
    conf = vals["confidence"]

    # EXIT path (exits proceed even under daily loss kill)
    if pf.has_position:
        if vals["dump_0_100"] >= cfg.exit_min_dump:
            return _result("exit", f"dump {vals['dump_0_100']} >= {cfg.exit_min_dump}",
                           0.0, [], decision_id)
        if vals["exhaustion_prob"] >= cfg.exit_exhaustion:
            return _result(
                "exit", f"exhaustion {vals['exhaustion_prob']} >= {cfg.exit_exhaustion}",
                0.0, [], decision_id)
        return _skip("hold", [], decision_id)

    # ENTER path — evaluate ALL gates (F-P1-4): record every failing gate, not
    # just the first. ``reason`` remains the first failing gate in check order.
    failed = []  # (gate name, readable reason) in check order
    if pf.daily_pnl_pct <= -cfg.daily_loss_limit_pct:
        failed.append(("daily_loss_kill",
                       f"daily_loss_kill: daily pnl {pf.daily_pnl_pct}% <= "
                       f"-{cfg.daily_loss_limit_pct}%"))
    if phase == "capitulation":
        failed.append(("capitulation", "capitulation: phase blocks entries"))
    if vals["pump_0_100"] < cfg.entry_min_pump:
        failed.append(("low_pump",
                       f"low_pump: {vals['pump_0_100']} < {cfg.entry_min_pump}"))
    if vals["whipsaw_prob"] > cfg.entry_max_whipsaw:
        failed.append(("high_whipsaw",
                       f"high_whipsaw: {vals['whipsaw_prob']} > {cfg.entry_max_whipsaw}"))
    if vals["exhaustion_prob"] > cfg.entry_max_exhaustion:
        failed.append(("high_exhaustion",
                       f"high_exhaustion: {vals['exhaustion_prob']} > "
                       f"{cfg.entry_max_exhaustion}"))
    if conf < cfg.min_confidence:
        failed.append(("low_confidence",
                       f"low_confidence: {conf} < {cfg.min_confidence}"))
    if pf.last_entry_ts_ms is not None:
        elapsed = now_ms - pf.last_entry_ts_ms
        if elapsed < cfg.cooldown_seconds * 1000:
            failed.append(("cooldown",
                           f"cooldown: {elapsed}ms since last entry < "
                           f"{cfg.cooldown_seconds * 1000}ms"))
    if failed:
        return _skip(failed[0][1], [name for name, _ in failed], decision_id)

    size = round(cfg.max_position_fraction * conf, 4)
    return _result(
        "enter",
        f"all gates passed: pump={vals['pump_0_100']} whipsaw={vals['whipsaw_prob']} "
        f"exhaustion={vals['exhaustion_prob']} confidence={conf}",
        size,
        [],
        decision_id,
    )
