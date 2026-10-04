#!/usr/bin/env python3
"""jev_forward — forward-return replay of logged decisions against Binance klines.

jev_calibrate (M4) answers "what would a vetoed entry have netted" by walking
the RECORDED cycle prices of the store. That can't see what the market did
after the run, between cycles, or for decisions the store never got. This
harness joins the per-cycle decision log (``runtime/paper_decisions.jsonl``)
with public 1m Binance spot klines (keyless; the perps book logs the spot
price too) and reports:

  1. forward returns per decision (15m / 1h / 4h / 24h; ref = logged cycle
     price, fwd = open of the first 1m candle at/after ts + h);
  2. per-gate attribution: veto count, sole-veto count, mean DIRECTIONAL
     forward move of the blocked side vs the round-trip cost;
  3. information coefficient — Spearman(feature, fwd) on all rows and on a
     decimated sample (1 per 15 min per symbol: overlapping windows make the
     all-rows n optimistic);
  4. a rule-faithful sim per book: the REAL jev_gates.decide /
     jev_perps.decide_perps drive entries + hysteresis exits, perps stop/liq
     are checked intrabar on the 1m klines, costs come from config; variants
     are config overrides, each split into H1/H2 by time.

Nothing here touches an exchange account or Jev: klines are public market
data, cached under ``runtime/klines``. Unit-notional returns (sizing tiers
ignored; PF / win rate are size-free). Funding is excluded (fail-open None,
as at runtime when unavailable); the daily kill and M5 portfolio layer are not
modeled (per-symbol books, risk=None).

Usage: .venv/bin/python scripts/jev_forward.py [--decisions PATH]
       [--v1-calls PATH] [--config config/v2.yaml] [--cache runtime/klines]
       [--out report.md]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.request
from bisect import bisect_left
from dataclasses import replace
from pathlib import Path

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_calibrate import (_fmt_pf, _ts_bkk, profit_factor,  # noqa: E402
                           win_rate)
from jev_config import load_config  # noqa: E402
from jev_gates import PortfolioState, decide  # noqa: E402
from jev_perps import (PerpsPortfolioState, decide_perps,  # noqa: E402
                       liq_price_for, stop_price_for)
from jev_questions import CRITERIA_SIZES  # noqa: E402
from jev_scorer import _confidence_min, normalize_score  # noqa: E402

KLINE_URL = "https://api.binance.com/api/v3/klines"
PAGE_LIMIT = 1000
MINUTE_MS = 60_000
HORIZONS = (("15m", 15), ("1h", 60), ("4h", 240), ("24h", 1440))
DECIMATE_MS = 15 * MINUTE_MS
ROOT = Path(__file__).resolve().parent.parent

# Sim variants: (name, {section: {field: value}} overrides, max_hold_min).
# Proposals only — shipped config/v2.yaml is the baseline. Since 2026-10-04
# the shipped short bars are [distribution] / whipsaw off; ``mirrored-shorts``
# replays the pre-ratification bars (shorts unreachable on the 09-27 run).
_MIRRORED_SHORTS = {"perps": {"short_entry_phases": ("breakout", "accumulation"),
                              "short_max_whipsaw": 0.45}}
VARIANTS = (
    ("baseline", {}, None),
    ("mirrored-shorts", _MIRRORED_SHORTS, None),
    ("baseline+hold60", {}, 60),
    ("loose-longs", {"spot": {"entry_min_pump": 55.0, "counter_trend": "allow"},
                     "perps": {"entry_min_pump": 55.0, "counter_trend": "allow"}},
     None),
    ("conf0.55", {"spot": {"min_confidence": 0.55},
                  "perps": {"min_confidence": 0.55}}, None),
)
FEATURES = (
    ("pump", lambda v: _finite(v.get("pump_0_100"))),
    ("dump", lambda v: _finite(v.get("dump_0_100"))),
    ("pump-dump", lambda v: (v["pump_0_100"] - v["dump_0_100"])
     if _finite(v.get("pump_0_100")) is not None
     and _finite(v.get("dump_0_100")) is not None else None),
    ("whipsaw", lambda v: _finite(v.get("whipsaw_prob"))),
    ("exhaustion", lambda v: _finite(v.get("exhaustion_prob"))),
    ("confidence", lambda v: _finite(v.get("confidence"))),
)


# ---------------------------------------------------------------------------
# klines: fetch (injectable) + cache + lookups
# ---------------------------------------------------------------------------

def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "jevelin-forward/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_klines(symbol, start_ms, end_ms, fetch=None, pause_s=0.0) -> list:
    """1m spot klines [open_time, open, high, low, close] for open_time in
    [start_ms, end_ms], paging PAGE_LIMIT at a time. ``fetch(url) -> list``
    is injectable (tests); the default is a keyless public GET."""
    fetch = fetch or _http_json
    out, t = [], int(start_ms)
    while t <= end_ms:
        url = (f"{KLINE_URL}?symbol={symbol}&interval=1m&startTime={t}"
               f"&endTime={int(end_ms)}&limit={PAGE_LIMIT}")
        page = fetch(url)
        if not page:
            break
        out += [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])]
                for r in page]
        t = int(page[-1][0]) + MINUTE_MS
        if pause_s:
            time.sleep(pause_s)
    return out


def load_klines(symbol, start_ms, end_ms, cache_dir, fetch=None,
                pause_s=0.0) -> "Klines":
    """Cached fetch: one file per (symbol, minute-aligned range)."""
    start = int(start_ms) - int(start_ms) % MINUTE_MS
    end = int(end_ms) - int(end_ms) % MINUTE_MS
    path = Path(cache_dir) / f"spot_{symbol}_1m_{start}_{end}.json"
    if path.exists():
        return Klines(json.loads(path.read_text()))
    rows = fetch_klines(symbol, start, end, fetch=fetch, pause_s=pause_s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return Klines(rows)


class Klines:
    """Time-indexed 1m candles; lookups are pure and never extrapolate."""

    def __init__(self, rows):
        rows = sorted(rows, key=lambda r: r[0])
        self.open_time = [int(r[0]) for r in rows]
        self.open = [float(r[1]) for r in rows]
        self.high = [float(r[2]) for r in rows]
        self.low = [float(r[3]) for r in rows]

    def price_at(self, ts_ms):
        """Open of the first candle with open_time >= ts_ms; None past the data."""
        i = bisect_left(self.open_time, ts_ms)
        return self.open[i] if i < len(self.open) else None

    def hilo(self, t0_ms, t1_ms):
        """(max high, min low) over candles with open_time in [t0, t1); None if empty."""
        i = bisect_left(self.open_time, t0_ms)
        j = bisect_left(self.open_time, t1_ms)
        if i >= j:
            return None
        return max(self.high[i:j]), min(self.low[i:j])


# ---------------------------------------------------------------------------
# decision logs -> normalized rows
# ---------------------------------------------------------------------------

def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _jsonl(path):
    with open(path) as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def load_decisions(path) -> list:
    """paper_decisions.jsonl (one row per book per cycle) -> normalized rows."""
    rows = []
    for obj in _jsonl(path):
        ts, price = _finite(obj.get("ts_ms")), _finite(obj.get("price"))
        book, symbol = obj.get("book"), obj.get("symbol")
        if ts is None or price is None or price <= 0 \
                or book not in ("spot", "perps") or not isinstance(symbol, str):
            continue
        verdict = obj.get("verdict")
        vetoed = obj.get("vetoed_by")
        rows.append({"decision_id": obj.get("decision_id"), "book": book,
                     "symbol": symbol, "ts_ms": int(ts), "price": price,
                     "regime": obj.get("regime"),
                     "verdict": verdict if isinstance(verdict, dict) else None,
                     "vetoed_by": list(vetoed) if isinstance(vetoed, list) else [],
                     "action": obj.get("action")})
    rows.sort(key=lambda r: r["ts_ms"])
    return rows


def load_v1_calls(path, symbol="BTCUSDT") -> list:
    """Pre-M0 Jev calls from jev_decisions.jsonl (no decision_id: the v1
    single-symbol run). The audit log has no symbol/price, so ``symbol`` is
    given and the price comes from klines; verdicts are rebuilt with the
    scorer's own normalize_score / _confidence_min. Rows needing any of the
    five answers are skipped."""
    rows = []
    for obj in _jsonl(path):
        if obj.get("decision_id") or obj.get("error"):
            continue
        ts = _finite(obj.get("ts"))
        answers = (obj.get("raw") or {}).get("answers") or {}
        try:
            verdict = {
                "ok": True, "error": None,
                "pump_0_100": normalize_score(answers["pump"]["score"],
                                              CRITERIA_SIZES["pump"]),
                "dump_0_100": normalize_score(answers["dump"]["score"],
                                              CRITERIA_SIZES["dump"]),
                "phase": str(answers["phase"]["choice"]),
                "exhaustion_prob": float(answers["exhaustion"]["noul"]),
                "whipsaw_prob": float(answers["whipsaw"]["noul"]),
                "confidence": _confidence_min(answers),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if ts is None or verdict["confidence"] is None:
            continue
        rows.append({"decision_id": None, "book": None, "symbol": symbol,
                     "ts_ms": int(round(ts * 1000)), "price": None,
                     "regime": None, "verdict": verdict, "vetoed_by": [],
                     "action": None, "source": "v1"})
    rows.sort(key=lambda r: r["ts_ms"])
    return rows


# ---------------------------------------------------------------------------
# forward returns + statistics (pure)
# ---------------------------------------------------------------------------

def forward_returns(rows, klines_by_symbol, horizons=HORIZONS) -> list:
    """Attach row["fwd"] = {label: % move or None}. ref = logged price (or the
    kline open at ts when the log has none); never extrapolated past the data."""
    for row in rows:
        k = klines_by_symbol.get(row["symbol"])
        if row.get("price") is None and k is not None:
            row["price"] = k.price_at(row["ts_ms"])
        ref = row.get("price")
        fwd = {}
        for label, minutes in horizons:
            px = k.price_at(row["ts_ms"] + minutes * MINUTE_MS) if k else None
            fwd[label] = ((px / ref - 1.0) * 100.0
                          if px is not None and ref else None)
        row["fwd"] = fwd
    return rows


def _ranks(values):
    """Average ranks (1-based); ties share the mean of their positions."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(xs, ys, min_n=10):
    """(rho, n) over pairs with both values present; None if n < min_n or a
    side has no variance."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < min_n:
        return None
    rx = _ranks([p[0] for p in pairs])
    ry = _ranks([p[1] for p in pairs])
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy), len(pairs)


def decimate(rows, bucket_ms=DECIMATE_MS) -> list:
    """First row per (symbol, ts bucket): near-independent forward windows."""
    seen, out = set(), []
    for row in sorted(rows, key=lambda r: (r["symbol"], r["ts_ms"])):
        key = (row["symbol"], row["ts_ms"] // bucket_ms)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# per-gate attribution (pure)
# ---------------------------------------------------------------------------

def round_trip_cost_pct(cfg, book) -> float:
    """Entry + exit fee and adverse slippage, % of notional (config, never hardcoded)."""
    if book == "spot":
        per_side = cfg.execution.spot_fee_rate + cfg.execution.slippage_rate
    else:
        per_side = cfg.perps.taker_fee_rate + cfg.perps.slippage_rate
    return 2.0 * per_side * 100.0


def signal_side(row) -> int:
    """+1 long / -1 short: the side the blocked entry would have taken.
    Spot is long-only; perps takes the stronger signal (decide_perps prefers
    long on pump >= dump)."""
    if row["book"] == "spot":
        return 1
    v = row.get("verdict") or {}
    pump, dump = _finite(v.get("pump_0_100")), _finite(v.get("dump_0_100"))
    if pump is None or dump is None:
        return 1
    return 1 if pump >= dump else -1


def _mean(values):
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _dir_means(rows, horizons):
    return {label: _mean([signal_side(r) * r["fwd"][label]
                          if r["fwd"].get(label) is not None else None
                          for r in rows])
            for label, _ in horizons}


def gate_table(rows, book, horizons=HORIZONS) -> list:
    """Per gate: veto count, sole-veto count (the gate was the ONLY blocker —
    its true marginal decision) and mean directional forward move (% of the
    blocked side) over all its vetoes and over its sole vetoes. Positive =
    the veto cost money; negative = the veto saved money. Sorted by count."""
    by_gate = {}
    for row in rows:
        if row["book"] != book or not row["vetoed_by"] or "fwd" not in row:
            continue
        for gate in dict.fromkeys(row["vetoed_by"]):
            by_gate.setdefault(gate, []).append(row)
    table = []
    for gate, grows in by_gate.items():
        sole = [r for r in grows if r["vetoed_by"] == [gate]]
        table.append({"gate": gate, "count": len(grows), "sole": len(sole),
                      "mean": _dir_means(grows, horizons),
                      "sole_mean": _dir_means(sole, horizons)})
    table.sort(key=lambda g: (-g["count"], g["gate"]))
    return table


# ---------------------------------------------------------------------------
# rule-faithful sim (the REAL gate functions drive every entry/exit)
# ---------------------------------------------------------------------------

def apply_variant(cfg, overrides):
    """V2Config with ``{section: {field: value}}`` overrides (dataclasses.replace)."""
    for name, fields in (overrides or {}).items():
        cfg = replace(cfg, **{name: replace(getattr(cfg, name), **fields)})
    return cfg


def _costs(cfg, book):
    if book == "spot":
        return cfg.execution.spot_fee_rate, cfg.execution.slippage_rate
    return cfg.perps.taker_fee_rate, cfg.perps.slippage_rate


def _open(row, side, action, cfg, book):
    _fee, slip = _costs(cfg, book)
    p = row["price"]
    fill = p * (1.0 + slip) if side == "long" else p * (1.0 - slip)
    pos = {"symbol": row["symbol"], "book": book, "side": side,
           "entry_ts": row["ts_ms"], "entry_fill": fill,
           "decision_id": row.get("decision_id")}
    if book == "perps":
        lev = min(_finite(action.get("leverage")) or cfg.perps.max_leverage,
                  cfg.perps.max_leverage)
        pos.update(leverage=lev, stop=stop_price_for(side, fill, cfg.perps),
                   liq=liq_price_for(side, fill, lev))
    return pos


def _close(pos, price, ts, path, cfg):
    """Unit-notional net % of the round trip. Exits pay adverse slippage and
    the exit fee; liquidation fills as-is; "open" marks at price, no exit costs."""
    fee, slip = _costs(cfg, pos["book"])
    long_ = pos["side"] == "long"
    if path in ("liq", "open"):
        fill = price
    else:
        fill = price * (1.0 - slip) if long_ else price * (1.0 + slip)
    ratio = fill / pos["entry_fill"]
    gross = (ratio - 1.0) if long_ else (1.0 - ratio)
    if pos["book"] == "perps":                       # isolated-margin floor
        floor = -1.0 / pos["leverage"]
        gross = floor if path == "liq" else max(gross, floor)
    exit_fee = 0.0 if path == "open" else fee * ratio
    net = gross - fee - exit_fee
    return {"symbol": pos["symbol"], "book": pos["book"], "side": pos["side"],
            "entry_ts": pos["entry_ts"], "exit_ts": ts,
            "entry_fill": pos["entry_fill"], "exit_fill": fill, "path": path,
            "net_pct": net * 100.0,
            "hold_min": (ts - pos["entry_ts"]) / MINUTE_MS,
            "decision_id": pos["decision_id"]}


def _perps_auto_exit(pos, k, t0, t1, price):
    """Liquidation then stop, intrabar on [t0, t1) when klines cover it (a
    resting stop: stricter than the runtime's cycle-price check, which misses
    wicks), else at the cycle price like PerpsPortfolio.apply_action.
    -> (fill reference price, path) or None."""
    long_ = pos["side"] == "long"
    span = k.hilo(t0, t1) if k is not None else None
    hi, lo = span if span is not None else (price, price)
    worst = lo if long_ else hi
    if (long_ and worst <= pos["liq"]) or (not long_ and worst >= pos["liq"]):
        return pos["liq"], "liq"
    if (long_ and worst <= pos["stop"]) or (not long_ and worst >= pos["stop"]):
        if span is None:
            return price, "stop"
        return pos["stop"], "stop"
    if span is not None:  # the window misses the cycle tick itself
        if (long_ and price <= pos["stop"]) or (not long_ and price >= pos["stop"]):
            return price, "stop"
    return None


def simulate(rows, cfg, book, klines_by_symbol, max_hold_min=None) -> list:
    """Replay ``book`` rows per symbol through decide / decide_perps with the
    logged verdict + regime, carrying cooldown and the hysteresis counters the
    way jev_paper / PerpsPortfolio do. Entry/exit at the logged cycle price
    with adverse slippage; ``max_hold_min`` is a harness-only time stop.
    Returns closed trades plus any position still open at the end (path
    "open", marked at the last price)."""
    section = cfg.spot if book == "spot" else cfg.perps
    by_symbol = {}
    for row in rows:
        if row.get("book") == book and row.get("price"):
            by_symbol.setdefault(row["symbol"], []).append(row)
    trades = []
    for symbol in sorted(by_symbol):
        srows = sorted(by_symbol[symbol], key=lambda r: r["ts_ms"])
        k = klines_by_symbol.get(symbol)
        pos, last_entry, esc, held, prev_ts = None, None, 0, 0, None
        for row in srows:
            ts, price = row["ts_ms"], row["price"]
            if pos is not None:
                hit = (_perps_auto_exit(pos, k, prev_ts, ts, price)
                       if book == "perps" else None)
                if hit is None and max_hold_min is not None \
                        and ts - pos["entry_ts"] >= max_hold_min * MINUTE_MS:
                    hit = (price, "time")
                if hit is not None:
                    trades.append(_close(pos, hit[0], ts, hit[1], cfg))
                    pos, esc, held, prev_ts = None, 0, 0, ts
                    continue
            verdict = row.get("verdict") or {}
            if book == "spot":
                pf = PortfolioState(pos is not None, 1000.0, 0.0, last_entry,
                                    esc, held)
                act = decide(verdict, pf, section, ts, row.get("decision_id"),
                             row.get("regime"), None)
            else:
                pf = PerpsPortfolioState(pos is not None, 1000.0, 0.0, last_entry,
                                         esc, held,
                                         pos["side"] if pos is not None else None)
                act = decide_perps(verdict, pf, section, ts, None,
                                   row.get("decision_id"), row.get("regime"), None)
            kind = act.get("action")
            if pos is None and kind in ("enter", "enter_long", "enter_short"):
                side = "short" if kind == "enter_short" else "long"
                pos = _open(row, side, act, cfg, book)
                last_entry, esc, held = ts, 0, 0
            elif pos is not None and kind == "exit":
                trades.append(_close(pos, price, ts, "signal", cfg))
                pos, esc, held = None, 0, 0
            else:
                esc, held = act.get("exit_signal_cycles", 0), act.get("cycles_held", 0)
            prev_ts = ts
        if pos is not None:
            trades.append(_close(pos, srows[-1]["price"], srows[-1]["ts_ms"],
                                 "open", cfg))
    trades.sort(key=lambda t: (t["entry_ts"], t["symbol"]))
    return trades


def summarize(trades) -> dict:
    """n / long / short, PF and win rate (jev_calibrate), sum / avg net %,
    median hold (min) and exit-path counts."""
    pnls = [t["net_pct"] for t in trades]
    paths = {}
    for t in trades:
        paths[t["path"]] = paths.get(t["path"], 0) + 1
    return {"n": len(trades),
            "longs": sum(1 for t in trades if t["side"] == "long"),
            "shorts": sum(1 for t in trades if t["side"] == "short"),
            "pf": profit_factor(pnls), "win_rate": win_rate(pnls),
            "sum_pct": sum(pnls), "avg_pct": _mean(pnls),
            "median_hold_min": (statistics.median(t["hold_min"] for t in trades)
                                if trades else None),
            "paths": paths}


def split_halves(rows):
    """(H1, H2) at the median timestamp: H1 strictly before it. Every book
    splits at the same instant; positions do not carry across the cut."""
    ts = sorted(r["ts_ms"] for r in rows)
    if not ts:
        return [], []
    cut = ts[len(ts) // 2]
    return ([r for r in rows if r["ts_ms"] < cut],
            [r for r in rows if r["ts_ms"] >= cut])


# ---------------------------------------------------------------------------
# report (markdown, Bangkok times) + CLI
# ---------------------------------------------------------------------------

def kline_range(rows, now_ms):
    """[first decision - 1m, last decision + 24h + 5m], never past now - 1m
    (Binance has no future candles; an open candle would poison the cache)."""
    ts = [r["ts_ms"] for r in rows]
    start = min(ts) - min(ts) % MINUTE_MS - MINUTE_MS
    end = max(ts) + (HORIZONS[-1][1] + 5) * MINUTE_MS
    end = min(end, int(now_ms) - MINUTE_MS)
    return start, end - end % MINUTE_MS


def _signed(x, digits=2):
    return "n/a" if x is None else f"{x:+.{digits}f}"


def _unique(rows):
    """One row per (symbol, ts): both books log the same verdict per cycle."""
    seen, out = set(), []
    for r in rows:
        key = (r["symbol"], r["ts_ms"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _fwd_by(rows, key, horizons):
    groups = {}
    for r in rows:
        groups.setdefault(key(r) or "n/a", []).append(r)
    lines = ["| " + " | ".join(["group", "n"] + [h for h, _ in horizons]) + " |",
             "|" + "---|" * (2 + len(horizons))]
    for name in sorted(groups, key=lambda g: (-len(groups[g]), str(g))):
        g = groups[name]
        cells = [_signed(_mean([r["fwd"].get(h) for r in g])) for h, _ in horizons]
        lines.append(f"| {name} | {len(g)} | " + " | ".join(cells) + " |")
    return lines


def _ic_table(rows, horizons):
    lines = ["| feature | " + " | ".join(h for h, _ in horizons) + " |",
             "|---|" + "---|" * len(horizons)]
    for name, fn in FEATURES:
        cells = []
        for h, _ in horizons:
            xs = [fn(r["verdict"]) if r.get("verdict") else None for r in rows]
            res = spearman(xs, [r["fwd"].get(h) for r in rows])
            cells.append("n/a" if res is None else f"{res[0]:+.3f} (n={res[1]})")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return lines


def _sim_line(name, book, part, s):
    paths = " ".join(f"{k}:{v}" for k, v in sorted(s["paths"].items())) or "-"
    win = "n/a" if s["win_rate"] is None else f"{s['win_rate'] * 100:.0f}%"
    hold = "n/a" if s["median_hold_min"] is None else f"{s['median_hold_min']:.0f}"
    return (f"| {name} | {book} | {part} | {s['n']} | {s['longs']}/{s['shorts']} | "
            f"{_fmt_pf(s['pf'])} | {win} | {_signed(s['sum_pct'])} | "
            f"{_signed(s['avg_pct'], 3)} | {hold} | {paths} |")


def build_report(rows, klines_by_symbol, cfg, v1_rows=(), variants=VARIANTS,
                 horizons=HORIZONS) -> str:
    """Markdown report over rows that already carry ``fwd`` (forward_returns)."""
    out = ["# Jevelin forward replay — generated by scripts/jev_forward.py", "",
           "Machine output; analysis and recommendations live in the dated "
           "report that cites it. Times Asia/Bangkok. Forward move = % from the "
           "logged cycle price to the open of the first 1m candle at/after ts + h.",
           "", "## Window", ""]
    books = {b: sum(1 for r in rows if r["book"] == b) for b in ("spot", "perps")}
    uniq = _unique(rows)
    out.append(f"- {len(rows)} decision rows (spot {books['spot']}, perps "
               f"{books['perps']}), {len(uniq)} unique (symbol, cycle); "
               f"{_ts_bkk(rows[0]['ts_ms'])} -> {_ts_bkk(rows[-1]['ts_ms'])}")
    out += ["", "| symbol | cycles | first px | last px | move % |",
            "|---|---|---|---|---|"]
    for sym in sorted({r["symbol"] for r in uniq}):
        srows = [r for r in uniq if r["symbol"] == sym]
        a, b = srows[0]["price"], srows[-1]["price"]
        out.append(f"| {sym} | {len(srows)} | {a:g} | {b:g} | "
                   f"{_signed((b / a - 1.0) * 100.0)} |")

    out += ["", "## Forward returns by regime", "",
            "Mean raw forward move % (not directional), unique cycles.", ""]
    out += _fwd_by(uniq, lambda r: r.get("regime"), horizons)
    out += ["", "### by Jev phase", ""]
    out += _fwd_by(uniq, lambda r: (r.get("verdict") or {}).get("phase"), horizons)

    out += ["", "## Per-gate attribution", "",
            "Mean DIRECTIONAL forward move % of the side the veto blocked "
            "(gross; a veto only cost money where this beats the round trip). "
            "sole = the gate was the only blocker.", ""]
    for book in ("spot", "perps"):
        cost = round_trip_cost_pct(cfg, book)
        out += [f"### {book} — round trip {cost:.2f}%", "",
                "| gate | vetoes | sole | " + " | ".join(h for h, _ in horizons)
                + " | " + " | ".join(f"sole {h}" for h, _ in horizons) + " |",
                "|" + "---|" * (3 + 2 * len(horizons))]
        for g in gate_table(rows, book, horizons):
            out.append(f"| {g['gate']} | {g['count']} | {g['sole']} | "
                       + " | ".join(_signed(g["mean"][h]) for h, _ in horizons)
                       + " | "
                       + " | ".join(_signed(g["sole_mean"][h]) for h, _ in horizons)
                       + " |")
        out.append("")

    out += ["## Information coefficient", "",
            "Spearman(feature, forward move). Overlapping windows inflate the "
            "all-rows n; trust the decimated table (1 per 15 min per symbol, "
            "SE ~ 1/sqrt(n)).", "", "### all unique cycles", ""]
    out += _ic_table(uniq, horizons)
    dec = decimate(uniq)
    out += ["", f"### decimated (n={len(dec)})", ""]
    out += _ic_table(dec, horizons)
    if v1_rows:
        out += ["", f"### v1 BTC window ({len(v1_rows)} calls, "
                f"{_ts_bkk(v1_rows[0]['ts_ms'])} -> {_ts_bkk(v1_rows[-1]['ts_ms'])})",
                "", "all:", ""]
        out += _ic_table(v1_rows, horizons)
        v1_dec = decimate(v1_rows)
        out += ["", f"decimated (n={len(v1_dec)}):", ""]
        out += _ic_table(v1_dec, horizons)

    out += ["", "## Rule-faithful sim", "",
            "Real decide / decide_perps on the logged verdict + regime; unit "
            "notional, net of fees + slippage; perps stop/liq intrabar on 1m "
            "klines; funding, daily kill and M5 portfolio risk not modeled. "
            "H1/H2 split at the median timestamp (positions do not cross it).", ""]
    for name, overrides, hold in variants:
        desc = json.dumps(overrides, default=list) if overrides else "shipped config"
        out.append(f"- **{name}**: {desc}"
                   + (f", time stop {hold} min" if hold else ""))
    out += ["", "| variant | book | slice | n | L/S | PF | win | sum % | avg % | "
            "med hold m | exits |", "|" + "---|" * 11]
    h1, h2 = split_halves(rows)
    for name, overrides, hold in variants:
        vcfg = apply_variant(cfg, overrides)
        for book in ("spot", "perps"):
            for part, prows in (("ALL", rows), ("H1", h1), ("H2", h2)):
                trades = simulate(prows, vcfg, book, klines_by_symbol, hold)
                out.append(_sim_line(name, book, part, summarize(trades)))
    return "\n".join(out) + "\n"


def main(argv=None, fetch=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--decisions", default=str(ROOT / "runtime/paper_decisions.jsonl"))
    ap.add_argument("--v1-calls", default=None)
    ap.add_argument("--config", default=str(ROOT / "config/v2.yaml"))
    ap.add_argument("--cache", default=str(ROOT / "runtime/klines"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--now-ms", type=int, default=None)
    args = ap.parse_args(argv)
    now = args.now_ms if args.now_ms is not None else int(time.time() * 1000)
    pause = 0.0 if fetch is not None else 0.25
    cfg = load_config(args.config)
    rows = load_decisions(args.decisions)
    if not rows:
        print("jev_forward: no decision rows", file=sys.stderr)
        return 1
    start, end = kline_range(rows, now)
    klines = {sym: load_klines(sym, start, end, args.cache, fetch, pause)
              for sym in sorted({r["symbol"] for r in rows})}
    forward_returns(rows, klines)
    v1 = load_v1_calls(args.v1_calls) if args.v1_calls else []
    if v1:
        v1_start, v1_end = kline_range(v1, now)
        forward_returns(v1, {"BTCUSDT": load_klines("BTCUSDT", v1_start, v1_end,
                                                    args.cache, fetch, pause)})
    text = build_report(rows, klines, cfg, v1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        print(f"jev_forward: wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
