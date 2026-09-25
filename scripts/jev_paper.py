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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    ) -> None:
        self.state_path = str(state_path)
        self.initial_equity_usd = float(initial_equity_usd)
        self.cash: float = float(initial_equity_usd)
        self.position: Optional[dict] = None
        self.day: str = _utc_today()
        self.day_start_equity: float = float(initial_equity_usd)
        self.last_entry_ts_ms: Optional[int] = None

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
        )

    # -- actions ---------------------------------------------------------

    def apply_action(self, action: dict, symbol: str, price: float, ts_ms: int) -> dict:
        """Apply a decide() action locally. Returns executed/qty/usd/realized_pnl/detail.

        Never raises: bad input or a defensive mismatch yields ``executed: None``.
        """
        out = {"executed": None, "qty": 0.0, "usd": 0.0, "realized_pnl": 0.0, "detail": ""}
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
            kind = action.get("action")
            if kind == "enter":
                return self._enter(action, symbol, p, ts, out)
            if kind == "exit":
                return self._exit(symbol, p, ts, out)
            out["detail"] = f"no-op action={kind!r}"
            return out
        except Exception as exc:  # defensive: never raise into the loop
            out["detail"] = f"error: {type(exc).__name__}: {exc}"
            return out

    def _enter(self, action, symbol, price, ts_ms, out):
        if self.position is not None:
            out["detail"] = "enter while holding"
            return out
        size_fraction = _finite(action.get("size_fraction"))
        if size_fraction is None or size_fraction <= 0:
            out["detail"] = "bad size_fraction"
            return out
        equity = self.mark_to_market(price)  # flat => cash
        usd = size_fraction * equity
        qty = usd / price
        if usd <= 0 or qty <= 0:
            out["detail"] = "non-positive size"
            return out
        self.cash -= usd
        self.position = {
            "symbol": symbol,
            "qty": qty,
            "entry_price": price,
            "entry_ts_ms": ts_ms,
        }
        self.last_entry_ts_ms = ts_ms
        out.update(executed="enter", qty=qty, usd=usd, realized_pnl=0.0, detail="entered")
        self._append_trade(ts_ms, symbol, "buy", price, qty, usd, 0.0)
        return out

    def _exit(self, symbol, price, ts_ms, out):
        if self.position is None:
            out["detail"] = "exit while flat"
            return out
        pos = self.position
        qty = float(pos["qty"])
        entry_price = float(pos["entry_price"])
        proceeds = qty * price
        realized_pnl = (price - entry_price) * qty
        self.cash += proceeds
        self.position = None
        out.update(
            executed="exit", qty=qty, usd=proceeds, realized_pnl=realized_pnl, detail="exited"
        )
        self._append_trade(ts_ms, symbol, "sell", price, qty, proceeds, realized_pnl)
        return out

    def _append_trade(self, ts_ms, symbol, side, price, qty, usd, realized_pnl):
        line = {
            "ts_ms": int(ts_ms),
            "symbol": symbol,
            "side": side,
            "price": float(price),
            "qty": float(qty),
            "usd": float(usd),
            "realized_pnl": float(realized_pnl),
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
        }

    def save(self) -> None:
        """Atomic write: serialize to a tmp file in the same dir, then os.replace."""
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(self._state_dict(), indent=2))
        os.replace(tmp, path)

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
            }
        else:
            self.position = None

        self.day = str(data.get("day") or _utc_today())

        dse = _finite(data.get("day_start_equity"))
        if dse is not None and dse > 0:
            self.day_start_equity = dse

        le = _finite(data.get("last_entry_ts_ms"))
        self.last_entry_ts_ms = int(le) if le is not None else None
        return True
