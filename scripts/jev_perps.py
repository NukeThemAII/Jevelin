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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import (  # noqa: E402
    PERPS_TAKER_FEE_RATE,
    SLIPPAGE_RATE,
    PerpsConfig,
    atomic_write_json,
    bitmask_for,
)
from jev_gates import (  # noqa: E402
    NUMERIC_KEYS,
    REQUIRED_KEYS,
    PortfolioState,
    _as_counter,
    _as_float,
    sizing_tier,
    whipsaw_gate_name,
)
from jev_paper import _finite, _utc_today  # noqa: E402

FUNDING_PERIOD_MS = 8 * 60 * 60 * 1000  # 28_800_000 ms = one 8h funding period
LIQ_BUFFER = 0.95  # approx liquidation: 95% of the initial-margin move (no MM math)
SIDES = ("long", "short")


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
        self.fees_paid: float = 0.0
        self.slippage_paid: float = 0.0
        # M3 hysteresis state (persisted in the state JSON): consecutive exit-
        # signal cycles and decision cycles since entry.
        self.exit_signal_cycles: int = 0
        self.cycles_held: int = 0

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
            exit_signal_cycles=self.exit_signal_cycles,
            cycles_held=self.cycles_held,
            side=self.position["side"] if self.position else None,
        )

    # -- actions ---------------------------------------------------------

    def apply_action(self, action: dict, symbol: str, price: float, ts_ms: int,
                     funding_rate: Optional[float] = None,
                     decision_id: Optional[str] = None) -> dict:
        """Apply a decide_perps() action locally. Never raises.

        Automatic exits run first, whatever the action: (a) liquidation,
        (b) stop-loss; then (c) the explicit action. Every fill pays the taker
        fee and adverse slippage (M0 / F-P0-1); liquidation fills use the
        computed liq price as-is (no slippage).
        """
        out = {"executed": None, "qty": 0.0, "usd": 0.0, "realized_pnl": 0.0,
               "fees": 0.0, "slippage": 0.0, "funding_paid": 0.0, "detail": ""}
        try:
            p = _finite(price)
            if p is None or p <= 0:
                out["detail"] = "bad price"
                return out
            if _finite(ts_ms) is None:
                out["detail"] = "bad ts_ms"
                return out
            ts = int(ts_ms)
            did = decision_id
            if did is None and isinstance(action, dict):
                did = action.get("decision_id")
            reason = str(action.get("reason") or "") if isinstance(action, dict) else ""

            if self.position is not None:
                pos = self.position
                long_ = pos["side"] == "long"
                liq, stop = float(pos["liq_price"]), float(pos["stop_price"])
                if (long_ and p <= liq) or (not long_ and p >= liq):
                    # liquidation fills at the computed liq price, as-is
                    return self._close(symbol, liq, ts, out, "liquidated", "liquidated",
                                       did, fill_as_is=True)
                if (long_ and p <= stop) or (not long_ and p >= stop):
                    return self._close(symbol, p, ts, out, "stop_loss", "stop_loss", did)

            if not isinstance(action, dict):
                out["detail"] = "bad action"
                return out
            kind = action.get("action")
            if kind in ("enter_long", "enter_short"):
                side = "long" if kind == "enter_long" else "short"
                return self._enter(side, action, symbol, p, ts, funding_rate, out,
                                   did, reason)
            if kind == "exit":
                if self.position is None:
                    out["detail"] = "exit while flat"
                    return out
                return self._close(symbol, p, ts, out, "exited",
                                   reason or "exit", did)
            # hold / skip: adopt the gate layer's updated hysteresis counters
            self._adopt_signal_state(action)
            out["detail"] = f"no-op action={kind!r}"
            return out
        except Exception as exc:  # defensive: never raise into the loop
            out["detail"] = f"error: {type(exc).__name__}: {exc}"
            return out

    def _adopt_signal_state(self, action) -> None:
        """Persist decide_perps' hysteresis counters (M3): never trust junk."""
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

    def _enter(self, side, action, symbol, price, ts_ms, funding_rate, out,
               decision_id=None, reason=""):
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
        # Adverse slippage by trade direction: long entry is a buy (fills UP),
        # short entry is a sell (fills DOWN).
        fill = price * (1.0 + cfg.slippage_rate) if side == "long" \
            else price * (1.0 - cfg.slippage_rate)
        qty = notional / fill
        if margin <= 0 or qty <= 0:
            out["detail"] = "non-positive size"
            return out
        fee = notional * cfg.taker_fee_rate
        slippage = abs(fill - price) * qty
        self.equity -= fee  # entry fee reduces equity immediately
        self.fees_paid += fee
        self.slippage_paid += slippage
        self.position = {
            "symbol": symbol,
            "side": side,
            "margin_usd": margin,
            "leverage": leverage,
            "notional_usd": notional,
            "qty": qty,
            "entry_price": fill,  # fill price: all PnL math uses fills
            "stop_price": stop_price_for(side, fill, cfg),
            "liq_price": liq_price_for(side, fill, leverage),
            "entry_ts_ms": ts_ms,
            "entry_fee": fee,
            "funding_rate_at_entry": _finite(funding_rate),
        }
        self.last_entry_ts_ms = ts_ms
        self._reset_signal_state()  # fresh position: hysteresis counters restart
        out.update(executed=f"enter_{side}", qty=qty, usd=notional, realized_pnl=0.0,
                   fees=fee, slippage=slippage, detail="entered")
        self._append_trade(ts_ms, symbol, side, f"enter_{side}", fill, qty, notional,
                           leverage, 0.0, 0.0, fee, slippage, decision_id,
                           reason or "entered", size_tier=action.get("size_tier"))
        return out

    def _close(self, symbol, price, ts_ms, out, detail, reason, decision_id=None,
               fill_as_is=False):
        pos = self.position
        side, qty = pos["side"], float(pos["qty"])
        margin = float(pos["margin_usd"])
        entry_fee = _finite(pos.get("entry_fee")) or 0.0
        # Adverse slippage on exit fills; liquidation fills use the given liq
        # price as-is (fill_as_is=True).
        if fill_as_is:
            fill = price
        else:
            fill = price * (1.0 - self.cfg.slippage_rate) if side == "long" \
                else price * (1.0 + self.cfg.slippage_rate)
        slippage = abs(fill - price) * qty
        fee = abs(qty * fill) * self.cfg.taker_fee_rate
        if detail == "liquidated":
            gross = -margin  # whole margin lost
        else:
            gross = unrealized_pnl(side, float(pos["entry_price"]), fill, qty)
            gross = max(gross, -margin)  # isolated margin floor
        funding = funding_paid_for(pos, ts_ms)
        # Realized PnL includes BOTH fill fees (M0 / F-P0-1); funding stays separate.
        realized = gross - entry_fee - fee
        self.equity += gross - fee - funding
        self.fees_paid += fee
        self.slippage_paid += slippage
        self.position = None
        self._reset_signal_state()  # closed: hysteresis counters restart
        out.update(executed="exit", qty=qty, usd=float(pos["notional_usd"]),
                   realized_pnl=realized, fees=fee, slippage=slippage,
                   funding_paid=funding, detail=detail)
        self._append_trade(ts_ms, symbol or pos.get("symbol"), side, detail, fill, qty,
                           float(pos["notional_usd"]), float(pos["leverage"]), realized,
                           funding, fee, slippage, decision_id, reason)
        return out

    def _append_trade(self, ts_ms, symbol, side, action, price, qty, notional, leverage,
                      realized_pnl, funding_paid, fees, slippage, decision_id, reason,
                      size_tier=None):
        line = {
            "ts_ms": int(ts_ms),
            "decision_id": decision_id,
            "book": "perps",
            "symbol": symbol,
            "side": side,
            "action": action,
            "price": float(price),
            "qty": float(qty),
            "notional": float(notional),
            "leverage": float(leverage),
            "realized_pnl": float(realized_pnl),
            "funding_paid": float(funding_paid),
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
            "equity": self.equity,
            "position": self.position,
            "last_entry_ts_ms": self.last_entry_ts_ms,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "fees_paid": self.fees_paid,
            "slippage_paid": self.slippage_paid,
            "exit_signal_cycles": self.exit_signal_cycles,
            "cycles_held": self.cycles_held,
        }

    def save(self) -> None:
        """Atomic write (tmp in the same dir + os.replace); see jev_config."""
        atomic_write_json(self.state_path, self._state_dict())

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
            "entry_fee": _finite(pos.get("entry_fee")) or 0.0,
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
        fees = _finite(data.get("fees_paid"))
        self.fees_paid = fees if fees is not None and fees >= 0 else 0.0
        slip = _finite(data.get("slippage_paid"))
        self.slippage_paid = slip if slip is not None and slip >= 0 else 0.0
        esc = _finite(data.get("exit_signal_cycles"))
        self.exit_signal_cycles = int(esc) if esc is not None and esc >= 0 else 0
        ch = _finite(data.get("cycles_held"))
        self.cycles_held = int(ch) if ch is not None and ch >= 0 else 0
        return True


# ---------------------------------------------------------------------------
# decision layer (pure, never raises)
# ---------------------------------------------------------------------------

def _res(action: str, reason: str, size_fraction: float = 0.0, leverage: float = 0.0,
         failed: Optional[list] = None, decision_id: Optional[str] = None,
         size_tier: Optional[int] = None, exit_signal_cycles: int = 0,
         cycles_held: int = 0) -> dict:
    """Decision record: vetoed_by lists EVERY failing gate (F-P1-4)."""
    failed = list(failed or [])
    return {"action": action, "reason": reason, "size_fraction": float(size_fraction),
            "size_tier": size_tier, "leverage": float(leverage), "vetoed_by": failed,
            "veto_bitmask": bitmask_for(failed), "decision_id": decision_id,
            "exit_signal_cycles": int(exit_signal_cycles),
            "cycles_held": int(cycles_held)}


def _skip(reason: str, failed: list, decision_id: Optional[str] = None,
          exit_signal_cycles: int = 0, cycles_held: int = 0) -> dict:
    return _res("skip", reason, failed=failed, decision_id=decision_id,
                exit_signal_cycles=exit_signal_cycles, cycles_held=cycles_held)


def decide_perps(verdict: dict, pf: PortfolioState, cfg: PerpsConfig, now_ms: int,
                 funding_rate: Optional[float] = None,
                 decision_id: Optional[str] = None,
                 regime: Optional[str] = None) -> dict:
    """Map a Jev verdict + perps book state to enter_long/enter_short/exit/skip.

    Never raises. M3 (B.3) semantics: regime/phase/whipsaw-fanout entry gates,
    dump/pump exit hysteresis with min hold, confidence sizing tiers. See the
    module docstring for the exit rule (no separate exhaustion exit in v2).
    ``regime`` None = caller has no regime data (deprecated v1 fallback).
    """
    try:
        did = decision_id
        if did is None and isinstance(verdict, dict):
            did = verdict.get("decision_id")
        return _decide_perps(verdict, pf, cfg, now_ms, funding_rate, did, regime)
    except Exception as exc:  # defensive: never raise into the trading loop
        return _skip(f"malformed: {type(exc).__name__}: {exc}", ["malformed"], decision_id)


def _decide_perps(verdict, pf, cfg: PerpsConfig, now_ms, funding_rate,
                  decision_id=None, regime=None) -> dict:
    # (1) fail-open validation — same contract as jev_gates.decide
    if not isinstance(verdict, dict):
        return _skip("malformed: verdict is not a dict", ["malformed"], decision_id)
    if verdict.get("ok") is not True or verdict.get("confidence") is None:
        err = verdict.get("error")
        return _skip(f"no verdict ({err})" if err else "no verdict", ["no_verdict"],
                     decision_id)
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
    pump, dump = vals["pump_0_100"], vals["dump_0_100"]
    exh, whip, conf = vals["exhaustion_prob"], vals["whipsaw_prob"], vals["confidence"]

    # fan-out sample (M3): present-but-unusable counts as a tie (fail-closed)
    w2_raw = verdict.get("whipsaw_prob_2")
    w2 = _as_float(w2_raw) if w2_raw is not None else None
    fan_out = bool(verdict.get("fan_out")) or w2 is not None

    # (2)+(9) position held: exit or hold — never open the opposite side this
    # cycle. Hysteresis (B.3) mirrors the spot book: long exits on dump, short
    # exits on pump (>= bar x exit_consecutive_cycles OR one cycle >= hard bar);
    # min hold applies to signal exits, stops/liq always fire in the portfolio.
    if pf.has_position:
        side = getattr(pf, "side", None)
        if side not in SIDES:
            return _skip(f"malformed: held position side {side!r}", ["malformed"],
                         decision_id)
        signal_val = dump if side == "long" else pump
        min_bar = cfg.exit_min_dump if side == "long" else cfg.exit_min_pump
        hard_bar = cfg.exit_hard_dump if side == "long" else cfg.exit_hard_pump
        age_now = _as_counter(pf.cycles_held) + 1
        signal = signal_val >= min_bar
        streak = _as_counter(pf.exit_signal_cycles) + 1 if signal else 0
        if age_now >= cfg.min_hold_cycles:
            if signal_val >= hard_bar:
                return _res("exit", f"{side} exit: {signal_val} >= {hard_bar} "
                            f"(single-tick exit bar)", decision_id=decision_id,
                            exit_signal_cycles=streak, cycles_held=age_now)
            if streak >= cfg.exit_consecutive_cycles:
                return _res("exit", f"{side} exit: {signal_val} >= {min_bar} for "
                            f"{streak} consecutive cycles", decision_id=decision_id,
                            exit_signal_cycles=streak, cycles_held=age_now)
        return _skip("hold", [], decision_id, streak, age_now)

    # ENTRY path — evaluate ALL gates (F-P1-4): shared, long-side, short-side.
    # Every failing gate is recorded; ``reason`` stays the first in check order.
    shared = []  # (gate name, readable reason) in check order
    if pf.daily_pnl_pct <= -cfg.daily_loss_limit_pct:
        shared.append(("daily_loss_kill",
                       f"daily_loss_kill: daily pnl {pf.daily_pnl_pct}% <= "
                       f"-{cfg.daily_loss_limit_pct}%"))
    if regime == "chop":
        shared.append(("regime_chop", "regime_chop: chop forbids all entries"))
    if phase == "capitulation":
        shared.append(("capitulation", "capitulation: phase blocks entries"))
    if phase not in cfg.entry_phases:
        shared.append(("phase_not_in_entry_set",
                       f"phase_not_in_entry_set: phase {phase} not in "
                       f"{list(cfg.entry_phases)}"))
    whip_gate = whipsaw_gate_name(whip, w2, fan_out, cfg.entry_max_whipsaw)
    if whip_gate == "high_whipsaw":
        shared.append(("high_whipsaw",
                       f"high_whipsaw: both fan-out samples > {cfg.entry_max_whipsaw}"
                       if fan_out else f"high_whipsaw: {whip} > {cfg.entry_max_whipsaw}"))
    elif whip_gate == "whipsaw_fanout_tie":
        shared.append(("whipsaw_fanout_tie",
                       f"whipsaw_fanout_tie: fan-out samples disagree or 2nd "
                       f"sample unusable ({whip} vs {w2})"))
    if exh > cfg.entry_max_exhaustion:
        shared.append(("high_exhaustion",
                       f"high_exhaustion: {exh} > {cfg.entry_max_exhaustion}"))
    if conf < cfg.min_confidence:
        shared.append(("low_confidence", f"low_confidence: {conf} < {cfg.min_confidence}"))
    if pf.last_entry_ts_ms is not None:
        elapsed = now_ms - pf.last_entry_ts_ms
        if elapsed < cfg.cooldown_seconds * 1000:
            shared.append(("cooldown",
                           f"cooldown: {elapsed}ms since last entry < "
                           f"{cfg.cooldown_seconds * 1000}ms"))

    # side-specific gates (ALL failing recorded); funding None/non-finite =>
    # fail-open (no veto)
    fr = _as_float(funding_rate)
    thr = cfg.max_abs_funding_pct / 100.0  # 0.01% per 8h -> 0.0001
    long_failed = []
    if regime == "trend_down" and cfg.counter_trend == "block":
        long_failed.append(("regime_counter",
                            "regime_counter: trend_down blocks long entries"))
    if pump < cfg.entry_min_pump:
        long_failed.append(("low_pump", f"low_pump: {pump} < {cfg.entry_min_pump}"))
    if fr is not None and fr > thr:
        long_failed.append(("funding", f"funding: {fr} > {thr} blocks long"))
    short_failed = []
    if regime == "trend_up" and cfg.counter_trend == "block":
        short_failed.append(("regime_counter",
                             "regime_counter: trend_up blocks short entries"))
    if dump < cfg.short_min_dump:
        short_failed.append(("low_dump", f"low_dump: {dump} < {cfg.short_min_dump}"))
    if fr is not None and fr < -thr:
        short_failed.append(("funding", f"funding: {fr} < {-thr} blocks short"))

    # all failing gates in evaluation order (shared -> long -> short), deduped
    failed = []
    for item in shared + long_failed + short_failed:
        if item[0] not in [name for name, _ in failed]:
            failed.append(item)

    if shared:  # a shared gate blocks entries on both sides
        return _skip(shared[0][1], [name for name, _ in failed], decision_id)

    # pick a side; prefer long when pump >= dump
    prefer_long = pump >= dump
    long_ok, short_ok = not long_failed, not short_failed
    if long_ok and (prefer_long or not short_ok):
        side = "long"
    elif short_ok:
        side = "short"
    else:
        # Both blocked: report the preferred side's veto, unless it is just a weak
        # signal and the other side hit a real veto (regime / funding).
        first, other = ((long_failed, short_failed) if prefer_long
                        else (short_failed, long_failed))
        weak = ("low_pump", "low_dump")
        why = (other[0][1] if (first[0][0] in weak and other[0][0] not in weak)
               else first[0][1])
        return _skip(why, [name for name, _ in failed], decision_id)

    frac, tier = sizing_tier(conf, cfg)
    size = round(cfg.max_margin_fraction * frac, 4)
    return _res(
        f"enter_{side}",
        f"all gates passed ({side}): pump={pump} dump={dump} whipsaw={whip} "
        f"exhaustion={exh} confidence={conf} tier={tier}% "
        f"funding={'na' if fr is None else fr}",
        size, cfg.max_leverage, [], decision_id, size_tier=tier,
    )
