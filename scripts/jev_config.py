#!/usr/bin/env python3
"""Shared M0 plumbing — stdlib only.

Holds the named execution-cost constants (F-P0-1), the per-gate veto flags
(F-P1-4), decision-id generation (F-P1-3) and the atomic JSON writer (F-P2)
used by both paper books (jev_paper / jev_perps), both gate layers
(jev_gates / jev_perps) and paper_loop. The full config file is M3 — this
module only carries the named constants and tiny shared helpers.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntFlag
from pathlib import Path

# -- execution-cost defaults (M0 / F-P0-1) ---------------------------------
# Real Binance rates: spot 0.10% per side (maker=taker for the operator),
# USDT-M VIP0 taker 0.05% per side. Slippage 5 bps per side, 0.0 = perfect
# limit fills (explicitly allowed).
SPOT_FEE_RATE = 0.001
PERPS_TAKER_FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0005


@dataclass(frozen=True)
class ExecutionConfig:
    spot_fee_rate: float = SPOT_FEE_RATE
    perps_taker_fee_rate: float = PERPS_TAKER_FEE_RATE
    slippage_rate: float = SLIPPAGE_RATE


# -- per-gate veto flags (M0 / F-P1-4) -------------------------------------
class VetoFlags(IntFlag):
    NONE = 0
    NO_VERDICT = 1
    MALFORMED = 2
    DAILY_LOSS_KILL = 4
    CAPITULATION_BLOCK = 8
    WHIPSAW = 16
    EXHAUSTION = 32
    LOW_CONFIDENCE = 64
    COOLDOWN = 128
    LOW_PUMP = 256
    LOW_DUMP = 512
    FUNDING_VETO = 1024


# gate name (as used in ``vetoed_by``) -> flag; order = canonical bit order.
GATE_FLAG = {
    "no_verdict": VetoFlags.NO_VERDICT,
    "malformed": VetoFlags.MALFORMED,
    "daily_loss_kill": VetoFlags.DAILY_LOSS_KILL,
    "capitulation": VetoFlags.CAPITULATION_BLOCK,
    "high_whipsaw": VetoFlags.WHIPSAW,
    "high_exhaustion": VetoFlags.EXHAUSTION,
    "low_confidence": VetoFlags.LOW_CONFIDENCE,
    "cooldown": VetoFlags.COOLDOWN,
    "low_pump": VetoFlags.LOW_PUMP,
    "low_dump": VetoFlags.LOW_DUMP,
    "funding": VetoFlags.FUNDING_VETO,
}
GATE_NAMES = tuple(GATE_FLAG)


def bitmask_for(names) -> int:
    """Combined bitmask for a list of gate names (unknown names ignored)."""
    mask = VetoFlags.NONE
    for name in names or []:
        mask |= GATE_FLAG.get(name, VetoFlags.NONE)
    return int(mask)


def bit_names(mask: int) -> list:
    """Gate names whose bit is set, in canonical bit order."""
    return [name for name, flag in GATE_FLAG.items() if int(mask) & int(flag)]


# -- decision ids (M0 / F-P1-3) --------------------------------------------
def new_decision_id() -> str:
    """Unique per-cycle id: UTC timestamp + random suffix (globally unique)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


# -- atomic JSON persistence (M0 / F-P2) -----------------------------------
def atomic_write_json(path, obj) -> None:
    """Write ``obj`` as JSON: tmp file in the same dir + os.replace.

    A crash before the replace leaves the previous file untouched and
    parseable; the tmp file is cleaned up on failure.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(target) + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2))
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def append_jsonl(path, obj) -> None:
    """Append one JSON line. Logging must never break trading: swallow OSError."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, default=str) + "\n")
    except OSError:
        pass