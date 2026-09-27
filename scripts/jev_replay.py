#!/usr/bin/env python3
"""jev_replay — replay statistics from the M1 store, cross-checked on JSONL.

``--summary`` (default) recomputes per-book stats over the STORED trade rows
with the M0 fill accounting and prints them side by side with the numbers
``scripts/jev_summary.py`` derives from the raw JSONL logs:

  gross = realized + fees + slippage   (quote-price PnL before costs)
  net   = realized - funding           (funding is a real cost, not a fee)
  a round trip = one closing row (action exit/exited/stop_loss/liquidated,
                 or side == "sell")

Rows written before M0 carry no fee/slippage fields and count as 0, exactly
like jev_summary. The invariant: store replay == JSONL summary. Exit code 0
only when every compared value matches — run scripts/jev_import.py first if
the store is stale.

``--dump decisions|trades`` prints a paged listing (--limit/--offset) for
eyeballing. Reads only; it never writes logs or the store.

Usage: .venv/bin/python scripts/jev_replay.py [--summary | --dump decisions|trades]
       [--limit N] [--offset N] [--runtime-dir runtime] [--db runtime/jevelin.db]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_store  # noqa: E402
import jev_summary  # noqa: E402

BOOKS = ("spot", "perps")
BANGKOK = ZoneInfo("Asia/Bangkok")
METRICS = (("round trips", "trips"), ("gross PnL", "gross"), ("fees", "fees"),
           ("slippage", "slippage"), ("funding", "funding"), ("net PnL", "net"))
CLOSE_ACTIONS = ("exit", "exited", "stop_loss", "liquidated")
EPSILON = 1e-9


def _raw_row(store_row: dict) -> dict:
    try:
        obj = json.loads(store_row.get("raw_json") or "")
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def _is_closing(store_row: dict) -> bool:
    raw = _raw_row(store_row)
    if raw.get("action") in CLOSE_ACTIONS:
        return True
    return (raw.get("side") or store_row.get("side")) == "sell"


def _stats_from_rows(rows) -> dict:
    realized = fees = slippage = funding = 0.0
    trips = 0
    for row in rows:
        realized += float(row.get("realized_pnl") or 0.0)
        fees += float(row.get("fees") or 0.0)
        slippage += float(row.get("slippage") or 0.0)
        funding += float(row.get("funding_paid") or 0.0)
        if _is_closing(row):
            trips += 1
    return {"trips": trips, "gross": realized + fees + slippage, "fees": fees,
            "slippage": slippage, "funding": funding, "net": realized - funding}


def store_book_stats(conn) -> dict:
    """Per-book M0 accounting over the stored trades table."""
    stats = {book: _stats_from_rows([]) for book in BOOKS}
    for book in BOOKS:
        rows = jev_store.list_trades(conn, book=book)
        stats[book] = _stats_from_rows(rows)
    return stats


def jsonl_book_stats(runtime_dir) -> dict:
    """Per-book M0 accounting straight from the JSONL logs (via jev_summary)."""
    trades, _decisions = jev_summary._collect(Path(runtime_dir))
    return {book: jev_summary._book_stats(trades[book]) for book in BOOKS}


def compare_summary(conn, runtime_dir) -> list:
    """(book, metric, store, jsonl, match) rows over the six M0 stats."""
    store = store_book_stats(conn)
    logged = jsonl_book_stats(runtime_dir)
    out = []
    for book in BOOKS:
        for label, key in METRICS:
            a, b = store[book][key], logged[book][key]
            match = abs(float(a) - float(b)) < EPSILON
            out.append((book, label, a, b, match))
    return out

def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return f"{float(value):.4f}"


def print_summary(db_path, runtime_dir) -> int:
    """Print the store-vs-JSONL side-by-side table. 0 iff all values match."""
    runtime_dir = Path(runtime_dir)
    conn = jev_store.connect(str(db_path))
    try:
        rows = compare_summary(conn, runtime_dir)
    finally:
        conn.close()
    stamp = datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Jevelin M1 replay summary — store {db_path} vs JSONL {runtime_dir}/ "
          f"(generated {stamp} Asia/Bangkok)")
    print("accounting: M0 fill math — gross = realized + fees + slippage; "
          "net = realized - funding")
    print("note: rows written before M0 carry no fee/slippage fields and count as 0.")
    print()
    print(f"{'book':<8}{'metric':<14}{'store':>12}{'jsonl':>12}  match")
    for book, label, a, b, match in rows:
        print(f"{book:<8}{label:<14}{_fmt(a):>12}{_fmt(b):>12}"
              f"  {'OK' if match else 'DIFF'}")
    print()
    bad = sum(1 for *_x, match in rows if not match)
    if bad:
        print(f"verdict: MISMATCH — {bad} of {len(rows)} values differ "
              f"(store stale? run scripts/jev_import.py)")
        return 1
    print(f"verdict: store replay == JSONL summary ({len(rows)} values compared)")
    return 0


_DUMP_COLUMNS = {
    "decisions": ("id", "ts", "decision_id", "symbol", "cost", "latency_ms", "ok"),
    "trades": ("id", "ts", "book", "symbol", "side", "price", "qty", "notional",
               "fees", "slippage", "realized_pnl", "funding_paid"),
}


def dump(conn, table, limit=20, offset=0) -> int:
    """Paged listing of a store table for eyeballing."""
    cols = _DUMP_COLUMNS[table]
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM {table} ORDER BY ts, id LIMIT ? OFFSET ?",
        (int(limit), int(offset))).fetchall()
    print(f"{table} — showing {offset + 1 if rows else 0}-{offset + len(rows)} "
          f"of {total} (use --limit/--offset to page)")
    print("  " + "  ".join(c.rjust(12) for c in cols)
          + ("  reason" if table == "trades" else "  verdict"))
    for row in rows:
        cells = "  ".join(_fmt(row[c]).rjust(12) for c in cols)
        extra = row["reason"] if table == "trades" else row["verdict_json"]
        extra = (extra or "")
        if len(extra) > 40:
            extra = extra[:37] + "..."
        print(f"  {cells}  {extra}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="M1 store replay + JSONL cross-check (reads only).")
    p.add_argument("--summary", action="store_true",
                   help="recompute per-book stats and compare with jev_summary")
    p.add_argument("--dump", choices=("decisions", "trades"),
                   help="paged listing of a store table")
    p.add_argument("--limit", type=int, default=20, help="dump page size")
    p.add_argument("--offset", type=int, default=0, help="dump page offset")
    p.add_argument("--runtime-dir", default="runtime",
                   help="directory holding the JSONL logs")
    p.add_argument("--db", default=None,
                   help="store path (default: <runtime-dir>/jevelin.db)")
    args = p.parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    db_path = Path(args.db) if args.db else runtime_dir / "jevelin.db"
    if args.dump:
        conn = jev_store.connect(str(db_path))
        try:
            return dump(conn, args.dump, args.limit, args.offset)
        finally:
            conn.close()
    return print_summary(db_path, runtime_dir)


if __name__ == "__main__":
    raise SystemExit(main())