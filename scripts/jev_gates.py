#!/usr/bin/env python3
"""JevGate decision layer (M3): turn jev_scorer verdicts into trade actions.

Stdlib only. Pure function, no IO. Jev never places orders; this module only
returns a deterministic action dict ("enter" | "exit" | "skip") that the
executor may act on. Fail-open means: on missing/bad data we never trade.

M3 (docs/V2-DESIGN.md B.3; every number lives in RiskConfig / config/v2.yaml):
  * entry: pump >= 65, phase in {breakout, accumulation}, whipsaw <= 0.45
    (self-consistency fan-out in the coin-flip band), exhaustion <= 0.55,
    conf >= 0.65, regime != chop (counter-trend side vetoed per config);
  * exit while holding: dump >= 65 for 2 consecutive cycles OR one cycle
    dump >= 75; min hold 3 cycles for signal exits (stops/liq live in the
    portfolio and always fire). v1's exhaustion exit is superseded by this
    rule. Signal-streak counters and position age arrive on PortfolioState
    (persisted in the book JSONs) and flow back out on every result dict, so
    a supervisor restart keeps the hysteresis state;
  * sizing: confidence tiers — [0.65, 0.70) -> 60% of cap, [0.70, 0.85) -> 80%,
    >= 0.85 -> 100% (cap = max_position_fraction).

Entry check order (``reason`` = first failing gate; the bitmask records ALL
failing gates): daily_loss_kill, regime (chop/counter), capitulation,
phase_not_in_entry_set, low_pump, whipsaw (high_whipsaw / whipsaw_fanout_tie),
high_exhaustion, low_confidence, cooldown.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from jev_config import RiskConfig, bitmask_for  # noqa: F401  (RiskConfig re-export)

NUMERIC_KEYS = ("pump_0_100", "dump_0_100", "exhaustion_prob", "whipsaw_prob", "confidence")
REQUIRED_KEYS = NUMERIC_KEYS + ("phase",)


@dataclass
class PortfolioState:
    has_position: bool
    equity_usd: float
    daily_pnl_pct: float
    last_entry_ts_ms: Optional[int] = None
    exit_signal_cycles: int = 0   # consecutive cycles carrying the exit signal (M3)
    cycles_held: int = 0          # decision cycles since entry (M3 min hold)


def whipsaw_gate_name(w1, w2, fan_out, max_whipsaw) -> Optional[str]:
    """Whipsaw entry gate: None = pass, else the veto gate name.

    Single sample: w1 > max -> "high_whipsaw". Fan-out (2nd sample taken when
    the raw noul is in the coin-flip band): majority vote on passing (<= max);
    both pass -> None, both fail -> "high_whipsaw", split OR unusable 2nd
    sample -> "whipsaw_fanout_tie" (fail-closed, never fabricate consistency).
    """
    engaged = bool(fan_out) or w2 is not None
    if not engaged:
        return "high_whipsaw" if w1 > max_whipsaw else None
    if w2 is None:
        return "whipsaw_fanout_tie"
    p1, p2 = w1 <= max_whipsaw, w2 <= max_whipsaw
    if p1 and p2:
        return None
    if not p1 and not p2:
        return "high_whipsaw"
    return "whipsaw_fanout_tie"


def sizing_tier(conf: float, cfg) -> tuple:
    """(fraction_of_cap, tier_percent) — B.3 confidence banding."""
    lo, hi = cfg.tier_thresholds
    fr = cfg.tier_fractions
    frac = fr[0] if conf < lo else (fr[1] if conf < hi else fr[2])
    return frac, int(round(frac * 100))


def _result(action: str, reason: str, size_fraction: float = 0.0,
            size_tier: Optional[int] = None, failed: Optional[list] = None,
            decision_id: Optional[str] = None, exit_signal_cycles: int = 0,
            cycles_held: int = 0) -> dict:
    """Decision record: vetoed_by lists EVERY failing gate (F-P1-4)."""
    failed = list(failed or [])
    return {
        "action": action,
        "reason": reason,
        "size_fraction": float(size_fraction),
        "size_tier": size_tier,
        "vetoed_by": failed,
        "veto_bitmask": bitmask_for(failed),
        "decision_id": decision_id,
        "exit_signal_cycles": int(exit_signal_cycles),
        "cycles_held": int(cycles_held),
    }


def _skip(reason: str, failed: list, decision_id: Optional[str] = None,
          exit_signal_cycles: int = 0, cycles_held: int = 0) -> dict:
    return _result("skip", reason, 0.0, None, failed, decision_id,
                   exit_signal_cycles, cycles_held)


def _as_float(value) -> Optional[float]:
    """Finite float or None (bools and non-numerics rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _as_counter(value) -> int:
    """Non-negative int counter or 0 (never trust persisted state blindly)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def decide(verdict: dict, pf: PortfolioState, cfg: RiskConfig, now_ms: int,
           decision_id: Optional[str] = None, regime: Optional[str] = None) -> dict:
    """Map a Jev verdict + portfolio state (+ optional regime) to an action.

    Never raises. ``regime`` is "chop"/"trend_up"/"trend_down" or None (the
    caller has no regime data — deprecated v1 fallback — and the regime gates
    are skipped; the M3 supervisor always passes one).
    """
    try:
        did = decision_id
        if did is None and isinstance(verdict, dict):
            did = verdict.get("decision_id")
        return _decide(verdict, pf, cfg, now_ms, did, regime)
    except Exception as exc:  # defensive: never raise into the trading loop
        return _skip(f"malformed: {type(exc).__name__}: {exc}", ["malformed"], decision_id)


def _decide(verdict, pf: PortfolioState, cfg: RiskConfig, now_ms: int,
            decision_id: Optional[str] = None, regime: Optional[str] = None) -> dict:
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

    # fan-out sample (M3): present-but-unusable counts as a tie (fail-closed)
    w2_raw = verdict.get("whipsaw_prob_2")
    w2 = _as_float(w2_raw) if w2_raw is not None else None
    fan_out = bool(verdict.get("fan_out")) or w2 is not None

    # EXIT path — hysteresis (B.3): exits proceed even under daily loss kill
    # and are never regime-blocked. Counters flow back out for persistence.
    if pf.has_position:
        age_now = _as_counter(pf.cycles_held) + 1
        dump = vals["dump_0_100"]
        signal = dump >= cfg.exit_min_dump
        streak = _as_counter(pf.exit_signal_cycles) + 1 if signal else 0
        if age_now >= cfg.min_hold_cycles:  # min hold before signal exits
            if dump >= cfg.exit_hard_dump:
                return _result("exit", f"dump {dump} >= {cfg.exit_hard_dump} "
                               f"(single-tick exit bar)", 0.0, None, [], decision_id,
                               streak, age_now)
            if streak >= cfg.exit_consecutive_cycles:
                return _result("exit", f"dump {dump} >= {cfg.exit_min_dump} for "
                               f"{streak} consecutive cycles", 0.0, None, [],
                               decision_id, streak, age_now)
        return _result("skip", "hold", 0.0, None, [], decision_id, streak, age_now)

    # ENTER path — evaluate ALL gates (F-P1-4): record every failing gate, not
    # just the first. ``reason`` remains the first failing gate in check order.
    failed = []  # (gate name, readable reason) in check order
    if pf.daily_pnl_pct <= -cfg.daily_loss_limit_pct:
        failed.append(("daily_loss_kill",
                       f"daily_loss_kill: daily pnl {pf.daily_pnl_pct}% <= "
                       f"-{cfg.daily_loss_limit_pct}%"))
    if regime == "chop":
        failed.append(("regime_chop", "regime_chop: chop forbids all entries"))
    elif regime == "trend_down" and cfg.counter_trend == "block":
        failed.append(("regime_counter", "regime_counter: trend_down blocks long entries"))
    if phase == "capitulation":
        failed.append(("capitulation", "capitulation: phase blocks entries"))
    if phase not in cfg.entry_phases:
        failed.append(("phase_not_in_entry_set",
                       f"phase_not_in_entry_set: phase {phase} not in "
                       f"{list(cfg.entry_phases)}"))
    if vals["pump_0_100"] < cfg.entry_min_pump:
        failed.append(("low_pump",
                       f"low_pump: {vals['pump_0_100']} < {cfg.entry_min_pump}"))
    whip_gate = whipsaw_gate_name(vals["whipsaw_prob"], w2, fan_out,
                                  cfg.entry_max_whipsaw)
    if whip_gate == "high_whipsaw":
        if fan_out:
            failed.append(("high_whipsaw",
                           f"high_whipsaw: both fan-out samples > "
                           f"{cfg.entry_max_whipsaw}"))
        else:
            failed.append(("high_whipsaw",
                           f"high_whipsaw: {vals['whipsaw_prob']} > "
                           f"{cfg.entry_max_whipsaw}"))
    elif whip_gate == "whipsaw_fanout_tie":
        failed.append(("whipsaw_fanout_tie",
                       f"whipsaw_fanout_tie: fan-out samples disagree or 2nd "
                       f"sample unusable ({vals['whipsaw_prob']} vs {w2})"))
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

    frac, tier = sizing_tier(conf, cfg)
    size = round(cfg.max_position_fraction * frac, 4)
    return _result(
        "enter",
        f"all gates passed: pump={vals['pump_0_100']} whipsaw={vals['whipsaw_prob']} "
        f"exhaustion={vals['exhaustion_prob']} confidence={conf} tier={tier}%",
        size, tier, [], decision_id, 0, 0,
    )
