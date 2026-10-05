#!/usr/bin/env python3
"""jev_base — price-only base strategy with Jev as veto-only. Stdlib only.

Option (b), ruled 2026-10-04: the base strategy decides every entry and exit
from OHLCV alone; Jev can only BLOCK an entry the base strategy already wants
(``jev_veto``). Jev never creates a trade, never sizes one, never exits one.

Bars are ``[ts, open, high, low, close]`` (closed bars only). The caller
evaluates ``entry_signal`` at the close of bar i and fills at the next price
(the open of bar i+1 in the backtest, the next cycle price live).

Entry families (``BaseConfig.entry``), both volatility-scaled:
  * ``donchian`` — close beyond the prior ``entry_period``-bar channel by
    ``entry_buffer_atr`` x the prior bar's ATR (both exclude the current bar);
  * ``tsmom`` — drift t-stat of the last ``entry_period`` log returns
    (mean / stdev x sqrt(n)) crossing +-``entry_z``.

Trend filter (``trend``): ``none``; ``ema`` (close and fast EMA on the trade
side of the slow EMA); ``regime`` (jev_regime label: longs need trend_up,
shorts trend_down; chop / missing blocks — fail closed).

Exits, all in ATR units: initial stop ``stop_atr`` x ATR from the fill (a
resting stop, gap-aware: a gap through it fills at the open); chandelier trail
``trail_atr`` x ATR from the extreme since entry (ratchets, never loosens;
0 = off); optional opposite ``exit_period`` channel and ``max_hold_bars`` time
stop, both acted on at the next price. Per-regime exits (grid g3): with
``trail_atr_off`` > 0 the trail uses that multiple on any bar whose regime is
not the trade's trend (trend_up for longs, trend_down for shorts; missing ->
off, fail closed); 0 = the trail ignores regime.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

ENTRIES = ("donchian", "tsmom")
TRENDS = ("none", "ema", "regime")
SIDES = ("long", "short")


@dataclass(frozen=True)
class BaseConfig:
    timeframe: str = "1h"            # signal bars
    entry: str = "donchian"          # donchian | tsmom
    entry_period: int = 20           # channel / momentum lookback (bars)
    entry_buffer_atr: float = 0.0    # donchian: clear the channel by k x ATR
    entry_z: float = 2.0             # tsmom: drift t-stat threshold
    atr_period: int = 14             # Wilder ATR
    stop_atr: float = 2.0            # initial stop distance (ATR)
    trail_atr: float = 3.0           # chandelier trail (ATR); 0 = off
    trail_atr_off: float = 0.0       # trail on off-regime bars (ATR); 0 = trail_atr
    exit_period: int = 0             # opposite-channel exit lookback; 0 = off
    max_hold_bars: int = 0           # time stop; 0 = off
    trend: str = "none"              # none | ema | regime
    trend_fast: int = 50
    trend_slow: int = 200
    sides: tuple = SIDES

    def __post_init__(self):
        if self.entry not in ENTRIES:
            raise ValueError(f"entry must be one of {ENTRIES}: {self.entry!r}")
        if self.trend not in TRENDS:
            raise ValueError(f"trend must be one of {TRENDS}: {self.trend!r}")
        if not self.sides or any(s not in SIDES for s in self.sides):
            raise ValueError(f"sides must be a non-empty subset of {SIDES}")
        for name in ("entry_period", "atr_period", "trend_fast", "trend_slow"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be >= 1")
        for name in ("exit_period", "max_hold_bars"):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.stop_atr <= 0:
            raise ValueError("stop_atr must be > 0")
        if self.trail_atr < 0 or self.trail_atr_off < 0 or self.entry_buffer_atr < 0 \
                or self.entry_z <= 0:
            raise ValueError("trail_atr/trail_atr_off/entry_buffer_atr must be >= 0, "
                             "entry_z > 0")


@dataclass(frozen=True)
class VetoConfig:
    """Jev veto bars: a value AT or ABOVE a bar vetoes the entry.

    Pre-registered 2026-10-04 from base rates of the 09-27..09-30 paper run
    (no forward-return data used): long dump>=65 blocks 11.7% of cycles,
    exhaustion>=0.55 10.8%, whipsaw>=0.65 5.2%, short pump>=65 3.1%,
    capitulation 7.8%. Whipsaw is upside-phrased ("is this breakout a
    fakeout?"), so it vetoes longs only. ``fail_closed``: an unavailable or
    malformed verdict vetoes (project doctrine: no verdict -> no entry).
    """
    long_veto_dump: float = 65.0
    short_veto_pump: float = 65.0
    veto_exhaustion: float = 0.55
    long_veto_whipsaw: float = 0.65
    veto_phases: tuple = ("capitulation",)
    fail_closed: bool = True


# ---------------------------------------------------------------------------
# indicators (lists aligned to bars; None until warm)
# ---------------------------------------------------------------------------

def wilder_atr(highs, lows, closes, period) -> list:
    n = len(closes)
    out = [None] * n
    if n < period:
        return out
    trs = []
    for i in range(n):
        h, lo = highs[i], lows[i]
        if i == 0:
            trs.append(h - lo)
        else:
            pc = closes[i - 1]
            trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    atr = sum(trs[:period]) / period
    out[period - 1] = atr
    for i in range(period, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def ema_series(values, period) -> list:
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    k = 2.0 / (period + 1.0)
    for i in range(period, n):
        ema = values[i] * k + ema * (1.0 - k)
        out[i] = ema
    return out


def donchian_prior(highs, lows, period) -> tuple:
    """(upper, lower): max high / min low of the ``period`` bars BEFORE t."""
    n = len(highs)
    upper, lower = [None] * n, [None] * n
    for t in range(period, n):
        upper[t] = max(highs[t - period:t])
        lower[t] = min(lows[t - period:t])
    return upper, lower


def momentum_z(closes, period) -> list:
    """Drift t-stat of the last ``period`` log returns; None if flat/warmup."""
    n = len(closes)
    out = [None] * n
    if period < 2:
        return out
    rets = [None] + [math.log(closes[i] / closes[i - 1]) for i in range(1, n)]
    for i in range(period, n):
        window = rets[i - period + 1:i + 1]
        sd = statistics.stdev(window)
        if sd > 0:
            out[i] = statistics.fmean(window) / sd * math.sqrt(period)
    return out


def indicators(bars, cfg: BaseConfig) -> dict:
    ts = [int(b[0]) for b in bars]
    o = [float(b[1]) for b in bars]
    h = [float(b[2]) for b in bars]
    lo = [float(b[3]) for b in bars]
    c = [float(b[4]) for b in bars]
    upper, lower = donchian_prior(h, lo, cfg.entry_period)
    if cfg.exit_period:
        exit_upper, exit_lower = donchian_prior(h, lo, cfg.exit_period)
    else:
        exit_upper = exit_lower = [None] * len(bars)
    return {
        "ts": ts, "open": o, "high": h, "low": lo, "close": c,
        "atr": wilder_atr(h, lo, c, cfg.atr_period),
        "upper": upper, "lower": lower,
        "exit_upper": exit_upper, "exit_lower": exit_lower,
        "ema_fast": ema_series(c, cfg.trend_fast),
        "ema_slow": ema_series(c, cfg.trend_slow),
        "z": momentum_z(c, cfg.entry_period) if cfg.entry == "tsmom" else [None] * len(c),
    }


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def trend_ok(side, ind, i, cfg, regime) -> bool:
    """Trend filter for ``side`` at bar i (``cfg.trend``); fails closed."""
    if cfg.trend == "none":
        return True
    if cfg.trend == "regime":
        return regime == ("trend_up" if side == "long" else "trend_down")
    fast, slow, close = ind["ema_fast"][i], ind["ema_slow"][i], ind["close"][i]
    if fast is None or slow is None:
        return False
    if side == "long":
        return close > slow and fast > slow
    return close < slow and fast < slow


def entry_signal(ind, i, cfg: BaseConfig, regime: Optional[str] = None) -> Optional[str]:
    """"long" | "short" | None at the close of bar i."""
    atr = ind["atr"][i]
    if atr is None:
        return None
    close = ind["close"][i]
    side = None
    if cfg.entry == "donchian":
        up, lo = ind["upper"][i], ind["lower"][i]
        prior_atr = ind["atr"][i - 1] if i > 0 else None
        if up is None or prior_atr is None:
            return None
        # Like the channel, the buffer is fixed BEFORE bar i: the breakout
        # bar's own range must not widen the bar it is judged against.
        buf = cfg.entry_buffer_atr * prior_atr
        if close > up + buf:
            side = "long"
        elif close < lo - buf:
            side = "short"
    else:
        z = ind["z"][i]
        if z is None:
            return None
        zp = ind["z"][i - 1] if i > 0 else None
        if z >= cfg.entry_z and (zp is None or zp < cfg.entry_z):
            side = "long"
        elif z <= -cfg.entry_z and (zp is None or zp > -cfg.entry_z):
            side = "short"
    if side is None or side not in cfg.sides:
        return None
    return side if trend_ok(side, ind, i, cfg, regime) else None


# ---------------------------------------------------------------------------
# position management
# ---------------------------------------------------------------------------

@dataclass
class Position:
    side: str
    entry_ts: int
    entry_fill: float
    stop: float
    extreme: float
    init_risk: float
    bars_held: int = 0
    meta: dict = field(default_factory=dict)


def open_position(side, fill, ts, atr, cfg: BaseConfig) -> Position:
    risk = cfg.stop_atr * atr
    stop = fill - risk if side == "long" else fill + risk
    return Position(side, int(ts), float(fill), stop, float(fill), risk)


def stop_exit(pos: Position, o, h, lo) -> Optional[tuple]:
    """Resting stop inside one bar -> (fill reference, "stop") or None.
    A gap through the stop fills at the open, a touch at the stop."""
    if pos.side == "long":
        if o <= pos.stop:
            return o, "stop"
        if lo <= pos.stop:
            return pos.stop, "stop"
    else:
        if o >= pos.stop:
            return o, "stop"
        if h >= pos.stop:
            return pos.stop, "stop"
    return None


def after_close(pos: Position, ind, j, cfg: BaseConfig,
                regime: Optional[str] = None) -> Optional[str]:
    """Bar j closed with the position still open: ratchet the trail (after the
    intrabar stop check, so a bar never raises its own stop), then return a
    signal exit ("channel" | "time") to act on at the next price, or None.
    ``regime`` is bar j's label; it only matters when ``trail_atr_off`` > 0."""
    pos.bars_held += 1
    atr = ind["atr"][j]
    long_ = pos.side == "long"
    trail = cfg.trail_atr
    if cfg.trail_atr_off > 0 and regime != ("trend_up" if long_ else "trend_down"):
        trail = cfg.trail_atr_off
    if long_:
        pos.extreme = max(pos.extreme, ind["high"][j])
        if trail > 0 and atr is not None:
            pos.stop = max(pos.stop, pos.extreme - trail * atr)
    else:
        pos.extreme = min(pos.extreme, ind["low"][j])
        if trail > 0 and atr is not None:
            pos.stop = min(pos.stop, pos.extreme + trail * atr)
    if cfg.exit_period:
        close = ind["close"][j]
        if long_ and ind["exit_lower"][j] is not None and close < ind["exit_lower"][j]:
            return "channel"
        if not long_ and ind["exit_upper"][j] is not None and close > ind["exit_upper"][j]:
            return "channel"
    if cfg.max_hold_bars and pos.bars_held >= cfg.max_hold_bars:
        return "time"
    return None


# ---------------------------------------------------------------------------
# Jev veto (veto-only: can block an entry, never create one)
# ---------------------------------------------------------------------------

def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def jev_veto(side, verdict, vcfg: VetoConfig) -> list:
    """Veto reasons for a base-strategy entry on ``side``; [] = allowed."""
    if not isinstance(verdict, dict) or verdict.get("ok") is not True:
        return ["jev_unavailable"] if vcfg.fail_closed else []
    long_ = side == "long"
    signal = _num(verdict.get("dump_0_100" if long_ else "pump_0_100"))
    exhaustion = _num(verdict.get("exhaustion_prob"))
    whipsaw = _num(verdict.get("whipsaw_prob")) if long_ else 0.0
    phase = verdict.get("phase")
    if signal is None or exhaustion is None or whipsaw is None \
            or not isinstance(phase, str):
        return ["jev_malformed"] if vcfg.fail_closed else []
    reasons = []
    if long_ and signal >= vcfg.long_veto_dump:
        reasons.append("jev_dump")
    if not long_ and signal >= vcfg.short_veto_pump:
        reasons.append("jev_pump")
    if exhaustion >= vcfg.veto_exhaustion:
        reasons.append("jev_exhaustion")
    if long_ and whipsaw >= vcfg.long_veto_whipsaw:
        reasons.append("jev_whipsaw")
    if phase in vcfg.veto_phases:
        reasons.append("jev_phase")
    return reasons
