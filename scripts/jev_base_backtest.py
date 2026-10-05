#!/usr/bin/env python3
"""jev_base_backtest — bar-by-bar backtest of the jev_base strategy. Stdlib only.

History: public keyless Binance 15m spot klines, fetched one calendar month
(UTC) at a time; CLOSED months are cached as JSON under ``--cache`` (default
``runtime/klines_15m``), the open month is refetched and its open bar dropped.
Signal bars are resampled from the 15m bars (default 1h).

Regime: per signal bar, ``jev_regime.classify`` on the last 60 x 15m and 120 x
1h bars CLOSED at that bar's close — the live supervisor's windows
(market.ohlcv_15m_limit / ohlcv_1h_limit), minus its still-open last bar.
Warmup -> None, which fails the ``regime`` trend filter closed.

Simulation (one position per symbol): a signal at bar i's close fills at bar
i+1's open; the resting ATR stop is checked inside every bar including the
entry bar (gap-aware); the trail ratchets after the close; channel / time
exits fill at the next open. Fee per side on notional, slippage adverse on
every fill. A position still open on the last bar is marked at its close
without exit costs (path "open"). Unit-notional, non-compounded returns.

Fit boundary: nothing at or after FIT_CUTOFF_MS (2026-09-17 00:00 +07) may be
used to fit or calibrate — the CLI refuses an ``--end`` past it.

The random-entry control keeps the exits and swaps the entries for seeded
coin flips at the strategy's own entry rate and long share: it measures
whether the ENTRIES add anything beyond the exit structure.

Grids are pre-declared (``GRIDS``), each with a default signal timeframe (a
config may name its own), seed count and pass gate (``gate_failures``), and
run once on the fit window. Perps funding is a report-only stress
(``funded_pcts``), not part of the gate.

Jev is not replayed here: no verdicts exist before 2026-09-27, so the veto
(jev_base.jev_veto) is a forward-paper concern only.

Routed grids (g3): a config may name a ``router`` whose labels feed its
entry filter and per-regime trail (``ROUTERS``: live, r1, clf); each routed
config is also judged against the matched and shift nulls (``NULLS``), and
``--prescreen`` prints return-blind label/signal counts without simulating a
trade. ``--synthetic`` swaps the market for a seeded random walk (temp cache,
no network) to dry-run the harness end to end.

Usage: .venv/bin/python scripts/jev_base_backtest.py [--grid g1|g2|g3]
       [--symbols BTCUSDT,...] [--start 2024-01-01] [--end 2026-09-17]
       [--book perps|spot] [--seeds N] [--cache runtime/klines_15m]
       [--out report.md] [--prescreen] [--synthetic]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import tempfile
import time
import zlib
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_router as jr  # noqa: E402
from jev_base import (BaseConfig, after_close, entry_signal,  # noqa: E402
                      indicators, open_position, stop_exit, wilder_atr)
from jev_calibrate import _fmt_pf, _ts_bkk, profit_factor, since_ms  # noqa: E402
from jev_config import RegimeConfig, load_config  # noqa: E402
from jev_forward import ROOT, fetch_klines  # noqa: E402
from jev_regime import classify, classify_features  # noqa: E402

M15 = 15 * 60_000
H = 3_600_000
TF_MS = {"15m": M15, "1h": H, "4h": 4 * H}
FIT_CUTOFF_MS = 1_789_578_000_000      # 2026-09-17 00:00 +07 (Jev contamination boundary)
REGIME_15M_BARS = 60                   # = market.ohlcv_15m_limit
REGIME_1H_BARS = 120                   # = market.ohlcv_1h_limit
WARMUP_SIGNAL_BARS = 250               # > trend_slow (200) and every lookback
# Routers (grid g3): "live" = jev_regime on the live windows (60 x 15m + 120 x
# 1h); "r1" = the same classifier one timeframe up (60 x 1h + 120 x 4h);
# "clf" = jev_router's walk-forward logistic, exposure-matched to r1.
ROUTERS = ("live", "r1", "clf")
# Nulls: "uncond" = random entries on any flat bar; "matched" = only on bars
# the config's router labels with-trend (CH-1); "shift" = the strategy's own
# entries with its exit labels circularly shifted (router timing).
NULLS = ("uncond", "matched", "shift")
SHIFT_MIN_BARS = 42                    # one week of 4h bars, either way round
SYN_ORIGIN_MS = 1_609_459_200_000      # 2021-01-01 00:00 UTC, synthetic walk origin

# Pre-declared grid (2026-10-04), not searched: two entry families x trend
# filters, default 2 ATR stop / 3 ATR chandelier trail.
GRID = (
    ("dc20", {}),
    ("dc55", {"entry_period": 55}),
    ("dc20-ema", {"trend": "ema"}),
    ("dc20-regime", {"trend": "regime"}),
    ("ts48", {"entry": "tsmom", "entry_period": 48}),
    ("ts48-ema", {"entry": "tsmom", "entry_period": 48, "trend": "ema"}),
)

# Pre-declared grid 2 (2026-10-04), committed before any run on the fit
# window and amended once, still before any run, on Oracle's design check.
# Hypothesis: grid 1's best gross capture (+0.089%/trade) lost to the ~0.20%
# perps round trip; 4h bars and wider exits cut the trade count and raise the
# move per trade, so the same cost is a smaller share of 1R. tsmom and the
# regime filter are dropped (tsmom lost before costs; regime = no filter).
# dc20-4h keeps grid 1's 2/3 ATR exits (timeframe lever alone); dc20-1h-w
# keeps grid 1's 1h bars (exit lever alone); dc20-4h-w has both.
GRID2 = (
    ("dc20-4h", {}),
    ("dc20-4h-w", {"stop_atr": 3.0, "trail_atr": 5.0}),
    ("dc20-4h-ema-w", {"trend": "ema", "stop_atr": 3.0, "trail_atr": 5.0}),
    ("dc55-4h-w", {"entry_period": 55, "stop_atr": 3.0, "trail_atr": 5.0}),
    # Turtle System 2 exits: 2 ATR stop, opposite 20-bar channel, no trail.
    ("dc55-x20-4h", {"entry_period": 55, "exit_period": 20, "trail_atr": 0.0}),
    ("dc20-1h-w", {"timeframe": "1h", "stop_atr": 3.0, "trail_atr": 5.0}),
)

# Pre-declared grid 3 (DRAFT 2026-10-05, docs/reports/2026-10-05-g3-prereg.md):
# regime-routed exits on a fresh venue (spot, long-only). c1 = grid 2's best
# shape (dc20-4h-w) at spot costs; c2 gates entries on the r1 router; c3 also
# routes the trail (5 ATR on trend_up bars, grid 2's 3 ATR default off them);
# c4 = c3 with r1 swapped for the local classifier. c4 vs c3 is the entire AI
# increment.
_WIDE = {"stop_atr": 3.0, "trail_atr": 5.0}
GRID3 = (
    ("c1-base", dict(_WIDE)),
    ("c2-r1-gate", {**_WIDE, "trend": "regime", "router": "r1"}),
    ("c3-r1-trail", {**_WIDE, "trend": "regime", "trail_atr_off": 3.0, "router": "r1"}),
    ("c4-clf-trail", {**_WIDE, "trend": "regime", "trail_atr_off": 3.0, "router": "clf"}),
)

# Report-only funding stress (perps): longs pay this per 8h held, shorts are
# credited nothing. 0.01% is the exchange baseline; 0.03% a rally-ish level.
FUNDING_8H = (0.0001, 0.0003)


@dataclass(frozen=True)
class Grid:
    """A pre-declared grid and the gate a config must clear to be a
    forward-paper candidate (see ``gate_failures``). ``timeframe`` is the
    default; a config's ``timeframe`` override wins."""
    timeframe: str
    configs: tuple
    seeds: int
    pass_pctile: float      # beats-random share of seeds required
    min_trades: int = 100
    book: str = None        # declared venue; the CLI refuses another
    routed_gate: str = "uncond"   # null gating routed configs; else = legacy grid
    exact_rate: bool = False      # control rate from the flat decision bars
    baseline: str = None    # routed configs must beat its sum% ("base")
    train_start: str = None       # clf training history start (YYYY-MM-DD, +07)

    def __post_init__(self):
        for tf in self.timeframes:
            if tf not in TF_MS:
                raise ValueError(f"timeframe must be one of {sorted(TF_MS)}: {tf!r}")
        if self.book not in (None, "perps", "spot"):
            raise ValueError(f"book must be perps or spot: {self.book!r}")
        if self.routed_gate not in NULLS:
            raise ValueError(f"routed_gate must be one of {NULLS}: {self.routed_gate!r}")
        if self.baseline is not None and self.baseline not in dict(self.configs):
            raise ValueError(f"baseline is not a config: {self.baseline!r}")
        if self.train_start is not None:
            since_ms(self.train_start)
        for name, overrides in self.configs:
            router = overrides.get("router", "live")
            if router not in ROUTERS:
                raise ValueError(f"{name}: router must be one of {ROUTERS}: {router!r}")
            if router == "clf" and self.train_start is None:
                raise ValueError(f"{name}: the clf router needs train_start")
            cfg = BaseConfig(**base_overrides(overrides))
            if not is_routed(cfg) or self.routed_gate == "uncond":
                continue
            if self.routed_gate == "matched" and cfg.trend != "regime":
                raise ValueError(f"{name}: the matched null needs routed entries")
            if self.routed_gate == "shift" and cfg.trail_atr_off <= 0:
                raise ValueError(f"{name}: the shift null needs routed exits")

    def timeframe_of(self, overrides) -> str:
        return overrides.get("timeframe", self.timeframe)

    @property
    def extended(self) -> bool:
        """Routed-grid mode (g3+): per-arm nulls, routing diagnostics."""
        return self.routed_gate != "uncond"

    @property
    def routers(self) -> tuple:
        """Routers the routed configs read, in config order."""
        out = []
        for _, overrides in self.configs:
            r = overrides.get("router", "live")
            if is_routed(BaseConfig(**base_overrides(overrides))) and r not in out:
                out.append(r)
        return tuple(out)

    @property
    def timeframes(self) -> tuple:
        """Signal timeframes the grid needs, the default first."""
        out = [self.timeframe]
        for _, overrides in self.configs:
            if self.timeframe_of(overrides) not in out:
                out.append(self.timeframe_of(overrides))
        return tuple(out)


def base_overrides(overrides) -> dict:
    """Config overrides minus the grid-level ``router`` key."""
    return {k: v for k, v in overrides.items() if k != "router"}


def is_routed(cfg: BaseConfig) -> bool:
    """Regime labels change this config's entries or exits."""
    return cfg.trend == "regime" or cfg.trail_atr_off > 0


GRIDS = {
    "g1": Grid("1h", GRID, seeds=200, pass_pctile=0.95),
    # 12 configs have now been tested on this one window: Bonferroni 0.05 / 12.
    "g2": Grid("4h", GRID2, seeds=1000, pass_pctile=1 - 0.05 / 12),
    # 16 configs tested or pre-registered on this window + these 4 = 20.
    "g3": Grid("4h", GRID3, seeds=1000, pass_pctile=1 - 0.05 / 20, book="spot",
               routed_gate="matched", exact_rate=True, baseline="c1-base",
               train_start="2022-01-01"),
}


# ---------------------------------------------------------------------------
# history: month-chunked cache of 15m klines
# ---------------------------------------------------------------------------

def _month_start(ts_ms) -> int:
    d = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc)
    return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _next_month(month_start_ms) -> int:
    d = datetime.fromtimestamp(month_start_ms / 1000.0, timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _clean(rows, lo, hi) -> list:
    """Sorted, de-duplicated rows with open_time in [lo, hi)."""
    by_ts = {int(r[0]): [int(r[0])] + [float(x) for x in r[1:5]] for r in rows}
    return [by_ts[t] for t in sorted(by_ts) if lo <= t < hi]


def load_history(symbol, start_ms, end_ms, cache_dir, fetch=None, now_ms=None,
                 pause_s=0.0) -> list:
    """Closed 15m bars [ts, o, h, l, c] with open_time in [start, end)."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cache = Path(cache_dir)
    out, m = [], _month_start(int(start_ms))
    while m < end_ms:
        nxt = _next_month(m)
        tag = datetime.fromtimestamp(m / 1000.0, timezone.utc).strftime("%Y-%m")
        path = cache / f"spot_{symbol}_15m_{tag}.json"
        if nxt <= now and path.exists():
            rows = json.loads(path.read_text())
        elif nxt <= now:
            rows = _clean(fetch_klines(symbol, m, nxt - M15, fetch=fetch,
                                       pause_s=pause_s, interval="15m"), m, nxt)
            cache.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows))
            tmp.replace(path)
        else:   # open month: never cached, open bar dropped
            hi = min(nxt, now - now % M15)
            rows = _clean(fetch_klines(symbol, m, hi - M15, fetch=fetch,
                                       pause_s=pause_s, interval="15m"), m, hi)
        out += rows
        m = nxt
    return [r for r in out if start_ms <= r[0] < end_ms]


def _step(rows, default=M15) -> int:
    diffs = [rows[k + 1][0] - rows[k][0] for k in range(len(rows) - 1)]
    pos = [d for d in diffs if d > 0]
    return min(pos) if pos else default


def resample(sub, period_ms, sub_ms=None) -> list:
    """Aggregate time-sorted bars into ``period_ms`` buckets. Interior gaps are
    kept; the last bucket is dropped unless its final sub-bar closes it."""
    if not sub:
        return []
    sub_ms = sub_ms or _step(sub)
    out = []
    for ts, o, h, lo, c in (r[:5] for r in sub):
        b = int(ts) - int(ts) % period_ms
        if out and out[-1][0] == b:
            cur = out[-1]
            cur[2], cur[3], cur[4] = max(cur[2], h), min(cur[3], lo), c
        else:
            out.append([b, o, h, lo, c])
    if int(sub[-1][0]) + sub_ms < out[-1][0] + period_ms:
        out.pop()
    return out


def regime_series(sub, bars, cfg=None, period_ms=None, sub_ms=M15, upper_ms=H,
                  fn=None) -> list:
    """Causal jev_regime label at each signal bar's close (None in warmup):
    ``fn`` (default ``classify``) on the last 60 ``sub`` bars and 120 upper
    bars (``sub`` resampled to ``upper_ms``) closed by that close. Defaults
    are the live windows (15m + 1h); R1 is ``sub_ms=H, upper_ms=4H``.
    ``fn=classify_features`` returns the feature dicts instead."""
    cfg = cfg or RegimeConfig()
    fn = fn or classify
    period_ms = period_ms or _step(bars, default=H)
    upper = resample(sub, upper_ms, sub_ms)
    sub_ts = [r[0] for r in sub]
    up_ts = [r[0] for r in upper]
    out = []
    for b in bars:
        close_t = b[0] + period_ms
        i = bisect_right(sub_ts, close_t - sub_ms)  # sub bars closed by close_t
        j = bisect_right(up_ts, close_t - upper_ms) # upper bars closed by close_t
        if i < REGIME_15M_BARS or j < REGIME_1H_BARS:
            out.append(None)
            continue
        out.append(fn(sub[i - REGIME_15M_BARS:i], upper[j - REGIME_1H_BARS:j], cfg))
    return out


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------

def _adverse(price, side, slip, opening) -> float:
    """Slippage against us: buys fill higher, sells lower."""
    buy = (side == "long") == opening
    return price * (1.0 + slip) if buy else price * (1.0 - slip)


def _trade(pos, exit_fill, exit_ts, path, fee, bars, exit_costs=True) -> dict:
    ratio = exit_fill / pos.entry_fill
    gross = ratio - 1.0 if pos.side == "long" else 1.0 - ratio
    net = gross - fee - (fee * ratio if exit_costs else 0.0)
    return {"side": pos.side, "signal_ts": pos.meta["signal_ts"],
            "entry_ts": pos.entry_ts, "exit_ts": int(exit_ts),
            "entry_fill": pos.entry_fill, "exit_fill": exit_fill, "path": path,
            "bars": bars, "net_pct": net * 100.0,
            "r": net / (pos.init_risk / pos.entry_fill)}


def simulate(ind, cfg: BaseConfig, fee, slip, start_ts=None, regimes=None,
             entries=None, exit_regimes=None, decisions=None) -> list:
    """Trades for one symbol. ``entries`` (side | None per bar) replaces the
    strategy's entry signal (random control); exits are unchanged. Labels:
    ``regimes`` feed the entry filter and, unless ``exit_regimes`` is given,
    the per-regime trail. ``decisions`` (a list) collects the flat bars an
    entry could be decided on."""
    ts, op, hi, lo, cl = ind["ts"], ind["open"], ind["high"], ind["low"], ind["close"]
    n = len(ts)
    exit_labels = regimes if exit_regimes is None else exit_regimes
    trades, pos, pending, pending_exit, entry_i = [], None, None, None, 0
    for i in range(n):
        if pos is not None and pending_exit:
            fill = _adverse(op[i], pos.side, slip, opening=False)
            trades.append(_trade(pos, fill, ts[i], pending_exit, fee, i - entry_i))
            pos, pending_exit = None, None
        if pending is not None:
            side, sig_i = pending
            pending = None
            pos = open_position(side, _adverse(op[i], side, slip, opening=True),
                                ts[i], ind["atr"][sig_i], cfg)
            pos.meta["signal_ts"] = ts[sig_i]
            entry_i = i
        if pos is not None:
            hit = stop_exit(pos, op[i], hi[i], lo[i])
            if hit is not None:
                fill = _adverse(hit[0], pos.side, slip, opening=False)
                trades.append(_trade(pos, fill, ts[i], hit[1], fee, i - entry_i + 1))
                pos = None
        if pos is not None:
            pending_exit = after_close(pos, ind, i, cfg,
                                       exit_labels[i] if exit_labels else None)
        elif i + 1 < n and (start_ts is None or ts[i] >= start_ts):
            if decisions is not None:
                decisions.append(i)
            if entries is not None:
                side = entries[i] if ind["atr"][i] is not None else None
            else:
                side = entry_signal(ind, i, cfg, regimes[i] if regimes else None)
            if side is not None:
                pending = (side, i)
    if pos is not None:
        trades.append(_trade(pos, cl[-1], ts[-1], "open", fee, n - entry_i,
                             exit_costs=False))
    return trades


WITH_TREND = {"long": "trend_up", "short": "trend_down"}


def random_control(ind, cfg, fee, slip, rate, long_share, seed, start_ts=None,
                   regimes=None, matched=False) -> list:
    """Seeded coin-flip entries (``rate`` per bar while flat) with the same
    exits. Two draws per bar regardless of state: the schedule is fixed by
    the seed, not by the path. ``regimes`` route the exits as they do the
    strategy's; ``matched`` also discards a drawn entry unless its bar is
    labelled with-trend for the drawn side (regime-matched control, CH-1)."""
    rng = random.Random(seed)
    entries = []
    for k, _ in enumerate(ind["ts"]):
        u, v = rng.random(), rng.random()
        side = None if u >= rate else ("long" if v < long_share else "short")
        if side and matched and (not regimes or regimes[k] != WITH_TREND[side]):
            side = None
        entries.append(side)
    return simulate(ind, cfg, fee, slip, start_ts=start_ts, regimes=regimes,
                    entries=entries)


def shift_routes(labels, lo, rng, min_shift) -> list:
    """``labels`` with ``labels[lo:]`` rotated by k in [min_shift, L - min_shift]
    (L = its length): same on-share and episode lengths, broken timing."""
    seg = labels[lo:]
    if len(seg) - min_shift < min_shift:
        raise ValueError(f"window of {len(seg)} bars is too short to shift by "
                         f">= {min_shift} either way")
    k = rng.randint(min_shift, len(seg) - min_shift)
    return labels[:lo] + seg[k:] + seg[:k]


# ---------------------------------------------------------------------------
# metrics / folds
# ---------------------------------------------------------------------------

def metrics(trades) -> dict:
    trades = sorted(trades, key=lambda t: t.get("exit_ts", 0))
    pcts = [t["net_pct"] for t in trades]
    rs = [t["r"] for t in trades]
    n = len(trades)
    cum = peak = dd = 0.0
    for p in pcts:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    sd = statistics.stdev(rs) if n >= 2 else 0.0
    return {"n": n, "longs": sum(t["side"] == "long" for t in trades),
            "shorts": sum(t["side"] == "short" for t in trades),
            "pf": profit_factor(pcts),
            "win": (sum(p > 0 for p in pcts) / n) if n else None,
            "sum_pct": sum(pcts), "max_dd_pct": dd,
            "avg_r": statistics.fmean(rs) if n else None,
            "sqn": statistics.fmean(rs) / sd * math.sqrt(n) if sd > 0 else None}


def in_window(trades, lo_ms, hi_ms) -> list:
    """Trades whose ENTRY SIGNAL falls in [lo, hi)."""
    return [t for t in trades if lo_ms <= t["signal_ts"] < hi_ms]


def funded_pcts(trades, rate_8h, bar_ms) -> list:
    """net_pct after perps funding: longs pay ``rate_8h`` per 8h held, pro
    rata on entry notional; shorts are credited nothing (conservative). Held
    time is ``bars`` x bar length, so a stop's exit bar counts in full."""
    return [t["net_pct"] - (rate_8h * 100.0 * t["bars"] * bar_ms / (8 * H)
                            if t["side"] == "long" else 0.0) for t in trades]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _f(x, fmt="{:.2f}"):
    return "n/a" if x is None else fmt.format(x)


def _entry_rate(ind, trades, start_ts) -> float:
    eligible = sum(1 for k, t in enumerate(ind["ts"][:-1])
                   if t >= start_ts and ind["atr"][k] is not None)
    flat = eligible - sum(t["bars"] for t in trades)
    return len(trades) / flat if flat > 0 else 0.0


def gate_failures(r, grid: Grid) -> list:
    """The pre-declared pass rule for one config result; [] = PASS."""
    fails = []
    if r["all"]["n"] < grid.min_trades:
        fails.append(f"n<{grid.min_trades}")
    for key, label in (("all", "PF"), ("a", "PF A"), ("b", "PF B")):
        if r[key]["pf"] is None or r[key]["pf"] <= 1.0:
            fails.append(f"{label}<=1")
    symbols = r["per_symbol"].values()
    if 2 * sum(s["sum_pct"] > 0 for s in symbols) <= len(symbols):
        fails.append("symbols")
    if r["pctile"] is None or r["pctile"] < grid.pass_pctile:
        fails.append("rand")
    if r.get("baseline_sum") is not None and r["all"]["sum_pct"] <= r["baseline_sum"]:
        fails.append("base")
    return fails


def _q(xs, q):
    """Nearest-rank quantile (None for no data)."""
    if not xs:
        return None
    xs = sorted(xs)
    return xs[max(0, math.ceil(q * len(xs)) - 1)]


def _decision_rate(ind, trades, decisions, labels=None, long_share=1.0) -> float:
    """Exact control rate: entries per eligible flat decision bar (ATR warm).
    With ``labels`` (matched control) a bar counts by the chance a drawn side
    is with-trend there, so the matched schedule keeps the strategy's rate."""
    elig = [i for i in decisions if ind["atr"][i] is not None]
    if labels is None:
        w = float(len(elig))
    else:
        w = sum(long_share if labels[i] == "trend_up" else
                (1.0 - long_share) if labels[i] == "trend_down" else 0.0 for i in elig)
    return min(1.0, len(trades) / w) if w > 0 else 0.0


def _route_labels(routes, router, routed):
    """A config's labels: ``routes`` is a legacy list (= live) or {router: labels}."""
    if isinstance(routes, dict):
        if router in routes:
            return routes[router]
    elif router == "live" or not routed:
        return routes
    if routed:
        raise ValueError(f"no {router!r} labels in the series")
    return None


def _on(label, sides) -> bool:
    return any(label == WITH_TREND[s] for s in sides)


def _null_stats(sums, ns, holds, strat) -> dict:
    beats = sum(x < strat["sum_pct"] for x in sums) if strat["n"] else 0
    return {"sums": sums, "p50": statistics.median(sums) if sums else None,
            "beats": beats, "seeds": len(sums),
            "pctile": beats / len(sums) if sums and strat["n"] else None,
            "n": (_q(ns, 0.05), _q(ns, 0.5), _q(ns, 0.95)),
            "hold": (_q(holds, 0.5), _q(holds, 0.9))}


def run(series, start_ms, end_ms, fee, slip, sides, seeds, grid=GRIDS["g1"],
        funding=()) -> list:
    """series: {timeframe: {symbol: (bars, routes)}} -> one result dict per
    grid config, each on its own timeframe's bars. ``routes``: a label list
    (the live router, legacy) or {router: labels}. ``funding``: 8h rates for
    the report-only PF stress (empty = no funding, e.g. spot).

    Every config gets the unconditional null; in an extended grid a routed
    config also gets the matched null (routed entries) and the shift null
    (routed exits), and ``grid.routed_gate`` picks the one that gates it."""
    mid = start_ms + (end_ms - start_ms) // 2
    results = []
    for name, overrides in grid.configs:
        tf = grid.timeframe_of(overrides)
        router = overrides.get("router", "live")
        cfg = replace(BaseConfig(), **{"timeframe": tf, "sides": sides,
                                       **base_overrides(overrides)})
        routed = is_routed(cfg)
        trades, per_symbol, arms = [], {}, {}
        for sym, (bars, routes) in series[tf].items():
            labels = _route_labels(routes, router, routed)
            ind = indicators(bars, cfg)
            dec = []
            got = simulate(ind, cfg, fee, slip, start_ts=start_ms, regimes=labels,
                           decisions=dec)
            arms[sym] = (ind, got, labels, dec)
            per_symbol[sym] = metrics(got)
            trades += got
        m = metrics(trades)
        kinds = ["uncond"]
        if grid.extended and routed:
            kinds += ["matched"] * (cfg.trend == "regime") + ["shift"] * (cfg.trail_atr_off > 0)
        nulls = {}
        for kind in kinds:
            sums, ns, holds = [], [], []
            for seed in range(seeds):
                pooled = []
                rng = random.Random(seed)
                for sym, (ind, got, labels, dec) in arms.items():
                    if kind == "shift":
                        shifted = shift_routes(labels, bisect_left(ind["ts"], start_ms), rng,
                                               SHIFT_MIN_BARS)
                        pooled += simulate(ind, cfg, fee, slip, start_ts=start_ms,
                                           regimes=labels, exit_regimes=shifted)
                        continue
                    if not got:
                        continue
                    share = sum(t["side"] == "long" for t in got) / len(got)
                    matched = kind == "matched"
                    if grid.exact_rate:
                        rate = _decision_rate(ind, got, dec, labels if matched else None,
                                              share)
                    else:
                        rate = _entry_rate(ind, got, start_ms)
                    pooled += random_control(ind, cfg, fee, slip, rate, share, seed=seed,
                                             start_ts=start_ms, regimes=labels,
                                             matched=matched)
                if kind == "shift":      # metrics' order: a no-op shift ties exactly
                    pooled.sort(key=lambda t: t.get("exit_ts", 0))
                sums.append(sum(t["net_pct"] for t in pooled))
                ns.append(len(pooled))
                if grid.extended:
                    holds += [t["bars"] for t in pooled]
            nulls[kind] = _null_stats(sums, ns, holds, m)
        gated = grid.routed_gate if grid.extended and routed else "uncond"
        g = nulls[gated]
        on_share = None
        if grid.extended and routed:
            bars_on = held_on = held = n_bars = 0
            for sym, (ind, got, labels, dec) in arms.items():
                for k, t in enumerate(ind["ts"]):
                    if start_ms <= t < end_ms:
                        n_bars += 1
                        bars_on += _on(labels[k], sides)
                for t in got:
                    e = bisect_left(ind["ts"], t["entry_ts"])
                    for k in range(e, e + t["bars"]):
                        held += 1
                        held_on += _on(labels[k], sides)
            on_share = {"bars": bars_on / n_bars if n_bars else None,
                        "held": held_on / held if held else None}
        results.append({
            "name": name, "tf": tf, "all": m, "per_symbol": per_symbol,
            "a": metrics(in_window(trades, start_ms, mid)),
            "b": metrics(in_window(trades, mid, end_ms)),
            "rand_p50": g["p50"], "beats": g["beats"], "seeds": g["seeds"],
            "pctile": g["pctile"],
            "pf_fund": [profit_factor(funded_pcts(trades, f, TF_MS[tf]))
                        for f in funding],
            "router": router if routed else None, "nulls": nulls, "gated": gated,
            "on_share": on_share,
            "holds": (_q([t["bars"] for t in trades], 0.5),
                      _q([t["bars"] for t in trades], 0.9)),
            "baseline_sum": None})
    if grid.baseline is not None:
        base = next(r for r in results if r["name"] == grid.baseline)
        for r in results:
            if r is not base:
                r["baseline_sum"] = base["all"]["sum_pct"]
    return results


def _claims(results, grid, need, seeds) -> list:
    """Router and AI claim lines (extended grids). Both are decided by rules
    fixed in the pre-registration, printed here so nobody re-derives them."""
    by = {r["name"]: r for r in results}
    lines = []
    for r in results:
        sh = r["nulls"].get("shift")
        if sh is None:
            continue
        ok = not gate_failures(r, grid) and sh["pctile"] is not None \
            and sh["pctile"] >= grid.pass_pctile
        lines.append(f"- Router timing, {r['name']}: beats its shift null in "
                     f"{sh['beats']}/{sh['seeds']} seeds (need >= {need}/{seeds}) -> "
                     f"router claim {'SUPPORTED' if ok else 'NOT supported'} "
                     "(needs the gate AND the shift null).")
    for name, overrides in grid.configs:
        if overrides.get("router") != "clf":
            continue
        twin = next((n for n, o in grid.configs if o.get("router") == "r1" and
                     base_overrides(o) == base_overrides(overrides)), None)
        if twin is None:
            continue
        a, b = by[name]["all"]["sum_pct"], by[twin]["all"]["sum_pct"]
        fails = gate_failures(by[name], grid)
        ok = not fails and a > b
        lines.append(f"- AI increment, {name} vs {twin}: sum% {a:+.1f} vs {b:+.1f} "
                     f"({a - b:+.1f}); {name} gate {', '.join(fails) or 'PASS'} -> AI claim "
                     f"{'ACCEPTED' if ok else 'REJECTED (the AI added nothing)'} "
                     "(needs the gate AND sum% above its r1 twin).")
    return lines


def _nulls_table(results) -> list:
    lines = ["| config | router | on-share bars / held | arm | n p5/p50/p95 | hold p50/p90 "
             "| sum% p50 | beats | gates |", "|---" * 9 + "|"]
    for r in results:
        on = r["on_share"]
        cell = ("n/a" if on is None else
                f"{_f(on['bars'], '{:.0%}')} / {_f(on['held'], '{:.0%}')}")
        head = f"| {r['name']} | {r['router'] or 'none'} | {cell} "
        lines.append(head + f"| strategy | {r['all']['n']} "
                     f"| {_f(r['holds'][0], '{}')}/{_f(r['holds'][1], '{}')} "
                     f"| {r['all']['sum_pct']:+.1f} | | |")
        for kind, g in r["nulls"].items():
            lines.append(head + f"| {kind} | {'/'.join(_f(x, '{}') for x in g['n'])} "
                         f"| {_f(g['hold'][0], '{}')}/{_f(g['hold'][1], '{}')} "
                         f"| {_f(g['p50'], '{:+.1f}')} | {g['beats']}/{g['seeds']} "
                         f"| {'yes' if kind == r['gated'] else ''} |")
    return lines


def build_report(results, symbols, start_ms, end_ms, grid_name, book, fee, slip,
                 seeds, funding=(), extra=(), synthetic=False) -> str:
    grid = GRIDS[grid_name]
    mid = start_ms + (end_ms - start_ms) // 2
    need = math.ceil(grid.pass_pctile * seeds - 1e-9)
    fund = "/".join(f"{f * 1e4:g}" for f in funding)
    lines = [
        f"# jev_base backtest grid {grid_name} — {','.join(symbols)} "
        f"{'+'.join(grid.timeframes)}, "
        f"{_ts_bkk(start_ms)[:10]} .. {_ts_bkk(end_ms)[:10]} (+07, end exclusive)",
        "",
    ]
    if synthetic:
        lines += ["**SYNTHETIC DATA — seeded random walk with Markov drift, NOT market data. "
                  "Harness dry run only: these numbers say nothing about any strategy.**", ""]
    lines += [
        "SIMULATED unit-notional returns on historical Binance spot klines — not real "
        "funds, not compounded. Fit window only: nothing at or after "
        f"{_ts_bkk(FIT_CUTOFF_MS)} +07 is used.",
        f"Costs ({book}): fee {fee * 100:.3f}% + slippage {slip * 100:.3f}% per side. "
        f"Halves split by entry-signal time at {_ts_bkk(mid)} +07. Random control: "
        f"{seeds} seeds, same exits, rate- and side-matched entries.",
        f"Gate (pre-declared): n >= {grid.min_trades}, net PF > 1 overall and in both "
        f"halves, sum% > 0 in a majority of symbols, beats random >= "
        f"{grid.pass_pctile:.1%} of seeds (>= {need}/{seeds} seeds).",
    ]
    if grid.extended:
        lines += [
            f"Nulls ({seeds} seeds each; routed exits follow the same labels in every arm): "
            "uncond = random entries on any flat bar; matched = random entries only on "
            "bars the config's router labels with-trend (CH-1); shift = the strategy's "
            f"own entries with its exit labels circularly shifted by >= {SHIFT_MIN_BARS} "
            "bars (router timing). Control rates are exact: trades per eligible flat "
            f"decision bar. \"beats random\" is the {grid.routed_gate} null for routed "
            "configs, uncond otherwise.",
        ] + ([f"Routed configs must also beat the baseline {grid.baseline}'s sum% (base)."]
             if grid.baseline else [])
    lines += [
        (f"Funding stress (report-only, not in the gate): longs pay {fund} bp per 8h "
         "held (pro rata, exit bar counted in full), shorts credited nothing."
         if funding else "Funding: none (spot)."),
        "",
        f"| config | tf | n | L/S | PF | win | sum% | maxDD% | avgR | SQN | PF A | PF B "
        f"| PF fund {fund or 'n/a'}bp | rand sum% p50 | beats rand | gate |",
        "|---" * 16 + "|",
    ]
    for r in results:
        m = r["all"]
        lines.append(
            f"| {r['name']} | {r['tf']} | {m['n']} | {m['longs']}/{m['shorts']} "
            f"| {_fmt_pf(m['pf'])} "
            f"| {_f(m['win'], '{:.0%}')} | {m['sum_pct']:+.1f} | {m['max_dd_pct']:.1f} "
            f"| {_f(m['avg_r'], '{:+.3f}')} | {_f(m['sqn'])} | {_fmt_pf(r['a']['pf'])} "
            f"| {_fmt_pf(r['b']['pf'])} "
            f"| {' / '.join(_fmt_pf(x) for x in r['pf_fund']) or 'n/a'} "
            f"| {_f(r['rand_p50'], '{:+.1f}')} | {r['beats']}/{r['seeds']} "
            f"| {', '.join(gate_failures(r, grid)) or 'PASS'} |")
    lines += ["", "Per symbol (n / PF / sum%):", ""]
    lines.append("| config | " + " | ".join(symbols) + " |")
    lines.append("|---" * (len(symbols) + 1) + "|")
    for r in results:
        cells = [f"{s['n']} / {_fmt_pf(s['pf'])} / {s['sum_pct']:+.1f}"
                 for s in (r["per_symbol"][sym] for sym in symbols)]
        lines.append(f"| {r['name']} | " + " | ".join(cells) + " |")
    if grid.extended:
        lines += ["", "Nulls and routing (per-arm n and hold-time percentiles in bars, "
                      "CH-2; on-share = share of window / held bars labelled with-trend):",
                  ""] + _nulls_table(results)
        lines += ["", "Claims:", ""] + _claims(results, grid, need, seeds)
    if extra:
        lines += [""] + list(extra)
    return "\n".join(lines) + "\n"


def _episodes(flags) -> list:
    """Lengths of the maximal runs of True."""
    out, run_len = [], 0
    for f in flags:
        if f:
            run_len += 1
        elif run_len:
            out.append(run_len)
            run_len = 0
    return out + ([run_len] if run_len else [])


def prescreen_report(series, grid_name, symbols, start_ms, end_ms, sides) -> str:
    """Return-blind feasibility counts per symbol and router: label shares,
    episode lengths, and how many raw baseline entry signals fall on
    with-trend bars. No trade is simulated and no outcome is read."""
    grid = GRIDS[grid_name]
    name = grid.baseline or grid.configs[0][0]
    overrides = dict(grid.configs)[name]
    tf = grid.timeframe_of(overrides)
    cfg = replace(BaseConfig(), **{"timeframe": tf, "sides": sides,
                                   **base_overrides(overrides)})
    lines = [f"# grid {grid_name} prescreen — {','.join(symbols)} {tf}, "
             f"{_ts_bkk(start_ms)[:10]} .. {_ts_bkk(end_ms)[:10]} (+07, end exclusive)", "",
             "This prescreen is return-blind: it counts labels and raw entry signals; no trade is "
             f"simulated and no outcome is read. Raw signals = {name}'s entry rule on "
             "every window bar, position state ignored.", "",
             "| symbol | router | bars | on-share | episodes | episode p50/p90 bars "
             "| raw signals | on with-trend bars |", "|---" * 8 + "|"]
    for router in [r for r in ROUTERS if r != "clf"]:
        tot = {"bars": 0, "on": 0, "eps": [], "sig": 0, "sig_on": 0}
        for sym in symbols:
            bars, routes = series[tf][sym]
            labels = routes[router]
            ind = indicators(bars, cfg)
            ks = [k for k, b in enumerate(bars) if start_ms <= b[0] < end_ms]
            flags = [_on(labels[k], sides) for k in ks]
            sig = [k for k in ks if entry_signal(ind, k, cfg, None) is not None]
            row = {"bars": len(ks), "on": sum(flags), "eps": _episodes(flags),
                   "sig": len(sig), "sig_on": sum(_on(labels[k], sides) for k in sig)}
            for key in tot:
                tot[key] += row[key]
            lines.append(_prescreen_row(sym, router, row))
        lines.append(_prescreen_row("all", router, tot))
    return "\n".join(lines) + "\n"


def _prescreen_row(who, router, r) -> str:
    eps = r["eps"]
    return (f"| {who} | {router} | {r['bars']} | "
            f"{_f(r['on'] / r['bars'] if r['bars'] else None, '{:.1%}')} | {len(eps)} "
            f"| {_f(_q(eps, 0.5), '{}')}/{_f(_q(eps, 0.9), '{}')} | {r['sig']} "
            f"| {r['sig_on']} ({_f(r['sig_on'] / r['sig'] if r['sig'] else None, '{:.1%}')}) |")


def synthetic_fetch(vol=0.003, drift=0.0002, switch=0.001):
    """Keyless-API stand-in for dry runs: per symbol a seeded 15m random walk
    from SYN_ORIGIN_MS whose drift flips sign with probability ``switch`` per
    bar (Markov trend episodes). A bar depends only on (symbol, open_time), so
    any chunking of the fetches agrees."""
    paths = {}

    def bars_to(sym, k):
        st = paths.get(sym)
        if st is None:
            seed = zlib.crc32(sym.encode())
            st = paths[sym] = {"rng": random.Random(seed), "rows": [],
                               "p": 10.0 * (1 + seed % 1000), "d": drift}
        rows, rng = st["rows"], st["rng"]
        while len(rows) <= k:
            if rng.random() < switch:
                st["d"] = -st["d"]
            o = st["p"]
            c = o * math.exp(st["d"] + vol * rng.gauss(0.0, 1.0))
            h = max(o, c) * math.exp(0.5 * vol * abs(rng.gauss(0.0, 1.0)))
            lo = min(o, c) * math.exp(-0.5 * vol * abs(rng.gauss(0.0, 1.0)))
            rows.append((o, h, lo, c))
            st["p"] = c
        return rows

    def fetch(url):
        q = dict(kv.split("=", 1) for kv in url.split("?", 1)[1].split("&"))
        start, end = int(q["startTime"]), int(q["endTime"])
        t = max(start + (-start) % M15, SYN_ORIGIN_MS)
        out = []
        while t <= end and len(out) < int(q.get("limit", 1000)):
            k = (t - SYN_ORIGIN_MS) // M15
            o, h, lo, c = bars_to(q["symbol"], k)[k]
            out.append([t, repr(o), repr(h), repr(lo), repr(c), "0"])
            t += M15
        return out
    return fetch


def build_series(grid, sub, tf, rcfg, need_clf):
    """(bars, routes, clf inputs) for one symbol on one signal timeframe.
    Legacy grids keep the live label list; routed grids get {router: labels}
    and, for the classifier, its causal feature rows."""
    period = TF_MS[tf]
    bars = resample(sub, period, M15)
    if not grid.extended:
        return bars, regime_series(sub, bars, rcfg, period), None
    live_f = regime_series(sub, bars, rcfg, period, fn=classify_features)
    r1_f = regime_series(resample(sub, H, M15), bars, rcfg, period, sub_ms=H,
                         upper_ms=4 * H, fn=classify_features)
    routes = {"live": [f and f["regime"] for f in live_f],
              "r1": [f and f["regime"] for f in r1_f]}
    clf = None
    if need_clf:
        atr = wilder_atr([b[2] for b in bars], [b[3] for b in bars], [b[4] for b in bars], 14)
        clf = {"bars": bars, "X": jr.features(bars, atr, r1_f, live_f),
               "y": jr.labels(bars), "r1": routes["r1"]}
    return bars, routes, clf


def fold_lines(folds_by_tf) -> list:
    lines = []
    for tf, folds in folds_by_tf.items():
        lines += [f"Classifier folds ({tf}; walk-forward, purged, threshold exposure-matched "
                  "to r1 on the training rows; weights not printed):", "",
                  "| fold start | n train | last label end | r1 on-share (train) | tau "
                  "| test bars | test on-share |", "|---" * 7 + "|"]
        for f in folds:
            lines.append(f"| {_ts_bkk(f['start'])[:10]} | {f['n_train']} "
                         f"| {_ts_bkk(f['last_label_end'])} | {f['r1_on_share']:.1%} "
                         f"| {f['tau']:.4f} | {f['n_pred']} "
                         f"| {_f(f['on_share'], '{:.1%}')} |")
    return lines


def main(argv=None, fetch=None, now_ms=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", default="g1", choices=sorted(GRIDS))
    ap.add_argument("--symbols", default=None, help="comma list (default: config pairs)")
    ap.add_argument("--start", default="2024-01-01", help="YYYY-MM-DD, +07")
    ap.add_argument("--end", default="2026-09-17", help="YYYY-MM-DD, +07, exclusive")
    ap.add_argument("--timeframe", default=None, choices=sorted(TF_MS),
                    help="must match the grid's declared timeframe")
    ap.add_argument("--book", default=None, choices=("perps", "spot"),
                    help="default: the grid's declared book, else perps")
    ap.add_argument("--seeds", type=int, default=None, help="default: the grid's")
    ap.add_argument("--config", default=None)
    ap.add_argument("--cache", default=str(ROOT / "runtime" / "klines_15m"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--prescreen", action="store_true",
                    help="routed grids: return-blind label/signal counts only, no trades")
    ap.add_argument("--synthetic", action="store_true",
                    help="dry run on a seeded random walk (temp cache, no network)")
    args = ap.parse_args(argv)

    start_ms, end_ms = since_ms(args.start), since_ms(args.end)
    if end_ms > FIT_CUTOFF_MS:
        print(f"refused: --end {args.end} is past the fit cutoff "
              f"{_ts_bkk(FIT_CUTOFF_MS)} +07", file=sys.stderr)
        return 2
    if start_ms >= end_ms:
        print("refused: --start must be before --end", file=sys.stderr)
        return 2
    grid = GRIDS[args.grid]
    if args.timeframe not in (None, grid.timeframe):
        print(f"refused: grid {args.grid} is declared on {grid.timeframe} bars, "
              f"not {args.timeframe}", file=sys.stderr)
        return 2
    if grid.book and args.book not in (None, grid.book):
        print(f"refused: grid {args.grid} is declared on the {grid.book} book, "
              f"not {args.book}", file=sys.stderr)
        return 2
    if args.prescreen and not grid.extended:
        print(f"refused: --prescreen needs a routed grid, not {args.grid}", file=sys.stderr)
        return 2
    book = args.book or grid.book or "perps"
    seeds = grid.seeds if args.seeds is None else args.seeds
    cfg = load_config(args.config)
    symbols = args.symbols.split(",") if args.symbols else list(cfg.pairs)
    if book == "spot":
        fee, slip, sides = cfg.execution.spot_fee_rate, cfg.execution.slippage_rate, ("long",)
        funding = ()
    else:
        fee, slip, sides = cfg.perps.taker_fee_rate, cfg.perps.slippage_rate, ("long", "short")
        funding = FUNDING_8H
    warmup = max(WARMUP_SIGNAL_BARS * max(TF_MS[tf] for tf in grid.timeframes),
                 (REGIME_1H_BARS + 24) * H)
    if grid.extended:                         # r1 needs 120 x 4h bars closed
        warmup = max(warmup, (REGIME_1H_BARS + 6) * 4 * H)
    need_clf = "clf" in grid.routers and not args.prescreen
    hist_start = start_ms
    if need_clf:
        hist_start = min(start_ms, since_ms(grid.train_start))
    tmp = None
    if args.synthetic:
        tmp = tempfile.TemporaryDirectory()
        fetch, now_ms, cache = synthetic_fetch(), FIT_CUTOFF_MS, tmp.name
    else:
        cache = args.cache
    try:
        series = {tf: {} for tf in grid.timeframes}
        clf_data = {tf: {} for tf in grid.timeframes}
        for sym in symbols:
            sub = load_history(sym, hist_start - warmup, end_ms, cache, fetch=fetch,
                               now_ms=now_ms, pause_s=0.0 if fetch else 0.2)
            for tf in grid.timeframes:
                bars, routes, clf = build_series(grid, sub, tf, cfg.regime, need_clf)
                series[tf][sym] = (bars, routes)
                if clf is not None:
                    clf_data[tf][sym] = clf
                print(f"{sym}: {len(sub)} x 15m -> {len(bars)} x {tf}")
    finally:
        if tmp is not None:
            tmp.cleanup()
    if args.prescreen:
        report = prescreen_report(series, args.grid, symbols, start_ms, end_ms, sides)
    else:
        folds_by_tf = {}
        for tf, data in clf_data.items():
            if data:
                routes, folds_by_tf[tf] = jr.walk_forward(
                    data, start_ms, end_ms, since_ms(grid.train_start), TF_MS[tf])
                for sym, labels in routes.items():
                    series[tf][sym][1]["clf"] = labels
        results = run(series, start_ms, end_ms, fee, slip, sides, seeds, grid, funding)
        report = build_report(results, symbols, start_ms, end_ms, args.grid, book, fee,
                              slip, seeds, funding, extra=fold_lines(folds_by_tf),
                              synthetic=args.synthetic)
    if args.out:
        Path(args.out).write_text(report)
        print(f"report -> {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
