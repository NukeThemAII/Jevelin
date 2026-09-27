#!/usr/bin/env python3
"""PaperPortfolio — local JSON-persisted paper-trading portfolio. Stdlib only.

Paper trading ONLY: this module never touches an exchange and never places an
order. The caller passes ``price`` into every method (there is deliberately no
``get_price`` here). State is a small JSON file plus an append-only trade log
JSONL (``state_path + ".trades.jsonl"``). Writes are atomic (tmp + os.replace).

Reuse: ``to_pf_state`` returns the ``PortfolioState`` dataclass consumed by
``jev_gates.decide``. No risk/gate logic is duplicated here.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import SLIPPAGE_RATE, SPOT_FEE_RATE, atomic_write_json
from jev_gates import PortfolioState  # noqa: E402  (path bootstrap above)


def _finite(value) -> Optional[float]:
    """Return a finite float or None (rejects bool / non-numeric / nan / inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _utc_today() -> str:
    """Current UTC date as 'YYYY-MM-DD'."""
    return datetime.now(timezone.utc).date().isoformat()


class PaperPortfolio:
    """JSON-persisted paper portfolio. Defensive: never raises on bad input."""

    def __init__(
        self,
        initial_equity_usd: float = 10000.0,
        state_path: str = "runtime/paper_state.json",
        fee_rate: float = SPOT_FEE_RATE,
        slippage_rate: float = SLIPPAGE_RATE,
    ) -> None:
        self.state_path = str(state_path)
        self.initial_equity_usd = float(initial_equity_usd)
        self.cash: float = float(initial_equity_usd)
        self.position: Optional[dict] = None
        self.day: str = _utc_today()
        self.day_start_equity: float = float(initial_equity_usd)
        self.last_entry_ts_ms: Optional[int] = None
        # Execution costs (M0 / F-P0-1): per-side fee + adverse slippage.
        fr = _finite(fee_rate)
        self.fee_rate: float = fr if fr is not None and fr >= 0 else float(SPOT_FEE_RATE)
        sr = _finite(slippage_rate)
        self.slippage_rate: float = sr if sr is not None and sr >= 0 else float(SLIPPAGE_RATE)
        self.fees_paid: float = 0.0
        self.slippage_paid: float = 0.0
        # M3 hysteresis state (persisted in the state JSON): consecutive exit-
        # signal cycles and decision cycles since entry.
        self.exit_signal_cycles: int = 0
        self.cycles_held: int = 0

    # -- accounting helpers ---------------------------------------------

    def _trade_log_path(self) -> Path:
        return Path(self.state_path + ".trades.jsonl")

    def _position_value(self, price) -> float:
        if not self.position:
            return 0.0
        p = _finite(price)
        if p is None or p <= 0:
            p = _finite(self.position.get("entry_price")) or 0.0
        return float(self.position["qty"]) * p

    def mark_to_market(self, price: float) -> float:
        """Equity = cash + qty*price. Falls back to entry price if price is bad."""
        return self.cash + self._position_value(price)

    def to_pf_state(self, price: Optional[float] = None) -> PortfolioState:
        """Build a jev_gates.PortfolioState for the decision layer.

        Handles UTC day rollover: on a new day, reset ``day_start_equity`` to the
        current marked equity so ``daily_pnl_pct`` restarts from zero.
        """
        equity = self.mark_to_market(price)
        today = _utc_today()
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
        base = self.day_start_equity
        daily_pnl_pct = ((equity - base) / base * 100.0) if base and base > 0 else 0.0
        return PortfolioState(
            has_position=self.position is not None,
            equity_usd=equity,
            daily_pnl_pct=daily_pnl_pct,
            last_entry_ts_ms=self.last_entry_ts_ms,
            exit_signal_cycles=self.exit_signal_cycles,
            cycles_held=self.cycles_held,
        )

    # -- actions ---------------------------------------------------------

    def apply_action(self, action: dict, symbol: str, price: float, ts_ms: int,
                     decision_id: Optional[str] = None) -> dict:
        """Apply a decide() action locally. Returns executed/qty/usd/realized_pnl/detail.

        Never raises: bad input or a defensive mismatch yields ``executed: None``.
        ``decision_id`` (or ``action["decision_id"]``) flows into every trade row.
        """
        out = {"executed": None, "qty": 0.0, "usd": 0.0, "realized_pnl": 0.0,
               "fees": 0.0, "slippage": 0.0, "detail": ""}
        try:
            if not isinstance(action, dict):
                out["detail"] = "bad action"
                return out
            p = _finite(price)
            if p is None or p <= 0:
                out["detail"] = "bad price"
                return out
            if _finite(ts_ms) is None:
                out["detail"] = "bad ts_ms"
                return out
            ts = int(ts_ms)
            did = decision_id if decision_id is not None else action.get("decision_id")
            reason = str(action.get("reason") or "")
            kind = action.get("action")
            if kind == "enter":
                return self._enter(action, symbol, p, ts, out, did, reason)
            if kind == "exit":
                return self._exit(symbol, p, ts, out, did, reason)
            # hold / skip: adopt the gate layer's updated hysteresis counters
            self._adopt_signal_state(action)
            out["detail"] = f"no-op action={kind!r}"
            return out
        except Exception as exc:  # defensive: never raise into the loop
            out["detail"] = f"error: {type(exc).__name__}: {exc}"
            return out

    def _adopt_signal_state(self, action) -> None:
        """Persist decide() hysteresis counters (M3): never trust junk."""
        if not isinstance(action, dict):
            return
        esc = action.get("exit_signal_cycles")
        if isinstance(esc, int) and not isinstance(esc, bool) and esc >= 0:
            self.exit_signal_cycles = esc
        ch = action.get("cycles_held")
        if isinstance(ch, int) and not isinstance(ch, bool) and ch >= 0:
            self.cycles_held = ch

    def _reset_signal_state(self) -> None:
        self.exit_signal_cycles = 0
        self.cycles_held = 0

    def _enter(self, action, symbol, price, ts_ms, out, decision_id=None, reason=""):
        if self.position is not None:
            out["detail"] = "enter while holding"
            return out
        size_fraction = _finite(action.get("size_fraction"))
        if size_fraction is None or size_fraction <= 0:
            out["detail"] = "bad size_fraction"
            return out
        equity = self.mark_to_market(price)  # flat => cash
        usd = size_fraction * equity  # target notional at quote price
        fill = price * (1.0 + self.slippage_rate)  # buys fill UP
        qty = usd / fill
        if usd <= 0 or qty <= 0:
            out["detail"] = "non-positive size"
            return out
        fee = usd * self.fee_rate
        slippage = (fill - price) * qty
        self.cash -= usd + fee  # fee reduces equity immediately
        self.fees_paid += fee
        self.slippage_paid += slippage
        self.position = {
            "symbol": symbol,
            "qty": qty,
            "entry_price": fill,  # fill price: all PnL math uses fills
            "entry_ts_ms": ts_ms,
            "entry_fee": fee,
        }
        self.last_entry_ts_ms = ts_ms
        self._reset_signal_state()  # fresh position: hysteresis counters restart
        out.update(executed="enter", qty=qty, usd=usd, realized_pnl=0.0,
                   fees=fee, slippage=slippage, detail="entered")
        self._append_trade(ts_ms, symbol, "buy", fill, qty, usd, 0.0, fee, slippage,
                           decision_id, reason or "entered",
                           size_tier=action.get("size_tier"))
        return out

    def _exit(self, symbol, price, ts_ms, out, decision_id=None, reason=""):
        if self.position is None:
            out["detail"] = "exit while flat"
            return out
        pos = self.position
        qty = float(pos["qty"])
        entry_price = float(pos["entry_price"])
        entry_fee = _finite(pos.get("entry_fee")) or 0.0
        fill = price * (1.0 - self.slippage_rate)  # sells fill DOWN
        proceeds = qty * fill
        fee = proceeds * self.fee_rate
        slippage = (price - fill) * qty
        # Realized is net of BOTH fill fees (slippage is in the fill prices).
        realized_pnl = (fill - entry_price) * qty - entry_fee - fee
        self.cash += proceeds - fee
        self.fees_paid += fee
        self.slippage_paid += slippage
        self.position = None
        self._reset_signal_state()  # closed: hysteresis counters restart
        out.update(
            executed="exit", qty=qty, usd=proceeds, realized_pnl=realized_pnl,
            fees=fee, slippage=slippage, detail="exited"
        )
        self._append_trade(ts_ms, symbol, "sell", fill, qty, proceeds, realized_pnl, fee,
                           slippage, decision_id, reason or "exited")
        return out

    def _append_trade(self, ts_ms, symbol, side, price, qty, usd, realized_pnl,
                      fees, slippage, decision_id, reason, size_tier=None):
        line = {
            "ts_ms": int(ts_ms),
            "decision_id": decision_id,
            "book": "spot",
            "symbol": symbol,
            "side": side,
            "price": float(price),
            "qty": float(qty),
            "usd": float(usd),
            "realized_pnl": float(realized_pnl),
            "fees": float(fees),
            "slippage": float(slippage),
            "reason": reason,
            "size_tier": size_tier,
        }
        try:
            path = self._trade_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError:
            pass

    # -- persistence -----------------------------------------------------

    def _state_dict(self) -> dict:
        return {
            "cash": self.cash,
            "position": self.position,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "last_entry_ts_ms": self.last_entry_ts_ms,
            "fees_paid": self.fees_paid,
            "slippage_paid": self.slippage_paid,
            "exit_signal_cycles": self.exit_signal_cycles,
            "cycles_held": self.cycles_held,
        }

    def save(self) -> None:
        """Atomic write (tmp in the same dir + os.replace); see jev_config."""
        atomic_write_json(self.state_path, self._state_dict())

    def load(self) -> bool:
        """Load state from disk. Returns False (keeps current state) if unusable."""
        path = Path(self.state_path)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False

        cash = _finite(data.get("cash"))
        if cash is not None and cash >= 0:
            self.cash = cash

        pos = data.get("position")
        qty = _finite(pos.get("qty")) if isinstance(pos, dict) else None
        entry_price = _finite(pos.get("entry_price")) if isinstance(pos, dict) else None
        if isinstance(pos, dict) and qty is not None and entry_price is not None:
            self.position = {
                "symbol": pos.get("symbol"),
                "qty": float(qty),
                "entry_price": float(entry_price),
                "entry_ts_ms": int(pos.get("entry_ts_ms") or 0),
                "entry_fee": _finite(pos.get("entry_fee")) or 0.0,
            }
        else:
            self.position = None

        self.day = str(data.get("day") or _utc_today())

        dse = _finite(data.get("day_start_equity"))
        if dse is not None and dse > 0:
            self.day_start_equity = dse

        le = _finite(data.get("last_entry_ts_ms"))
        self.last_entry_ts_ms = int(le) if le is not None else None
        fees = _finite(data.get("fees_paid"))
        self.fees_paid = fees if fees is not None and fees >= 0 else 0.0
        slip = _finite(data.get("slippage_paid"))
        self.slippage_paid = slip if slip is not None and slip >= 0 else 0.0
        esc = _finite(data.get("exit_signal_cycles"))
        self.exit_signal_cycles = int(esc) if esc is not None and esc >= 0 else 0
        ch = _finite(data.get("cycles_held"))
        self.cycles_held = int(ch) if ch is not None and ch >= 0 else 0
        return True
