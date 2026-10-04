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

Usage: .venv/bin/python scripts/jev_base_backtest.py [--grid g1|g2]
       [--symbols BTCUSDT,...] [--start 2024-01-01] [--end 2026-09-17]
       [--book perps|spot] [--seeds N] [--cache runtime/klines_15m]
       [--out report.md]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_base import (BaseConfig, after_close, entry_signal,  # noqa: E402
                      indicators, open_position, stop_exit)
from jev_calibrate import _fmt_pf, _ts_bkk, profit_factor, since_ms  # noqa: E402
from jev_config import RegimeConfig, load_config  # noqa: E402
from jev_forward import ROOT, fetch_klines  # noqa: E402
from jev_regime import classify  # noqa: E402

M15 = 15 * 60_000
H = 3_600_000
TF_MS = {"15m": M15, "1h": H, "4h": 4 * H}
FIT_CUTOFF_MS = 1_789_578_000_000      # 2026-09-17 00:00 +07 (Jev contamination boundary)
REGIME_15M_BARS = 60                   # = market.ohlcv_15m_limit
REGIME_1H_BARS = 120                   # = market.ohlcv_1h_limit
WARMUP_SIGNAL_BARS = 250               # > trend_slow (200) and every lookback

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

    def __post_init__(self):
        for tf in self.timeframes:
            if tf not in TF_MS:
                raise ValueError(f"timeframe must be one of {sorted(TF_MS)}: {tf!r}")

    def timeframe_of(self, overrides) -> str:
        return overrides.get("timeframe", self.timeframe)

    @property
    def timeframes(self) -> tuple:
        """Signal timeframes the grid needs, the default first."""
        out = [self.timeframe]
        for _, overrides in self.configs:
            if self.timeframe_of(overrides) not in out:
                out.append(self.timeframe_of(overrides))
        return tuple(out)


GRIDS = {
    "g1": Grid("1h", GRID, seeds=200, pass_pctile=0.95),
    # 12 configs have now been tested on this one window: Bonferroni 0.05 / 12.
    "g2": Grid("4h", GRID2, seeds=1000, pass_pctile=1 - 0.05 / 12),
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


def regime_series(sub, bars, cfg=None, period_ms=None) -> list:
    """Causal jev_regime label at each signal bar's close (None in warmup)."""
    cfg = cfg or RegimeConfig()
    period_ms = period_ms or _step(bars, default=H)
    hours = resample(sub, H, M15)
    sub_ts = [r[0] for r in sub]
    h_ts = [r[0] for r in hours]
    out = []
    for b in bars:
        close_t = b[0] + period_ms
        i = bisect_right(sub_ts, close_t - M15)     # 15m bars closed by close_t
        j = bisect_right(h_ts, close_t - H)         # 1h bars closed by close_t
        if i < REGIME_15M_BARS or j < REGIME_1H_BARS:
            out.append(None)
            continue
        out.append(classify(sub[i - REGIME_15M_BARS:i], hours[j - REGIME_1H_BARS:j], cfg))
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
             entries=None) -> list:
    """Trades for one symbol. ``entries`` (side | None per bar) replaces the
    strategy's entry signal (random control); exits are unchanged."""
    ts, op, hi, lo, cl = ind["ts"], ind["open"], ind["high"], ind["low"], ind["close"]
    n = len(ts)
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
            pending_exit = after_close(pos, ind, i, cfg)
        elif i + 1 < n and (start_ts is None or ts[i] >= start_ts):
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


def random_control(ind, cfg, fee, slip, rate, long_share, seed, start_ts=None) -> list:
    """Seeded coin-flip entries (``rate`` per bar while flat) with the same
    exits. Two draws per bar regardless of state: the schedule is fixed by
    the seed, not by the path."""
    rng = random.Random(seed)
    entries = []
    for _ in ind["ts"]:
        u, v = rng.random(), rng.random()
        entries.append(None if u >= rate else ("long" if v < long_share else "short"))
    return simulate(ind, cfg, fee, slip, start_ts=start_ts, entries=entries)


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
    return fails


def run(series, start_ms, end_ms, fee, slip, sides, seeds, grid=GRIDS["g1"],
        funding=()) -> list:
    """series: {timeframe: {symbol: (bars, regimes)}} -> one result dict per
    grid config, each on its own timeframe's bars. ``funding``: 8h rates for
    the report-only PF stress (empty = no funding, e.g. spot)."""
    mid = start_ms + (end_ms - start_ms) // 2
    results = []
    for name, overrides in grid.configs:
        tf = grid.timeframe_of(overrides)
        cfg = replace(BaseConfig(), **{"timeframe": tf, "sides": sides, **overrides})
        trades, rand_sums, per_symbol = [], [], {}
        inds = {}
        for sym, (bars, regimes) in series[tf].items():
            ind = indicators(bars, cfg)
            got = simulate(ind, cfg, fee, slip, start_ts=start_ms, regimes=regimes)
            inds[sym] = (ind, got)
            per_symbol[sym] = metrics(got)
            trades += got
        for seed in range(seeds):
            pooled = []
            for sym, (ind, got) in inds.items():
                if not got:
                    continue
                share = sum(t["side"] == "long" for t in got) / len(got)
                pooled += random_control(ind, cfg, fee, slip,
                                         _entry_rate(ind, got, start_ms), share,
                                         seed=seed, start_ts=start_ms)
            rand_sums.append(sum(t["net_pct"] for t in pooled))
        m = metrics(trades)
        beats = sum(s < m["sum_pct"] for s in rand_sums) if m["n"] else 0
        results.append({
            "name": name, "tf": tf, "all": m, "per_symbol": per_symbol,
            "a": metrics(in_window(trades, start_ms, mid)),
            "b": metrics(in_window(trades, mid, end_ms)),
            "rand_p50": statistics.median(rand_sums) if rand_sums else None,
            "beats": beats, "seeds": len(rand_sums),
            "pctile": beats / len(rand_sums) if rand_sums and m["n"] else None,
            "pf_fund": [profit_factor(funded_pcts(trades, f, TF_MS[tf]))
                        for f in funding]})
    return results


def build_report(results, symbols, start_ms, end_ms, grid_name, book, fee, slip,
                 seeds, funding=()) -> str:
    grid = GRIDS[grid_name]
    mid = start_ms + (end_ms - start_ms) // 2
    need = math.ceil(grid.pass_pctile * seeds - 1e-9)
    fund = "/".join(f"{f * 1e4:g}" for f in funding)
    lines = [
        f"# jev_base backtest grid {grid_name} — {','.join(symbols)} "
        f"{'+'.join(grid.timeframes)}, "
        f"{_ts_bkk(start_ms)[:10]} .. {_ts_bkk(end_ms)[:10]} (+07, end exclusive)",
        "",
        "SIMULATED unit-notional returns on historical Binance spot klines — not real "
        "funds, not compounded. Fit window only: nothing at or after "
        f"{_ts_bkk(FIT_CUTOFF_MS)} +07 is used.",
        f"Costs ({book}): fee {fee * 100:.3f}% + slippage {slip * 100:.3f}% per side. "
        f"Halves split by entry-signal time at {_ts_bkk(mid)} +07. Random control: "
        f"{seeds} seeds, same exits, rate- and side-matched entries.",
        f"Gate (pre-declared): n >= {grid.min_trades}, net PF > 1 overall and in both "
        f"halves, sum% > 0 in a majority of symbols, beats random >= "
        f"{grid.pass_pctile:.1%} of seeds (>= {need}/{seeds} seeds).",
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
    return "\n".join(lines) + "\n"


def main(argv=None, fetch=None, now_ms=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", default="g1", choices=sorted(GRIDS))
    ap.add_argument("--symbols", default=None, help="comma list (default: config pairs)")
    ap.add_argument("--start", default="2024-01-01", help="YYYY-MM-DD, +07")
    ap.add_argument("--end", default="2026-09-17", help="YYYY-MM-DD, +07, exclusive")
    ap.add_argument("--timeframe", default=None, choices=sorted(TF_MS),
                    help="must match the grid's declared timeframe")
    ap.add_argument("--book", default="perps", choices=("perps", "spot"))
    ap.add_argument("--seeds", type=int, default=None, help="default: the grid's")
    ap.add_argument("--config", default=None)
    ap.add_argument("--cache", default=str(ROOT / "runtime" / "klines_15m"))
    ap.add_argument("--out", default=None)
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
    seeds = grid.seeds if args.seeds is None else args.seeds
    cfg = load_config(args.config)
    symbols = args.symbols.split(",") if args.symbols else list(cfg.pairs)
    if args.book == "spot":
        fee, slip, sides = cfg.execution.spot_fee_rate, cfg.execution.slippage_rate, ("long",)
        funding = ()
    else:
        fee, slip, sides = cfg.perps.taker_fee_rate, cfg.perps.slippage_rate, ("long", "short")
        funding = FUNDING_8H
    warmup = max(WARMUP_SIGNAL_BARS * max(TF_MS[tf] for tf in grid.timeframes),
                 (REGIME_1H_BARS + 24) * H)

    series = {tf: {} for tf in grid.timeframes}
    for sym in symbols:
        sub = load_history(sym, start_ms - warmup, end_ms, args.cache, fetch=fetch,
                           now_ms=now_ms, pause_s=0.0 if fetch else 0.2)
        for tf in grid.timeframes:
            bars = resample(sub, TF_MS[tf], M15)
            series[tf][sym] = (bars, regime_series(sub, bars, cfg.regime, TF_MS[tf]))
            print(f"{sym}: {len(sub)} x 15m -> {len(bars)} x {tf}")
    results = run(series, start_ms, end_ms, fee, slip, sides, seeds, grid, funding)
    report = build_report(results, symbols, start_ms, end_ms, args.grid,
                          args.book, fee, slip, seeds, funding)
    if args.out:
        Path(args.out).write_text(report)
        print(f"report -> {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
