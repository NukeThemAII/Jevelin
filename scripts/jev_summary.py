#!/usr/bin/env python3
"""jev_summary — offline replay summary over the paper books' JSONL logs.

Reads (never writes, never touches the network):
  * ``runtime/*.trades.jsonl``        — per-fill trade rows (book/fees/slippage/realized)
  * ``runtime/*decisions*.jsonl``     — gate-decision rows with ``veto_bitmask``
    (covers ``runtime/jev_decisions.jsonl`` and ``runtime/paper_decisions.jsonl``)

Prints per book: round trips, gross PnL (quote-price, before costs), fees paid,
slippage paid, funding paid, net PnL and current equity (from the book's state
JSON: spot marked at entry fill, perps realized equity), plus a per-gate veto
count table derived from the bitmask (M0 / F-P1-4).

Usage: .venv/bin/python scripts/jev_summary.py [--runtime-dir runtime]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import GATE_FLAG, GATE_NAMES  # noqa: E402

BOOKS = ("spot", "perps")
BANGKOK = ZoneInfo("Asia/Bangkok")


def _read_jsonl(path: Path) -> list:
    rows = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        pass
    return rows


def _book_of(row: dict, path: Path) -> str:
    book = row.get("book")
    if book in BOOKS:
        return book
    return "perps" if "perps" in path.name else "spot"  # pre-M0 rows: filename heuristic


def _symbol_of(row: dict, path: Path) -> str:
    symbol = row.get("symbol")
    return str(symbol) if symbol else "unknown"


def _group_key(row: dict, path: Path) -> tuple:
    """(book, symbol) group — M5: summary tables group by (book, symbol)."""
    return _book_of(row, path), _symbol_of(row, path)


def _is_closing(row: dict) -> bool:
    action = row.get("action")
    if action in ("exit", "exited", "stop_loss", "liquidated"):
        return True
    return row.get("side") == "sell"


def _equity_from_state(trades_path: Path, book: str):
    """Current equity from the book's state JSON, honestly labelled."""
    state_path = Path(str(trades_path)[: -len(".trades.jsonl")])
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return None, "state unreadable"
    if not isinstance(data, dict):
        return None, "state unreadable"
    if book == "perps":
        eq = data.get("equity")
        if isinstance(eq, (int, float)):
            return float(eq), "realized equity"
        return None, "state unreadable"
    cash = data.get("cash")
    if not isinstance(cash, (int, float)):
        return None, "state unreadable"
    equity = float(cash)
    pos = data.get("position")
    if isinstance(pos, dict):
        try:
            equity += float(pos["qty"]) * float(pos["entry_price"])
        except (KeyError, TypeError, ValueError):
            pass
        return equity, "marked at entry fill"
    return equity, "flat"


def _collect(runtime_dir: Path):
    """{((book, symbol): [(path, row)])} for trades and {(book, symbol): [row]}
    for gate decisions — one group per (book, symbol) (M5 multi-pair)."""
    trades = {}
    for path in sorted(runtime_dir.glob("*.trades.jsonl")):
        for row in _read_jsonl(path):
            trades.setdefault(_group_key(row, path), []).append((path, row))
    decisions = {}
    for path in sorted(runtime_dir.glob("*decisions*.jsonl")):
        for row in _read_jsonl(path):
            if isinstance(row.get("veto_bitmask"), int):
                decisions.setdefault(_group_key(row, path), []).append(row)
    return trades, decisions


def _book_stats(rows) -> dict:
    realized = fees = slippage = funding = 0.0
    trips = 0
    for _path, row in rows:
        realized += float(row.get("realized_pnl") or 0.0)
        fees += float(row.get("fees") or 0.0)
        slippage += float(row.get("slippage") or 0.0)
        funding += float(row.get("funding_paid") or 0.0)
        if _is_closing(row):
            trips += 1
    net = realized - funding  # funding is a real cost, kept out of fees/slippage
    gross = realized + fees + slippage  # quote-price PnL before costs
    return {"trips": trips, "gross": gross, "fees": fees, "slippage": slippage,
            "funding": funding, "net": net}


def _group_order(key) -> tuple:
    """Canonical group order: spot before perps, then symbol."""
    book, symbol = key
    return (BOOKS.index(book) if book in BOOKS else len(BOOKS), symbol)


def _print_book(book: str, symbol: str, rows) -> None:
    stats = _book_stats(rows)
    paths = {path for path, _ in rows}
    equity, label = (None, "no state file")
    if paths:
        equity, label = _equity_from_state(sorted(paths)[0], book)
    heading = f"{book} {symbol}:" if symbol and symbol != "unknown" else f"{book}:"
    print(heading)
    print(f"  round trips:      {stats['trips']}")
    print(f"  gross PnL:        {stats['gross']:.2f} USD")
    print(f"  fees paid:        {stats['fees']:.2f} USD")
    print(f"  slippage paid:    {stats['slippage']:.2f} USD")
    print(f"  funding paid:     {stats['funding']:.2f} USD")
    print(f"  net PnL:          {stats['net']:.2f} USD")
    if equity is None:
        print(f"  current equity:   n/a ({label})")
    else:
        print(f"  current equity:   {equity:.2f} USD ({label})")


def _print_veto_table(decisions) -> None:
    """Per-gate veto counts, one column per (book, symbol) group (M5)."""
    groups = sorted(decisions, key=_group_order)
    if not groups:
        groups = [(book, "unknown") for book in BOOKS]
    labels = [f"{book}/{symbol}" if symbol != "unknown" else book
              for book, symbol in groups]
    print("per-gate veto counts (from veto_bitmask):")
    print("  gate".ljust(22)
          + "".join(label.rjust(14) for label in labels) + "total".rjust(8))
    for name in GATE_NAMES:
        bit = int(GATE_FLAG[name])
        counts = [sum(1 for row in decisions.get(group, [])
                      if int(row.get("veto_bitmask") or 0) & bit)
                  for group in groups]
        total = sum(counts)
        print(f"  {name}".ljust(22)
              + "".join(str(c).rjust(14) for c in counts)
              + str(total).rjust(8))


def _print_regime_distribution(decisions) -> None:
    """M3: counts by regime from the gate-decision records (cheap, read-only)."""
    counts = {}
    for rows in decisions.values():
        for row in rows:
            regime = row.get("regime") or "n/a"
            counts[regime] = counts.get(regime, 0) + 1
    if not counts:
        return
    parts = "  ".join(f"{name}={counts[name]}" for name in sorted(counts))
    print(f"regime distribution (decision records): {parts}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Offline M0 replay summary (reads logs only).")
    p.add_argument("--runtime-dir", default="runtime",
                   help="directory holding *.trades.jsonl and *decisions*.jsonl")
    args = p.parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    trades, decisions = _collect(runtime_dir)
    stamp = datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Jevelin M0 summary — {runtime_dir}/ (generated {stamp} Asia/Bangkok)")
    print("note: rows written before M0 carry no fee/slippage fields and count as 0.")
    print()
    groups = sorted(trades, key=_group_order) or [
        (book, "unknown") for book in BOOKS]
    for book, symbol in groups:
        _print_book(book, symbol, trades.get((book, symbol), []))
        print()
    _print_veto_table(decisions)
    _print_regime_distribution(decisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())