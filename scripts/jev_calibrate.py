#!/usr/bin/env python3
"""jev_calibrate — M4 offline calibration report from the store. Zero network.

This is where we learn whether Jev has an edge: replay is free, never re-ask
Jev. The report is built from recorded store rows only (``gate_decisions`` +
``trades``; run ``scripts/jev_import.py`` first if the store is stale) and every
figure comes from the store or from the deterministic counterfactual engine
below — nothing is invented. One row per run lands in ``calibration_runs``.

Report sections (markdown, timestamps Asia/Bangkok):
  1. Overview — date range, counts, per-book trips/PnL/PF/equity, cross-checked
     against the jev_replay.py --summary accounting on the same rows;
  2. Regime distribution — decisions + trip PnL/PF by regime per book;
  3. Per-gate veto attribution — veto counts + counterfactual PnL ("veto
     value": what the vetoed entries would have netted) + avg per veto;
  4. Confidence calibration curve — entry-confidence buckets (M3 tiers);
  5. Fan-out stats — call count, tie rate, single-vs-fan-out flips;
  6. Hysteresis stats — signal exits by path + median hold time;
  7. Re-tune proposals — deterministic evidence-quoted heuristics only,
     NEVER auto-applied (a human must approve any config change).

Counterfactual engine (pure): for every stored skip decision with a nonzero
veto bitmask and no position open in that book, the hypothetical entry comes
from the stored verdict (spot: long on pump; perps: long on pump / short on
dump, stronger side when both qualify), sized by the stored confidence tier and
the book caps, filled at the stored cycle price with the M0 adverse slippage +
fee (config/v2.yaml execution block — loaded, never hardcoded). The walk
forward replays the ACTUAL stored verdicts under the M3 exit rules (dump/pump
hysteresis, single >=75 bar, min hold) plus perps stop/liq on recorded prices;
still open at data end -> marked at the last recorded price (labelled open).
Net PnL is after fees + slippage on both sides (funding is excluded: per-decision
funding rates are not stored; holds under 8h make it ~0 — an estimate, and
labelled as one). The counterfactual is attributed to EVERY gate in the
decision's bitmask — multi-gated rows overlap by design.

Usage: .venv/bin/python scripts/jev_calibrate.py [--out report.md]
       [--since YYYY-MM-DD] [--db runtime/jevelin.db]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_gates  # noqa: E402
import jev_perps  # noqa: E402
import jev_replay  # noqa: E402
import jev_store  # noqa: E402
from jev_config import GATE_NAMES, ConfigError, bit_names, load_config  # noqa: E402

BOOKS = ("spot", "perps")
BANGKOK = ZoneInfo("Asia/Bangkok")
CLOSE_ACTIONS = ("exit", "exited", "stop_loss", "liquidated")

# exit path labels (M3 hysteresis paths + the automatic ones + mark-to-market)
PATH_2CONSEC = "2-consecutive"
PATH_SINGLE = "single >=75"
PATH_STOP = "stop"
PATH_LIQ = "liq"
PATH_OPEN = "open"
PATH_OTHER = "other"
PATHS = (PATH_2CONSEC, PATH_SINGLE, PATH_STOP, PATH_LIQ, PATH_OTHER)

MIN_TRIPS_FOR_PROPOSALS = 50
PROPOSAL_EQUITY_FRACTION = 0.02   # |10-veto value| > 2% of current equity
PROPOSAL_VETOES = 10


# ---------------------------------------------------------------------------
# small helpers (pure)
# ---------------------------------------------------------------------------

def _raw_obj(text) -> dict:
    try:
        obj = json.loads(text or "")
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def _f(value):
    return float(value) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) and math.isfinite(float(value)) else None


def _fmt(x, digits=2):
    return "n/a" if x is None else f"{x:.{digits}f}"


def _fmt_pf(pf):
    if pf is None:
        return "n/a"
    return "inf" if math.isinf(pf) else f"{pf:.2f}"


def _ts_bkk(ts_ms) -> str:
    if ts_ms is None:
        return "n/a"
    return datetime.fromtimestamp(float(ts_ms) / 1000.0, BANGKOK).strftime(
        "%Y-%m-%d %H:%M")


def since_ms(since):
    """YYYY-MM-DD -> epoch ms of that day's start in Asia/Bangkok."""
    if since is None:
        return None
    try:
        day = datetime.strptime(str(since), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"--since must be YYYY-MM-DD, got {since!r}") from exc
    return day.replace(tzinfo=BANGKOK).timestamp() * 1000.0

# ---------------------------------------------------------------------------
# store loaders -> normalized rows (the engine's input shape)
# ---------------------------------------------------------------------------

def gate_rows_from_store(conn, since_ts_ms=None) -> list:
    """Normalized gate-decision rows: verdict parsed, vetoed_by from raw_json."""
    rows = []
    for r in jev_store.list_gate_decisions(conn, since_ts=since_ts_ms):
        raw = _raw_obj(r.get("raw_json"))
        verdict = None
        if r.get("verdict_json"):
            try:
                verdict = json.loads(r["verdict_json"])
            except ValueError:
                verdict = None
        rows.append({
            "ts_ms": _f(r.get("ts")),
            "decision_id": r.get("decision_id"),
            "book": r.get("book"),
            "symbol": r.get("symbol"),
            "price": _f(r.get("price")),
            "action": r.get("action"),
            "executed": r.get("executed"),
            "veto_bitmask": r.get("veto_bitmask") or 0,
            "vetoed_by": raw.get("vetoed_by")
            if isinstance(raw.get("vetoed_by"), list) else [],
            "reason": r.get("reason") or "",
            "equity": _f(r.get("equity")),
            "regime": r.get("regime"),
            "fan_out": bool(r.get("fan_out")),
            "verdict": verdict if isinstance(verdict, dict) else None,
        })
    return rows


def trade_rows_from_store(conn, since_ts_ms=None) -> list:
    """Normalized trade rows (fills) with an is_closing flag (M1 semantics)."""
    rows = []
    for r in jev_store.list_trades(conn):
        ts = _f(r.get("ts"))
        if since_ts_ms is not None and (ts is None or ts < since_ts_ms):
            continue
        raw = _raw_obj(r.get("raw_json"))
        row = {
            "ts_ms": ts,
            "decision_id": r.get("decision_id"),
            "book": r.get("book"),
            "symbol": r.get("symbol"),
            "side": r.get("side"),
            "price": _f(r.get("price")),
            "qty": _f(r.get("qty")),
            "fees": _f(r.get("fees")),
            "slippage": _f(r.get("slippage")),
            "realized_pnl": _f(r.get("realized_pnl")),
            "funding_paid": _f(r.get("funding_paid")),
            "reason": r.get("reason") or "",
            "action": raw.get("action"),
        }
        row["is_closing"] = (raw.get("action") in CLOSE_ACTIONS) \
            or row["side"] == "sell"
        rows.append(row)
    return rows


def is_closing(row: dict) -> bool:
    if "is_closing" in row:
        return bool(row["is_closing"])
    return (row.get("action") in CLOSE_ACTIONS) or row.get("side") == "sell"

def position_intervals(trades, book, symbol=None) -> list:
    """Actual (entry_ts, close_ts|None) intervals from the real fills."""
    intervals = []
    open_at = None
    rows = sorted((t for t in trades if t.get("book") == book
                   and (symbol is None or t.get("symbol") == symbol)),
                  key=lambda t: (t.get("ts_ms") or 0))
    for row in rows:
        ts = row.get("ts_ms")
        if is_closing(row):
            if open_at is not None:
                intervals.append((open_at, ts))
                open_at = None
        elif open_at is None:
            open_at = ts
    if open_at is not None:
        intervals.append((open_at, None))
    return intervals


def position_open_at(intervals, ts_ms) -> bool:
    if ts_ms is None:
        return False
    return any(e is not None and e <= ts_ms and (c is None or ts_ms < c)
               for e, c in intervals)


def exit_path_of(row: dict) -> str:
    """Classify one closing fill: which M3/automatic exit path fired."""
    action = row.get("action")
    reason = row.get("reason") or ""
    if action == "liquidated" or "liquidated" in reason:
        return PATH_LIQ
    if action == "stop_loss" or reason == "stop_loss":
        return PATH_STOP
    if "single-tick exit bar" in reason:
        return PATH_SINGLE
    if "consecutive cycles" in reason:
        return PATH_2CONSEC
    return PATH_OTHER


def round_trips(trades, book) -> list:
    """Pair entry/close fills into round trips (one position at a time per book)."""
    trips = []
    for symbol in sorted({t.get("symbol") for t in trades if t.get("book") == book}):
        rows = sorted((t for t in trades if t.get("book") == book
                       and t.get("symbol") == symbol),
                      key=lambda t: (t.get("ts_ms") or 0))
        entry = None
        for row in rows:
            if is_closing(row):
                if entry is None:
                    continue
                close_ts, entry_ts = row.get("ts_ms"), entry.get("ts_ms")
                trips.append({
                    "book": book, "symbol": symbol,
                    "entry": entry, "close": row,
                    "decision_id": entry.get("decision_id"),
                    "net": (row.get("realized_pnl") or 0.0)
                    - (row.get("funding_paid") or 0.0),
                    "hold_ms": (close_ts - entry_ts)
                    if close_ts is not None and entry_ts is not None else None,
                    "path": exit_path_of(row),
                })
                entry = None
            elif entry is None:
                entry = row
    return trips


def trade_stats(rows) -> dict:
    """M0 accounting over normalized fills (mirrors jev_replay._stats_from_rows)."""
    realized = fees = slippage = funding = 0.0
    trips = 0
    for row in rows:
        realized += row.get("realized_pnl") or 0.0
        fees += row.get("fees") or 0.0
        slippage += row.get("slippage") or 0.0
        funding += row.get("funding_paid") or 0.0
        if is_closing(row):
            trips += 1
    return {"trips": trips, "gross": realized + fees + slippage, "fees": fees,
            "slippage": slippage, "funding": funding,
            "net": realized - funding}

# ---------------------------------------------------------------------------
# counterfactual engine (pure): vetoed entry -> hypothetical round trip
# ---------------------------------------------------------------------------

def _section(cfg, book):
    return cfg.spot if book == "spot" else cfg.perps


def counterfactual_entry(decision, cfg):
    """Hypothetical entry fill for a vetoed decision, or None.

    None = fail-open: no qualifying side signal in the stored verdict (the
    entry would not have fired even with the veto removed) or missing data.
    Sizing mirrors the books exactly: M3 confidence tier x book cap, adverse
    slippage in the fill price, fee on the quote-side notional.
    """
    try:
        book = decision.get("book")
        if book not in BOOKS:
            return None
        verdict = decision.get("verdict")
        if not isinstance(verdict, dict):
            return None
        pump = _f(verdict.get("pump_0_100"))
        dump = _f(verdict.get("dump_0_100"))
        conf = _f(verdict.get("confidence"))
        price = _f(decision.get("price"))
        equity = _f(decision.get("equity"))
        if None in (pump, dump, conf, price, equity) or price <= 0 or equity <= 0:
            return None
        section = _section(cfg, book)
        # side from the verdict: spot is long-only on pump; perps goes long on
        # pump / short on dump; both qualify -> prefer the stronger signal.
        long_ok = pump >= section.entry_min_pump
        short_ok = book == "perps" and dump >= section.short_min_dump
        if book == "spot":
            side = "long" if long_ok else None
        elif long_ok and (pump >= dump or not short_ok):
            side = "long"
        else:
            side = "short" if short_ok else None
        if side is None:
            return None
        frac, tier = jev_gates.sizing_tier(conf, section)
        entry = {"ts_ms": decision.get("ts_ms"),
                 "decision_id": decision.get("decision_id"),
                 "book": book, "symbol": decision.get("symbol"),
                 "side": side, "price": price, "equity": equity,
                 "confidence": conf, "size_tier": tier, "size_fraction": None,
                 "margin": None, "notional": None, "leverage": None,
                 "stop_price": None, "liq_price": None}
        if book == "spot":
            slip, fee_rate = cfg.execution.slippage_rate, cfg.execution.spot_fee_rate
            size_fraction = round(section.max_position_fraction * frac, 4)
            usd = size_fraction * equity
            fill = price * (1.0 + slip)          # buys fill UP
            qty = usd / fill
            entry_fee = usd * fee_rate
            entry.update(size_fraction=size_fraction, usd=usd, qty=qty,
                         fill=fill, entry_fee=entry_fee)
        else:
            slip, fee_rate = section.slippage_rate, section.taker_fee_rate
            size_fraction = round(section.max_margin_fraction * frac, 4)
            leverage = float(section.max_leverage)
            margin = size_fraction * equity
            notional = margin * leverage
            fill = price * (1.0 + slip) if side == "long" \
                else price * (1.0 - slip)        # adverse by direction
            qty = notional / fill
            entry_fee = notional * fee_rate
            entry.update(size_fraction=size_fraction, margin=margin,
                         notional=notional, leverage=leverage, usd=notional,
                         qty=qty, fill=fill, entry_fee=entry_fee,
                         stop_price=jev_perps.stop_price_for(side, fill, section),
                         liq_price=jev_perps.liq_price_for(side, fill, leverage))
        entry["entry_slippage"] = abs(fill - price) * qty
        return entry
    except (TypeError, ValueError, KeyError):
        return None

def _close_result(entry, book, cfg, path, exit_price, row, cycles_held,
                  fill_as_is=False) -> dict:
    """Hypothetical close: adverse exit slippage + fee, exactly like the books."""
    section = _section(cfg, book)
    if book == "spot":
        slip, fee_rate = cfg.execution.slippage_rate, cfg.execution.spot_fee_rate
    else:
        slip, fee_rate = section.slippage_rate, section.taker_fee_rate
    side, qty = entry["side"], entry["qty"]
    if fill_as_is:                     # liquidation fills at the liq price
        fill = exit_price
    else:
        fill = exit_price * (1.0 - slip) if side == "long" \
            else exit_price * (1.0 + slip)
    exit_fee = abs(qty * fill) * fee_rate
    if path == PATH_LIQ:
        gross = -entry["margin"]       # whole margin lost (mirror _close)
    else:
        gross = jev_perps.unrealized_pnl(side, entry["fill"], fill, qty)
        if book == "perps":
            gross = max(gross, -entry["margin"])   # isolated margin floor
    net = gross - entry["entry_fee"] - exit_fee
    return {"path": path, "open": False, "cycles_held": cycles_held,
            "exit_ts_ms": row.get("ts_ms"), "exit_price": exit_price,
            "exit_fill": fill, "exit_fee": exit_fee,
            "exit_slippage": abs(fill - exit_price) * qty,
            "gross": gross, "net": net,
            "reason": row.get("reason") or path}


def _mark_result(entry, book, mark_price, mark_ts, cycles_held) -> dict:
    """Still open at data end -> mark at the last recorded price (labelled open)."""
    price = mark_price if mark_price is not None else entry["price"]
    gross = jev_perps.unrealized_pnl(entry["side"], entry["fill"], price,
                                     entry["qty"])
    if book == "perps":
        gross = max(gross, -entry["margin"])
    return {"path": PATH_OPEN, "open": True, "cycles_held": cycles_held,
            "exit_ts_ms": mark_ts, "exit_price": price, "exit_fill": None,
            "exit_fee": 0.0, "exit_slippage": 0.0,
            "gross": gross, "net": gross - entry["entry_fee"],
            "reason": "open at data end — marked, not closed (no exit costs)"}


def walk_forward_exit(decisions_after, entry, cfg, book) -> dict:
    """Walk the stored verdicts forward to the hypothetical exit.

    Mirrors the runtime rules exactly: perps stop/liq fire first and always
    (they bypass the min hold); then the M3 signal hysteresis — single
    >= hard bar or >= exit_consecutive_cycles cycles over the min bar, gated
    by min_hold_cycles. decisions_after must be same-book rows in time order.
    """
    section = _section(cfg, book)
    side = entry["side"]
    if side == "long":
        signal_key, min_bar, hard_bar = ("dump_0_100", section.exit_min_dump,
                                         section.exit_hard_dump)
    else:
        signal_key, min_bar, hard_bar = ("pump_0_100", section.exit_min_pump,
                                         section.exit_hard_pump)
    streak = 0
    age = 0
    mark_price = mark_ts = None
    for row in decisions_after:
        price = _f(row.get("price"))
        verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else {}
        signal_val = _f(verdict.get(signal_key))
        if price is None or price <= 0 or signal_val is None:
            continue
        age += 1
        mark_price, mark_ts = price, row.get("ts_ms")
        if book == "perps":
            if (side == "long" and price <= entry["liq_price"]) \
                    or (side == "short" and price >= entry["liq_price"]):
                return _close_result(entry, book, cfg, PATH_LIQ,
                                     entry["liq_price"], row, age,
                                     fill_as_is=True)
            if (side == "long" and price <= entry["stop_price"]) \
                    or (side == "short" and price >= entry["stop_price"]):
                return _close_result(entry, book, cfg, PATH_STOP, price, row, age)
        signal = signal_val >= min_bar
        streak = streak + 1 if signal else 0
        if age >= section.min_hold_cycles:
            if signal_val >= hard_bar:
                return _close_result(entry, book, cfg, PATH_SINGLE, price,
                                     row, age)
            if streak >= section.exit_consecutive_cycles:
                return _close_result(entry, book, cfg, PATH_2CONSEC, price,
                                     row, age)
    return _mark_result(entry, book, mark_price, mark_ts, age)

# ---------------------------------------------------------------------------
# aggregation math (pure)
# ---------------------------------------------------------------------------

def profit_factor(pnls):
    """gross profit / gross loss over net round-trip PnLs; None when undefined."""
    if not pnls:
        return None
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    if gl == 0:
        return math.inf if gp > 0 else None
    return gp / gl


def win_rate(pnls):
    if not pnls:
        return None
    return sum(1 for p in pnls if p > 0) / float(len(pnls))


def confidence_bucket(conf, cfg) -> str:
    """Entry-confidence bucket label (M3 tier edges from config)."""
    if conf is None:
        return "unknown"
    lo = cfg.spot.min_confidence
    t1, t2 = cfg.spot.tier_thresholds
    if conf < lo:
        return f"<{lo:.2f}"
    if conf < t1:
        return f"[{lo:.2f},{t1:.2f})"
    if conf < t2:
        return f"[{t1:.2f},{t2:.2f})"
    return f">={t2:.2f}"


def bucket_stats(trips, cfg) -> list:
    """Per-bucket count / win rate / avg net PnL / PF over entry confidences."""
    lo = cfg.spot.min_confidence
    t1, t2 = cfg.spot.tier_thresholds
    order = [f"[{lo:.2f},{t1:.2f})", f"[{t1:.2f},{t2:.2f})", f">={t2:.2f}",
             f"<{lo:.2f}", "unknown"]
    buckets = {label: [] for label in order}
    for trip in trips:
        label = confidence_bucket(trip.get("confidence"), cfg)
        buckets.setdefault(label, []).append(trip.get("net") or 0.0)
    rows = []
    for label in order:
        pnls = buckets.get(label, [])
        rows.append({"bucket": label, "count": len(pnls),
                     "win_rate": win_rate(pnls),
                     "avg_net": (sum(pnls) / len(pnls)) if pnls else None,
                     "pf": profit_factor(pnls)})
    return rows


def fanout_stats(rows, cfg) -> dict:
    """Fan-out usage + how often the 2nd sample flipped the single-sample call."""
    calls = ties = both = flip_veto = flip_pass = 0
    for row in rows:
        verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else {}
        w1 = _f(verdict.get("whipsaw_prob"))
        w2 = _f(verdict.get("whipsaw_prob_2"))
        if not (bool(row.get("fan_out")) or w2 is not None):
            continue
        if w1 is None:
            continue
        calls += 1
        section = _section(cfg, row.get("book"))
        gate = jev_gates.whipsaw_gate_name(w1, w2, True,
                                           section.entry_max_whipsaw)
        if gate == "whipsaw_fanout_tie":
            ties += 1
        if w2 is None:
            continue
        both += 1
        single_pass = w1 <= section.entry_max_whipsaw
        fanout_pass = gate is None
        if single_pass and not fanout_pass:
            flip_veto += 1
        if not single_pass and fanout_pass:
            flip_pass += 1
    return {"fanout_calls": calls, "ties": ties,
            "tie_rate": (ties / calls) if calls else None,
            "both_stored": both,
            "single_pass_fanout_veto": flip_veto,
            "single_fail_fanout_pass": flip_pass}


def hysteresis_stats(trips) -> dict:
    """Real exits by path + median hold time (minutes) per book and overall."""
    paths = {p: {book: 0 for book in BOOKS} for p in PATHS + (PATH_OTHER,)}
    holds = {book: [] for book in BOOKS}
    all_holds = []
    for trip in trips:
        path = trip.get("path") or PATH_OTHER
        paths.setdefault(path, {book: 0 for book in BOOKS})
        paths[path][trip.get("book")] += 1
        if trip.get("hold_ms") is not None:
            holds.setdefault(trip.get("book"), []).append(trip["hold_ms"])
            all_holds.append(trip["hold_ms"])

    def _median(values):
        return (statistics.median(values) / 60000.0) if values else None

    median_hold = {book: _median(holds.get(book, [])) for book in BOOKS}
    median_hold["all"] = _median(all_holds)
    return {"paths": paths, "median_hold_min": median_hold}

def veto_attribution(gate_rows, trades, cfg) -> dict:
    """Per-gate veto counts + counterfactual PnL (the "veto value" table).

    Population: skip decisions with a nonzero bitmask and no position open in
    that book at that time. Each evaluated counterfactual is attributed to
    EVERY gate in its bitmask — multi-gated rows overlap by design.
    """
    gates = {name: {"vetoes": 0, "counterfactual_pnl": 0.0, "avg_per_veto": None}
             for name in GATE_NAMES}
    evaluated = no_side = pos_open = 0
    groups = {}
    for row in gate_rows:
        groups.setdefault((row.get("book"), row.get("symbol")), []).append(row)
    for (book, _symbol), rows in sorted(groups.items(),
                                        key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        rows = sorted(rows, key=lambda r: (r.get("ts_ms") or 0))
        intervals = position_intervals(trades, book, _symbol)
        for i, row in enumerate(rows):
            mask = int(row.get("veto_bitmask") or 0)
            if row.get("action") != "skip" or mask == 0:
                continue
            names = [n for n in bit_names(mask) if n in gates]
            if position_open_at(intervals, row.get("ts_ms")):
                pos_open += 1
                continue
            entry = counterfactual_entry(row, cfg)
            if entry is None:
                no_side += 1            # no side signal: the veto cost nothing
                for name in names:
                    gates[name]["vetoes"] += 1
                continue
            res = walk_forward_exit(rows[i + 1:], entry, cfg, book)
            evaluated += 1
            for name in names:
                gates[name]["vetoes"] += 1
                gates[name]["counterfactual_pnl"] += res["net"]
    for g in gates.values():
        if g["vetoes"]:
            g["avg_per_veto"] = g["counterfactual_pnl"] / g["vetoes"]
    return {"gates": gates, "evaluated": evaluated, "no_side_signal": no_side,
            "position_open": pos_open}


def re_tune_proposals(gates, equity_ref, total_trips) -> list:
    """Deterministic, evidence-quoted heuristics ONLY. Never auto-applied.

    A gate whose counterfactual PnL per 10 vetoes is strongly negative
    (< -2% of current equity) is PROPOSAL tighten (the vetoed entries would
    have lost money — the gate earns its keep); strongly positive is
    PROPOSAL relax. Below MIN_TRIPS_FOR_PROPOSALS round trips: no proposals.
    """
    if total_trips < MIN_TRIPS_FOR_PROPOSALS:
        return []
    threshold = PROPOSAL_EQUITY_FRACTION * (equity_ref or 0.0)
    proposals = []
    for name in GATE_NAMES:
        g = gates.get(name) or {}
        vetoes = g.get("vetoes") or 0
        avg = g.get("avg_per_veto")
        if not vetoes or avg is None:
            continue
        per_ten = avg * PROPOSAL_VETOES
        if per_ten < -threshold:
            direction = "tighten"
        elif per_ten > threshold:
            direction = "relax"
        else:
            continue
        proposals.append({
            "gate": name, "direction": direction, "vetoes": vetoes,
            "counterfactual_pnl": g["counterfactual_pnl"], "avg_per_veto": avg,
            "per_10_vetoes": per_ten, "threshold": threshold,
        })
    return proposals

# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def build_report(gate_rows, trades, cfg, db_label="runtime/jevelin.db",
                 replay_stats=None, since=None, jev_verdicts=0):
    """Markdown report + headline metrics dict. Pure over the loaded rows."""
    now = datetime.now(BANGKOK)
    trips_by_book = {book: round_trips(trades, book) for book in BOOKS}
    all_trips = [t for book in BOOKS for t in trips_by_book[book]]
    total_trips = len(all_trips)
    stats = {book: trade_stats([t for t in trades if t.get("book") == book])
             for book in BOOKS}
    conf_by_id = {(r.get("decision_id"), r.get("book")):
                  _f((r.get("verdict") or {}).get("confidence"))
                  for r in gate_rows}
    regime_by_id = {(r.get("decision_id"), r.get("book")):
                    r.get("regime") or "n/a" for r in gate_rows}
    for trip in all_trips:
        key = (trip.get("decision_id"), trip.get("book"))
        trip["confidence"] = conf_by_id.get(key)
        trip["regime"] = regime_by_id.get(key, "n/a")
    equity = {}
    for book in BOOKS:
        rows = [r for r in gate_rows
                if r.get("book") == book and r.get("equity") is not None]
        equity[book] = max(rows, key=lambda r: r.get("ts_ms") or 0)["equity"] \
            if rows else None
    equity_ref = sum(e for e in equity.values() if e is not None) or None
    stamps = [r.get("ts_ms") for r in gate_rows if r.get("ts_ms") is not None] \
        + [t.get("ts_ms") for t in trades if t.get("ts_ms") is not None]
    t_min, t_max = (min(stamps), max(stamps)) if stamps else (None, None)

    attribution = veto_attribution(gate_rows, trades, cfg)
    fanout = fanout_stats(gate_rows, cfg)
    hyster = hysteresis_stats(all_trips)
    proposals = re_tune_proposals(attribution["gates"], equity_ref, total_trips)

    out = []
    out.append("# Jevelin M4 calibration report")
    out.append(f"generated: {now.strftime('%Y-%m-%d %H:%M:%S')} Asia/Bangkok"
               f" · store: {db_label}")
    out.append(f"data range: {_ts_bkk(t_min)} .. {_ts_bkk(t_max)} (Asia/Bangkok)"
               + (f" · since: {since}" if since else ""))
    out.append("accounting: M0 fill model — fees + adverse slippage per side; "
               "net = realized - funding. Counterfactuals are ESTIMATES from "
               "recorded cycle prices (funding excluded — rates are not stored; "
               "open positions are marked, not closed).")
    out.append("")

    # -- 1. overview ------------------------------------------------------
    out.append("## 1. Overview")
    out.append("")
    out.append("| book | round trips | gross PnL | fees | slippage | funding | "
               "net PnL | PF | win rate | current equity |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for book in BOOKS:
        s = stats[book]
        pnls = [t["net"] for t in trips_by_book[book]]
        out.append(
            f"| {book} | {s['trips']} | {_fmt(s['gross'])} | {_fmt(s['fees'])} | "
            f"{_fmt(s['slippage'])} | {_fmt(s['funding'])} | {_fmt(s['net'])} | "
            f"{_fmt_pf(profit_factor(pnls))} | "
            f"{_fmt(win_rate(pnls)) if pnls else 'n/a'} | "
            f"{_fmt(equity[book])} |")
    out.append("")
    out.append(f"gate decisions analyzed: {len(gate_rows)} "
               f"({sum(1 for r in gate_rows if r.get('book') == 'spot')} spot / "
               f"{sum(1 for r in gate_rows if r.get('book') == 'perps')} perps)"
               f" · Jev verdict rows: {jev_verdicts} · trade fills: {len(trades)}")
    # cross-check: same rows, M1 accounting per (book, symbol) (the numbers
    # jev_replay.py --summary prints on the store side)
    stats_by_group = {}
    for t in trades:
        stats_by_group.setdefault(
            (t.get("book"), str(t.get("symbol") or "unknown")), []).append(t)
    stats_by_group = {g: trade_stats(rows) for g, rows in stats_by_group.items()}
    groups = sorted(set(stats_by_group) | set(replay_stats or {})) \
        or [(book, "unknown") for book in BOOKS]
    compared = 0
    matched = 0
    for group in groups:
        replay = (replay_stats or {}).get(group) or {}
        s = stats_by_group.get(group) or trade_stats([])
        for key in ("trips", "gross", "fees", "slippage", "funding", "net"):
            compared += 1
            if key in replay and abs(float(replay[key])
                                     - float(s[key])) <= 1e-6:
                matched += 1
    verdict = "OK" if compared and matched == compared else "DIFF"
    out.append(f"cross-check vs jev_replay.py --summary accounting "
               f"(same rows, per book/symbol): {matched}/{compared} values {verdict}")
    out.append("")

    # -- 2. regime distribution ------------------------------------------
    out.append("## 2. Regime distribution")
    out.append("")
    out.append("| book | regime | decisions | round trips | net PnL | PF |")
    out.append("|---|---|---|---|---|---|")
    for book in BOOKS:
        regimes = sorted({r.get("regime") or "n/a" for r in gate_rows
                          if r.get("book") == book}
                         | {t["regime"] for t in trips_by_book[book]})
        regimes = [rg for rg in regimes if rg != "n/a"] + \
                  [rg for rg in regimes if rg == "n/a"]
        for regime in regimes:
            n_dec = sum(1 for r in gate_rows if r.get("book") == book
                        and (r.get("regime") or "n/a") == regime)
            trips_r = [t for t in trips_by_book[book] if t["regime"] == regime]
            pnls = [t["net"] for t in trips_r]
            out.append(f"| {book} | {regime} | {n_dec} | {len(trips_r)} | "
                       f"{_fmt(sum(pnls))} | {_fmt_pf(profit_factor(pnls))} |")
    out.append("")

    # -- 3. per-gate veto attribution ------------------------------------
    out.append("## 3. Per-gate veto attribution")
    out.append("")
    out.append(f"population: {sum(g['vetoes'] for g in attribution['gates'].values())}"
               f" vetoes over skip decisions with nonzero bitmask (counterfactuals"
               f" evaluated: {attribution['evaluated']}, no qualifying side signal"
               f" (valued 0): {attribution['no_side_signal']}, position open —"
               f" skipped: {attribution['position_open']})")
    out.append("")
    out.append("| gate | vetoes | counterfactual net PnL (USD) | avg per veto (USD) |")
    out.append("|---|---|---|---|")
    shown = 0
    for name in GATE_NAMES:
        g = attribution["gates"][name]
        if not g["vetoes"]:
            continue
        shown += 1
        out.append(f"| {name} | {g['vetoes']} | "
                   f"{_fmt(g['counterfactual_pnl'])} | {_fmt(g['avg_per_veto'])} |")
    if not shown:
        out.append("| (no entry vetoes recorded) | 0 | 0.00 | n/a |")
    out.append("")
    out.append("multi-gated — attribution overlaps by design: one counterfactual "
               "is summed into EVERY gate of its decision's bitmask. 'Veto value' "
               "= what the vetoed entries would have netted had they not been "
               "vetoed (estimate; see the header note).")
    out.append("")

    # -- 4. confidence calibration curve ---------------------------------
    out.append("## 4. Confidence calibration curve")
    out.append("")
    joined = sum(1 for t in all_trips if t.get("confidence") is not None)
    out.append(f"round trips joined to their entry decisions via decision_id: "
               f"{joined}/{len(all_trips)} (unjoined land in 'unknown')")
    out.append("")
    out.append("| entry confidence | entries | win rate | avg net PnL | PF |")
    out.append("|---|---|---|---|---|")
    for row in bucket_stats(all_trips, cfg):
        out.append(f"| {row['bucket']} | {row['count']} | "
                   f"{_fmt(row['win_rate'])} | {_fmt(row['avg_net'])} | "
                   f"{_fmt_pf(row['pf'])} |")
    out.append("")

    # -- 5. fan-out stats -------------------------------------------------
    out.append("## 5. Fan-out stats")
    out.append("")
    out.append("| metric | value |")
    out.append("|---|---|")
    out.append(f"| fan-out calls | {fanout['fanout_calls']} |")
    out.append(f"| ties (split samples / unusable 2nd sample) | {fanout['ties']} |")
    out.append(f"| tie rate | {_fmt(fanout['tie_rate'])} |")
    out.append(f"| rows with both samples stored | {fanout['both_stored']} |")
    out.append(f"| single sample would pass / fan-out vetoed | "
               f"{fanout['single_pass_fanout_veto']} |")
    out.append(f"| single sample would fail / fan-out passed | "
               f"{fanout['single_fail_fanout_pass']} |")
    out.append("")
    out.append("flip counting covers only rows with both whipsaw samples stored; "
               "a tie is fail-closed (veto), so the fan-out can only ever add "
               "vetoes — the 'flip to pass' count is structurally 0.")
    out.append("")

    # -- 6. hysteresis stats ---------------------------------------------
    out.append("## 6. Hysteresis stats")
    out.append("")
    out.append("| exit path | spot | perps | total |")
    out.append("|---|---|---|---|")
    for path in (PATH_2CONSEC, PATH_SINGLE, PATH_STOP, PATH_LIQ, PATH_OTHER):
        counts = hyster["paths"].get(path) or {}
        spot_n = counts.get("spot", 0)
        perps_n = counts.get("perps", 0)
        out.append(f"| {path} | {spot_n} | {perps_n} | {spot_n + perps_n} |")
    med = hyster["median_hold_min"]
    out.append("")
    out.append(f"median hold time: spot {_fmt(med['spot'])} min · "
               f"perps {_fmt(med['perps'])} min · all {_fmt(med['all'])} min")
    out.append("")

    # -- 7. re-tune proposals --------------------------------------------
    out.append("## 7. Re-tune proposals")
    out.append("")
    if total_trips < MIN_TRIPS_FOR_PROPOSALS:
        out.append(f"insufficient data for proposals ({total_trips} round trips "
                   f"< {MIN_TRIPS_FOR_PROPOSALS}) — no re-tune proposals "
                   f"generated.")
    else:
        threshold = PROPOSAL_EQUITY_FRACTION * (equity_ref or 0.0)
        out.append(f"threshold: |counterfactual PnL per "
                   f"{PROPOSAL_VETOES} vetoes| > "
                   f"{PROPOSAL_EQUITY_FRACTION:.0%} of current equity "
                   f"({_fmt(equity_ref)} USD) = {_fmt(threshold)} USD · "
                   f"based on {total_trips} round trips")
        out.append("")
        if not proposals:
            out.append(f"no gate crosses the "
                       f"±{PROPOSAL_EQUITY_FRACTION:.0%}-of-equity-per-"
                       f"{PROPOSAL_VETOES}-vetoes threshold — nothing to propose.")
        for p in proposals:
            out.append(
                f"- PROPOSAL {p['direction']}: {p['gate']} — evidence: "
                f"{p['vetoes']} vetoes, counterfactual net PnL "
                f"{_fmt(p['counterfactual_pnl'])} USD (avg "
                f"{_fmt(p['avg_per_veto'])} per veto; per-{PROPOSAL_VETOES}-veto "
                f"value {_fmt(p['per_10_vetoes'])} vs threshold "
                f"{_fmt(p['threshold'])} USD).")
        if proposals:
            out.append("")
            out.append("**HUMAN APPROVAL REQUIRED — proposals only, never "
                       "auto-applied.** Any config change is a separate, "
                       "explicitly approved step (config/v2.yaml + supervisor "
                       "restart); this report changes nothing.")
    out.append("")

    metrics = {
        "generated": now.isoformat(),
        "since": since,
        "db": db_label,
        "data_range_ms": [t_min, t_max],
        "gate_decisions": len(gate_rows),
        "jev_verdicts": int(jev_verdicts),
        "trades": len(trades),
        "total_round_trips": total_trips,
        "books": {
            book: {
                "round_trips": stats[book]["trips"],
                "gross": stats[book]["gross"],
                "net": stats[book]["net"],
                "pf": None if (lambda pf: pf is None or math.isinf(pf))(
                    profit_factor([t["net"] for t in trips_by_book[book]]))
                else profit_factor([t["net"] for t in trips_by_book[book]]),
                "win_rate": win_rate([t["net"] for t in trips_by_book[book]]),
                "equity": equity[book],
            } for book in BOOKS
        },
        "counterfactuals": {
            "evaluated": attribution["evaluated"],
            "no_side_signal": attribution["no_side_signal"],
            "position_open": attribution["position_open"],
            "gates": {name: g for name, g in attribution["gates"].items()
                      if g["vetoes"]},
        },
        "fanout": fanout,
        "proposals": proposals,
    }
    return "\n".join(out), metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(db_path, cfg, out_path=None, since=None) -> int:
    """Load the store, build the report, record one calibration_runs row.

    Fail-open: a missing or empty store is a clear error + non-zero exit —
    never fabricated numbers, never a report from nothing.
    """
    db = Path(db_path)
    if not db.exists():
        print(f"error: store not found: {db} — run scripts/jev_import.py first")
        return 1
    try:
        cutoff = since_ms(since)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    conn = jev_store.connect(str(db))
    try:
        gate_rows = gate_rows_from_store(conn, cutoff)
        trades = trade_rows_from_store(conn, cutoff)
        if not gate_rows and not trades:
            print(f"error: no recorded data in store {db} — run "
                  f"scripts/jev_import.py first (nothing to calibrate)")
            return 1
        try:
            jev_verdicts = conn.execute(
                "SELECT COUNT(*) FROM decisions").fetchone()[0]
        except Exception:
            jev_verdicts = 0
        raw_by_group = {}
        for r in jev_store.list_trades(conn):
            ts = _f(r.get("ts"))
            if cutoff is not None and (ts is None or ts < cutoff):
                continue
            key = (r.get("book"), str(r.get("symbol") or "unknown"))
            raw_by_group.setdefault(key, []).append(r)
        if not raw_by_group:  # legacy empty-data semantics: both books, zero rows
            raw_by_group = {(book, "unknown"): [] for book in BOOKS}
        replay_stats = {group: jev_replay._stats_from_rows(rows)
                        for group, rows in raw_by_group.items()}
        text, metrics = build_report(gate_rows, trades, cfg, db_label=str(db),
                                     replay_stats=replay_stats, since=since,
                                     jev_verdicts=jev_verdicts)
        print(text)
        if out_path:
            Path(out_path).write_text(text + "\n")
            print(f"report written to {out_path}")
        jev_store.insert_calibration_run(
            conn, {"out": out_path, "since": since, "db": str(db)}, metrics)
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="M4 calibration report from the store (reads only; one "
                    "calibration_runs row per run).")
    p.add_argument("--out", help="also write the markdown report to this file")
    p.add_argument("--since", help="YYYY-MM-DD (Asia/Bangkok): only data at or "
                                   "after this day start")
    p.add_argument("--db", default="runtime/jevelin.db", help="store path")
    args = p.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"error: {exc}")
        return 1
    return run(args.db, cfg, out_path=args.out, since=args.since)


if __name__ == "__main__":
    raise SystemExit(main())











