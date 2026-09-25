#!/usr/bin/env python3
"""PerpsPortfolio + decide_perps — local JSON-persisted PAPER perpetuals book. Stdlib only.

Paper trading ONLY: this module never touches an exchange and never places an
order. The caller passes ``price`` (and optionally a public ``funding_rate``)
into every call. Long AND short, isolated-margin style, one position at a time.

Reuse (no duplicated logic):
  * ``jev_gates``: PortfolioState (extended with ``side``), _as_float,
    NUMERIC_KEYS / REQUIRED_KEYS verdict validation contract.
  * ``jev_paper``: _finite, _utc_today, the apply_action / save / load pattern.

Safety invariants:
  * every stored position has a stop_price and a liq_price;
  * leverage is hard-capped at PerpsConfig.max_leverage, margin at
    max_margin_fraction of equity;
  * automatic exits in apply_action: liquidation > stop-loss > explicit exit;
  * no flip: with a position held decide_perps only ever returns exit/skip.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_gates import NUMERIC_KEYS, REQUIRED_KEYS, PortfolioState, _as_float  # noqa: E402
from jev_paper import _finite, _utc_today  # noqa: E402

FUNDING_PERIOD_MS = 8 * 60 * 60 * 1000  # 28_800_000 ms = one 8h funding period
LIQ_BUFFER = 0.95  # approx liquidation: 95% of the initial-margin move (no MM math)
SIDES = ("long", "short")


@dataclass(frozen=True)
class PerpsConfig:
    max_leverage: float = 3.0
    max_margin_fraction: float = 0.10
    entry_min_pump: float = 60.0
    short_min_dump: float = 60.0
    min_confidence: float = 0.6
    entry_max_whipsaw: float = 0.5
    entry_max_exhaustion: float = 0.6
    exit_exhaustion: float = 0.8
    cooldown_seconds: int = 900
    daily_loss_limit_pct: float = 5.0
    stop_loss_pct: float = 2.0
    max_abs_funding_pct: float = 0.01  # percent per 8h -> 0.0001 as a rate
    # Long-exit dump threshold (rule 2 references it; same default as RiskConfig).
    exit_min_dump: float = 60.0


@dataclass
class PerpsPortfolioState(PortfolioState):
    """jev_gates.PortfolioState + the held side ("long" | "short" | None)."""

    side: Optional[str] = None


# ---------------------------------------------------------------------------
# price helpers (pure)
# ---------------------------------------------------------------------------

def stop_price_for(side: str, entry: float, cfg: PerpsConfig) -> float:
    pct = cfg.stop_loss_pct / 100.0
    return entry * (1.0 - pct) if side == "long" else entry * (1.0 + pct)


def liq_price_for(side: str, entry: float, leverage: float) -> float:
    move = (1.0 / leverage) * LIQ_BUFFER
    return entry * (1.0 - move) if side == "long" else entry * (1.0 + move)


def unrealized_pnl(side: str, entry: float, price: float, qty: float) -> float:
    return (price - entry) * qty if side == "long" else (entry - price) * qty


def funding_paid_for(pos: dict, close_ts_ms: int) -> float:
    """Funding paid over the hold (positive = paid by us, negative = received).

    rate_at_entry * notional * whole_8h_periods * (+1 long | -1 short).
    """
    rate = _finite(pos.get("funding_rate_at_entry"))
    if rate is None:
        return 0.0
    duration = max(0, int(close_ts_ms) - int(pos.get("entry_ts_ms") or 0))
    periods = duration // FUNDING_PERIOD_MS
    sign = 1.0 if pos.get("side") == "long" else -1.0
    return rate * float(pos["notional_usd"]) * periods * sign


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------

class PerpsPortfolio:
    """JSON-persisted paper perps book. Defensive: never raises on bad input."""

    def __init__(
        self,
        initial_equity_usd: float = 10000.0,
        state_path: str = "runtime/perps_btc.json",
        cfg: Optional[PerpsConfig] = None,
    ) -> None:
        self.state_path = str(state_path)
        self.cfg = cfg if cfg is not None else PerpsConfig()
        self.initial_equity_usd = float(initial_equity_usd)
        self.equity: float = float(initial_equity_usd)  # realized account equity
        self.position: Optional[dict] = None
        self.last_entry_ts_ms: Optional[int] = None
        self.day: str = _utc_today()
        self.day_start_equity: float = float(initial_equity_usd)

    def _trade_log_path(self) -> Path:
        return Path(self.state_path + ".trades.jsonl")

    # -- accounting ------------------------------------------------------

    def mark_to_market(self, price) -> float:
        """equity + unrealized PnL. Falls back to entry price if price is bad."""
        if not self.position:
            return self.equity
        pos = self.position
        p = _finite(price)
        if p is None or p <= 0:
            p = float(pos["entry_price"])
        upnl = unrealized_pnl(pos["side"], float(pos["entry_price"]), p, float(pos["qty"]))
        # Isolated margin: a position can never lose more than its margin.
        upnl = max(upnl, -float(pos["margin_usd"]))
        return self.equity + upnl

    def to_pf_state(self, price: Optional[float] = None) -> PerpsPortfolioState:
        """PortfolioState (+side) for decide_perps; UTC day rollover like PaperPortfolio."""
        equity = self.mark_to_market(price)
        today = _utc_today()
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
        base = self.day_start_equity
        daily_pnl_pct = ((equity - base) / base * 100.0) if base and base > 0 else 0.0
        return PerpsPortfolioState(
            has_position=self.position is not None,
            equity_usd=equity,
            daily_pnl_pct=daily_pnl_pct,
            last_entry_ts_ms=self.last_entry_ts_ms,
            side=self.position["side"] if self.position else None,
        )

    # -- actions ---------------------------------------------------------

    def apply_action(self, action: dict, symbol: str, price: float, ts_ms: int,
                     funding_rate: Optional[float] = None) -> dict:
        """Apply a decide_perps() action locally. Never raises.

        Automatic exits run first, whatever the action: (a) liquidation,
        (b) stop-loss; then (c) the explicit action.
        """
        out = {"executed": None, "qty": 0.0, "usd": 0.0, "realized_pnl": 0.0,
               "funding_paid": 0.0, "detail": ""}
        try:
            p = _finite(price)
            if p is None or p <= 0:
                out["detail"] = "bad price"
                return out
            if _finite(ts_ms) is None:
                out["detail"] = "bad ts_ms"
                return out
            ts = int(ts_ms)

            if self.position is not None:
                pos = self.position
                long_ = pos["side"] == "long"
                liq, stop = float(pos["liq_price"]), float(pos["stop_price"])
                if (long_ and p <= liq) or (not long_ and p >= liq):
                    return self._close(symbol, p, ts, out, "liquidated", "liquidated")
                if (long_ and p <= stop) or (not long_ and p >= stop):
                    return self._close(symbol, p, ts, out, "stop_loss", "stop_loss")

            if not isinstance(action, dict):
                out["detail"] = "bad action"
                return out
            kind = action.get("action")
            if kind in ("enter_long", "enter_short"):
                side = "long" if kind == "enter_long" else "short"
                return self._enter(side, action, symbol, p, ts, funding_rate, out)
            if kind == "exit":
                if self.position is None:
                    out["detail"] = "exit while flat"
                    return out
                return self._close(symbol, p, ts, out, "exited",
                                   str(action.get("reason") or "exit"))
            out["detail"] = f"no-op action={kind!r}"
            return out
        except Exception as exc:  # defensive: never raise into the loop
            out["detail"] = f"error: {type(exc).__name__}: {exc}"
            return out

    def _enter(self, side, action, symbol, price, ts_ms, funding_rate, out):
        cfg = self.cfg
        if self.position is not None:
            out["detail"] = "enter while holding"  # no flip, no pyramiding
            return out
        size_fraction = _finite(action.get("size_fraction"))
        if size_fraction is None or size_fraction <= 0:
            out["detail"] = "bad size_fraction"
            return out
        size_fraction = min(size_fraction, cfg.max_margin_fraction)  # hard cap
        lev_raw = _finite(action.get("leverage", cfg.max_leverage))
        if lev_raw is None or lev_raw <= 0:
            out["detail"] = "bad leverage"
            return out
        leverage = min(cfg.max_leverage, lev_raw)  # hard cap, never exceed
        margin = size_fraction * self.equity
        notional = margin * leverage
        qty = notional / price
        if margin <= 0 or qty <= 0:
            out["detail"] = "non-positive size"
            return out
        self.position = {
            "symbol": symbol,
            "side": side,
            "margin_usd": margin,
            "leverage": leverage,
            "notional_usd": notional,
            "qty": qty,
            "entry_price": price,
            "stop_price": stop_price_for(side, price, cfg),
            "liq_price": liq_price_for(side, price, leverage),
            "entry_ts_ms": ts_ms,
            "funding_rate_at_entry": _finite(funding_rate),
        }
        self.last_entry_ts_ms = ts_ms
        out.update(executed=f"enter_{side}", qty=qty, usd=notional, realized_pnl=0.0,
                   detail="entered")
        self._append_trade(ts_ms, symbol, side, f"enter_{side}", price, qty, notional,
                           leverage, 0.0, 0.0, str(action.get("reason") or "entered"))
        return out

    def _close(self, symbol, price, ts_ms, out, detail, reason):
        pos = self.position
        side, qty = pos["side"], float(pos["qty"])
        margin = float(pos["margin_usd"])
        if detail == "liquidated":
            realized = -margin  # whole margin lost
        else:
            realized = unrealized_pnl(side, float(pos["entry_price"]), price, qty)
            realized = max(realized, -margin)  # isolated margin floor
        funding = funding_paid_for(pos, ts_ms)
        self.equity += realized - funding
        self.position = None
        out.update(executed="exit", qty=qty, usd=float(pos["notional_usd"]),
                   realized_pnl=realized, funding_paid=funding, detail=detail)
        self._append_trade(ts_ms, symbol or pos.get("symbol"), side, detail, price, qty,
                           float(pos["notional_usd"]), float(pos["leverage"]), realized,
                           funding, reason)
        return out

    def _append_trade(self, ts_ms, symbol, side, action, price, qty, notional, leverage,
                      realized_pnl, funding_paid, reason):
        line = {
            "ts_ms": int(ts_ms),
            "symbol": symbol,
            "side": side,
            "action": action,
            "price": float(price),
            "qty": float(qty),
            "notional": float(notional),
            "leverage": float(leverage),
            "realized_pnl": float(realized_pnl),
            "funding_paid": float(funding_paid),
            "reason": reason,
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
            "equity": self.equity,
            "position": self.position,
            "last_entry_ts_ms": self.last_entry_ts_ms,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
        }

    def save(self) -> None:
        """Atomic write: tmp file in the same dir, then os.replace."""
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(self._state_dict(), indent=2))
        os.replace(tmp, path)

    def _load_position(self, pos) -> Optional[dict]:
        if not isinstance(pos, dict) or pos.get("side") not in SIDES:
            return None
        nums = {}
        for k in ("margin_usd", "leverage", "notional_usd", "qty", "entry_price"):
            f = _finite(pos.get(k))
            if f is None or f <= 0:
                return None
            nums[k] = f
        side, entry = pos["side"], nums["entry_price"]
        nums["leverage"] = min(nums["leverage"], self.cfg.max_leverage)
        stop = _finite(pos.get("stop_price"))
        liq = _finite(pos.get("liq_price"))
        return {
            "symbol": pos.get("symbol"),
            "side": side,
            **nums,
            # Mandatory stop / liq: recompute if missing or corrupt.
            "stop_price": stop if stop and stop > 0 else stop_price_for(side, entry, self.cfg),
            "liq_price": liq if liq and liq > 0 else liq_price_for(side, entry, nums["leverage"]),
            "entry_ts_ms": int(_finite(pos.get("entry_ts_ms")) or 0),
            "funding_rate_at_entry": _finite(pos.get("funding_rate_at_entry")),
        }

    def load(self) -> bool:
        """Load state from disk. Returns False (keeps current state) if unusable."""
        try:
            data = json.loads(Path(self.state_path).read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        eq = _finite(data.get("equity"))
        if eq is not None and eq >= 0:
            self.equity = eq
        self.position = self._load_position(data.get("position"))
        le = _finite(data.get("last_entry_ts_ms"))
        self.last_entry_ts_ms = int(le) if le is not None else None
        self.day = str(data.get("day") or _utc_today())
        dse = _finite(data.get("day_start_equity"))
        if dse is not None and dse > 0:
            self.day_start_equity = dse
        return True


# ---------------------------------------------------------------------------
# decision layer (pure, never raises)
# ---------------------------------------------------------------------------

def _res(action: str, reason: str, size_fraction: float = 0.0, leverage: float = 0.0,
         vetoed_by: Optional[str] = None) -> dict:
    return {"action": action, "reason": reason, "size_fraction": float(size_fraction),
            "leverage": float(leverage), "vetoed_by": vetoed_by}


def _skip(reason: str, vetoed_by: Optional[str]) -> dict:
    return _res("skip", reason, vetoed_by=vetoed_by)


def decide_perps(verdict: dict, pf: PortfolioState, cfg: PerpsConfig, now_ms: int,
                 funding_rate: Optional[float] = None) -> dict:
    """Map a Jev verdict + perps book state to enter_long/enter_short/exit/skip. Never raises."""
    try:
        return _decide_perps(verdict, pf, cfg, now_ms, funding_rate)
    except Exception as exc:  # defensive: never raise into the trading loop
        return _skip(f"malformed: {type(exc).__name__}: {exc}", "malformed")


def _decide_perps(verdict, pf, cfg: PerpsConfig, now_ms, funding_rate) -> dict:
    # (1) fail-open validation — same contract as jev_gates.decide
    if not isinstance(verdict, dict):
        return _skip("malformed: verdict is not a dict", "malformed")
    if verdict.get("ok") is not True or verdict.get("confidence") is None:
        err = verdict.get("error")
        return _skip(f"no verdict ({err})" if err else "no verdict", "no_verdict")
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
    pump, dump = vals["pump_0_100"], vals["dump_0_100"]
    exh, whip, conf = vals["exhaustion_prob"], vals["whipsaw_prob"], vals["confidence"]

    # (2)+(9) position held: exit or hold — never open the opposite side this cycle
    if pf.has_position:
        side = getattr(pf, "side", None)
        if side == "long":
            if dump >= cfg.exit_min_dump:
                return _res("exit", f"long exit: dump {dump} >= {cfg.exit_min_dump}")
            if exh >= cfg.exit_exhaustion:
                return _res("exit", f"long exit: exhaustion {exh} >= {cfg.exit_exhaustion}")
        elif side == "short":
            if pump >= cfg.entry_min_pump:
                return _res("exit", f"short exit: pump {pump} >= {cfg.entry_min_pump}")
            if exh >= cfg.exit_exhaustion:
                return _res("exit", f"short exit: exhaustion {exh} >= {cfg.exit_exhaustion}")
        else:
            return _skip(f"malformed: held position side {side!r}", "malformed")
        return _skip("hold", None)

    # (3) daily loss kill blocks entries on both sides
    if pf.daily_pnl_pct <= -cfg.daily_loss_limit_pct:
        return _skip(
            f"daily_loss_kill: daily pnl {pf.daily_pnl_pct}% <= -{cfg.daily_loss_limit_pct}%",
            "daily_loss_kill")

    # (5) shared entry gates
    if whip > cfg.entry_max_whipsaw:
        return _skip(f"high_whipsaw: {whip} > {cfg.entry_max_whipsaw}", "high_whipsaw")
    if exh > cfg.entry_max_exhaustion:
        return _skip(f"high_exhaustion: {exh} > {cfg.entry_max_exhaustion}", "high_exhaustion")
    if conf < cfg.min_confidence:
        return _skip(f"low_confidence: {conf} < {cfg.min_confidence}", "low_confidence")
    if pf.last_entry_ts_ms is not None:
        elapsed = now_ms - pf.last_entry_ts_ms
        if elapsed < cfg.cooldown_seconds * 1000:
            return _skip(
                f"cooldown: {elapsed}ms since last entry < {cfg.cooldown_seconds * 1000}ms",
                "cooldown")

    # (4)(6)(7) side-specific gates; funding None/non-finite => fail-open (no veto)
    fr = _as_float(funding_rate)
    thr = cfg.max_abs_funding_pct / 100.0  # 0.01% per 8h -> 0.0001
    if pump < cfg.entry_min_pump:
        long_veto = ("low_pump", f"low_pump: {pump} < {cfg.entry_min_pump}")
    elif phase == "capitulation":
        long_veto = ("capitulation", "capitulation: phase blocks long entries")
    elif fr is not None and fr > thr:
        long_veto = ("funding", f"funding: {fr} > {thr} blocks long")
    else:
        long_veto = None
    if dump < cfg.short_min_dump:
        short_veto = ("low_dump", f"low_dump: {dump} < {cfg.short_min_dump}")
    elif fr is not None and fr < -thr:
        short_veto = ("funding", f"funding: {fr} < {-thr} blocks short")
    else:
        short_veto = None

    # (8) pick a side; prefer long when pump >= dump
    prefer_long = pump >= dump
    if long_veto is None and (short_veto is not None or prefer_long):
        side = "long"
    elif short_veto is None:
        side = "short"
    else:
        # Both blocked: report the preferred side's veto, unless it is just a weak
        # signal and the other side hit a real veto (capitulation / funding).
        first, other = (long_veto, short_veto) if prefer_long else (short_veto, long_veto)
        weak = ("low_pump", "low_dump")
        tag, why = other if (first[0] in weak and other[0] not in weak) else first
        return _skip(why, tag)

    size = round(cfg.max_margin_fraction * conf, 4)
    return _res(
        f"enter_{side}",
        f"all gates passed ({side}): pump={pump} dump={dump} whipsaw={whip} "
        f"exhaustion={exh} confidence={conf} funding={'na' if fr is None else fr}",
        size, cfg.max_leverage, None,
    )
