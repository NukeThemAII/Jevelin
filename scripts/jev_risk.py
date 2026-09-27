#!/usr/bin/env python3
"""PortfolioRisk (M5) — deterministic portfolio-level risk. Stdlib only.

Pure math on a caller-supplied books snapshot + a tiny persisted state file.
No network, no Jev: code is in control. Enforced here (config/v2.yaml
``portfolio``; scripts/jev_config.py PortfolioConfig):

  * per-pair caps: spot notional <= pair_spot_cap x that pair's spot book
    equity; perps margin <= pair_perps_margin_cap x that pair's perps equity;
  * basket caps: total long notional <= basket_long_cap x total equity,
    total short notional <= basket_short_cap x total equity (all pairs);
  * global daily kill: portfolio daily PnL <= global_daily_loss (inclusive,
    UTC-midnight reset) blocks ALL entries;
  * drawdown halt: dd >= drawdown_halt from the running equity peak blocks
    ALL entries; clears only below drawdown_recover (hysteresis);
  * dust: capacity below min_position_pct x book equity is vetoed by the gate
    layers from entry_budget() (no dust positions).

EXITS AND STOPS ARE NEVER BLOCKED — every rule here applies to entries only.

Contract for the gate layers (``entry_budget`` -> ``decide(decide_perps)``
``risk=``): a dict with global_daily_kill / drawdown_halt booleans, the still-
deployable ``pair_remaining`` and ``basket_remaining_long`` /
``basket_remaining_short`` in the BOOK's unit (spot: notional; perps: margin,
baskets translated by the entry leverage) and ``min_position_pct``. A
missing/non-numeric capacity is treated as 0.0 — fail-CLOSED for entries,
exits unaffected.

State (``runtime/risk_state.json``, M0 atomic write; loaded at start):
equity peak, UTC daily-reset date + daily PnL base, drawdown-halt latch and
the last flags snapshot. Persistence errors never raise (old file kept).
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import PortfolioConfig, atomic_write_json  # noqa: E402

BOOKS = ("spot", "perps")


def _finite(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


class PortfolioRisk:
    """Portfolio risk state + pure capacity math. Deterministic and auditable."""

    def __init__(self, cfg: PortfolioConfig = None,
                 state_path: str = "runtime/risk_state.json",
                 pairs=()) -> None:
        self.cfg = cfg if cfg is not None else PortfolioConfig()
        self.state_path = str(state_path)
        self.pairs = tuple(pairs)
        # persisted state
        self.equity_peak = 0.0
        self.daily_pnl_base = 0.0
        self.day: Optional[str] = None
        self.drawdown_halted = False
        # recomputed per update()
        self.total_equity = 0.0
        self.daily_pnl_frac = 0.0
        self.drawdown = 0.0
        self.basket_long = 0.0
        self.basket_short = 0.0
        self.deployed = {}      # pair -> {"spot_notional", "perps_margin", ...}
        self.equity_by_book = {}
        self.risk_events = 0    # flag transitions since start
        self.flags = self._empty_flags()
        self._prev_flags = None
        self.load()  # persisted state is loaded at start (M5 spec)

    # -- flags -------------------------------------------------------------

    def _empty_flags(self) -> dict:
        return {"pair_cap": {pair: False for pair in self.pairs},
                "basket_long": False, "basket_short": False,
                "global_daily_kill": False, "drawdown_halt": False}

    # -- core update -------------------------------------------------------

    def update(self, books_state: dict, equity_by_book: dict,
               now: Optional[float] = None) -> dict:
        """Recompute every risk number from the marks snapshot; return flags.

        ``books_state``: {pair: {book: {"notional", "margin", "side"}}} —
        deployed notionals at current marks (spot: position notional; perps:
        position notional + posted margin + side).
        ``equity_by_book``: {pair: {book: equity}} — marked book equities.
        ``now``: epoch seconds (fixture clock in tests; UTC day for resets).
        """
        now_s = float(now) if now is not None else time.time()
        self.equity_by_book = {}
        self.deployed = {}
        total = 0.0
        basket_long = 0.0
        basket_short = 0.0
        pairs = list(self.pairs) or sorted(set(books_state) | set(equity_by_book))
        for pair in pairs:
            books = books_state.get(pair) or {}
            eqs = equity_by_book.get(pair) or {}
            snap = {"spot_notional": 0.0, "perps_margin": 0.0,
                    "perps_long_notional": 0.0, "perps_short_notional": 0.0}
            self.equity_by_book[pair] = {}
            for book in BOOKS:
                eq = _finite(eqs.get(book))
                if eq is None:
                    continue
                self.equity_by_book[pair][book] = eq
                total += eq
                d = books.get(book) or {}
                notional = max(0.0, _finite(d.get("notional")) or 0.0)
                if book == "spot":
                    snap["spot_notional"] += notional
                    basket_long += notional
                else:
                    snap["perps_margin"] += max(0.0, _finite(d.get("margin")) or 0.0)
                    if d.get("side") == "short":
                        snap["perps_short_notional"] += notional
                        basket_short += notional
                    else:
                        snap["perps_long_notional"] += notional
                        basket_long += notional
            self.deployed[pair] = snap
        self.total_equity = total
        self.basket_long = basket_long
        self.basket_short = basket_short

        # UTC-midnight daily reset: base restarts at the current total equity.
        today = datetime.fromtimestamp(now_s, tz=timezone.utc).date().isoformat()
        if today != self.day:
            self.day = today
            self.daily_pnl_base = total
        if not self.daily_pnl_base or self.daily_pnl_base <= 0:
            self.daily_pnl_base = total
        self.daily_pnl_frac = ((total - self.daily_pnl_base) / self.daily_pnl_base
                               if self.daily_pnl_base > 0 else 0.0)

        # running equity peak + drawdown hysteresis
        if total > self.equity_peak:
            self.equity_peak = total
        self.drawdown = ((self.equity_peak - total) / self.equity_peak
                         if self.equity_peak > 0 else 0.0)
        if not self.drawdown_halted and self.drawdown >= self.cfg.drawdown_halt:
            self.drawdown_halted = True
        elif self.drawdown_halted and self.drawdown < self.cfg.drawdown_recover:
            self.drawdown_halted = False

        flags = self._empty_flags()
        for pair in pairs:
            flags["pair_cap"][pair] = self._pair_cap_hit(pair)
        long_cap = self.cfg.basket_long_cap * total
        short_cap = self.cfg.basket_short_cap * total
        flags["basket_long"] = self.basket_long >= long_cap > 0
        flags["basket_short"] = self.basket_short >= short_cap > 0
        flags["global_daily_kill"] = self.daily_pnl_frac <= self.cfg.global_daily_loss
        flags["drawdown_halt"] = self.drawdown_halted
        self._log_transitions(flags, now_s)
        self.flags = flags
        return flags

    def _pair_cap_hit(self, pair) -> bool:
        """True when the pair is at/over a cap in any of its present books."""
        snap = self.deployed.get(pair) or {}
        eqs = self.equity_by_book.get(pair) or {}
        hit = False
        if "spot" in eqs:
            rem = self.cfg.pair_spot_cap * eqs["spot"] - snap.get("spot_notional", 0.0)
            hit = hit or rem <= 0.0
        if "perps" in eqs:
            rem = (self.cfg.pair_perps_margin_cap * eqs["perps"]
                   - snap.get("perps_margin", 0.0))
            hit = hit or rem <= 0.0
        return hit

    def _log_transitions(self, flags: dict, now_s: float) -> None:
        prev = self._prev_flags
        self._prev_flags = json.loads(json.dumps(flags))
        if prev is None:
            return
        changed = []
        for name in ("basket_long", "basket_short", "global_daily_kill",
                     "drawdown_halt"):
            if prev.get(name) != flags[name]:
                changed.append(f"{name}={'on' if flags[name] else 'off'}")
        for pair, hit in flags["pair_cap"].items():
            if prev.get("pair_cap", {}).get(pair) != hit:
                changed.append(f"pair_cap[{pair}]={'on' if hit else 'off'}")
        if changed:
            self.risk_events += len(changed)
            stamp = datetime.fromtimestamp(now_s, tz=timezone.utc).isoformat()
            print(f"risk: {stamp} {','.join(changed)}")

    # -- capacity -----------------------------------------------------------

    def basket_remaining(self, side: str) -> float:
        """Still-deployable basket notional for ``side`` (never negative)."""
        if side == "short":
            return max(0.0, self.cfg.basket_short_cap * self.total_equity
                       - self.basket_short)
        return max(0.0, self.cfg.basket_long_cap * self.total_equity
                   - self.basket_long)

    def _pair_remaining(self, book: str, pair: str, equity: float) -> float:
        snap = self.deployed.get(pair) or {}
        if book == "spot":
            return max(0.0, self.cfg.pair_spot_cap * equity
                       - snap.get("spot_notional", 0.0))
        return max(0.0, self.cfg.pair_perps_margin_cap * equity
                   - snap.get("perps_margin", 0.0))

    def remaining_capacity(self, book: str, side: str, pair: str, equity: float,
                           leverage: float = 1.0, entries: bool = True) -> float:
        """Max notional (spot) / margin (perps) still deployable.

        min(pair cap, basket cap for ``side``); 0.0 for entries under the
        global daily kill or the drawdown halt. Basket notionals translate to
        the book's unit via ``leverage`` (perps). Clamped at 0.
        """
        if entries and (self.flags.get("global_daily_kill")
                        or self.flags.get("drawdown_halt")):
            return 0.0
        eq = _finite(equity) or 0.0
        lev = _finite(leverage) or 1.0
        if book == "spot":
            return min(self._pair_remaining("spot", pair, eq),
                       self.basket_remaining("long"))
        return min(self._pair_remaining("perps", pair, eq),
                   self.basket_remaining(side) / max(lev, 1e-9))

    def entry_budget(self, book: str, pair: str, equity: float,
                     leverage: float = 1.0) -> dict:
        """The exact ``risk`` dict jev_gates.decide / jev_perps.decide_perps take.

        Capacities in the book's unit (spot: notional, leverage ignored;
        perps: margin, baskets divided by ``leverage``). Kill/halt apply to
        entries only and are left to the gates to enforce (exits bypass).
        """
        eq = _finite(equity) or 0.0
        unit = 1.0 if book == "spot" else max(_finite(leverage) or 1.0, 1e-9)
        return {
            "global_daily_kill": bool(self.flags.get("global_daily_kill")),
            "drawdown_halt": bool(self.flags.get("drawdown_halt")),
            "pair_remaining": self._pair_remaining(book, pair, eq),
            "basket_remaining_long": self.basket_remaining("long") / unit,
            "basket_remaining_short": self.basket_remaining("short") / unit,
            "min_position_pct": float(self.cfg.min_position_pct),
        }

    # -- persistence -------------------------------------------------------

    def _state_dict(self) -> dict:
        return {
            "equity_peak": self.equity_peak,
            "day": self.day,
            "daily_pnl_base": self.daily_pnl_base,
            "drawdown_halted": self.drawdown_halted,
            "flags": self.flags,
        }

    def save(self) -> bool:
        """Atomic write (M0 pattern). Fail-open: returns False, never raises —
        an injected write failure leaves the previous file untouched."""
        try:
            atomic_write_json(self.state_path, self._state_dict())
            return True
        except Exception as exc:  # persistence must never break trading
            print(f"risk: save error {self.state_path}: "
                  f"{type(exc).__name__}: {exc}")
            return False

    def load(self) -> bool:
        """Load persisted state. Returns False (keeps current) if unusable."""
        try:
            data = json.loads(Path(self.state_path).read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        peak = _finite(data.get("equity_peak"))
        if peak is not None and peak >= 0:
            self.equity_peak = peak
        base = _finite(data.get("daily_pnl_base"))
        if base is not None and base > 0:
            self.daily_pnl_base = base
        day = data.get("day")
        self.day = str(day) if isinstance(day, str) and day else None
        halted = data.get("drawdown_halted")
        self.drawdown_halted = bool(halted) if isinstance(halted, bool) else False
        flags = data.get("flags")
        if isinstance(flags, dict):
            merged = self._empty_flags()
            for name in ("basket_long", "basket_short", "global_daily_kill",
                         "drawdown_halt"):
                if isinstance(flags.get(name), bool):
                    merged[name] = flags[name]
            pair_flags = flags.get("pair_cap")
            if isinstance(pair_flags, dict):
                for pair in merged["pair_cap"]:
                    merged["pair_cap"][pair] = bool(pair_flags.get(pair))
            self.flags = merged
            self._prev_flags = json.loads(json.dumps(merged))
        return True