#!/usr/bin/env python3
"""jev_import — idempotent JSONL -> SQLite importer (M1).

Sources are READ-ONLY (a live paper loop owns them):
  runtime/jev_decisions.jsonl  -> decisions  (jev_client.JevClient audit rows)
  runtime/*.trades.jsonl       -> trades     (spot + perps fill rows)

Lossless: every imported row keeps the verbatim source line in ``raw_json``.
Pre-M0 rows (no ``fees``/``slippage``/``decision_id``/...) import with NULLs;
rows without a ``decision_id`` get the stable synthetic id
``synth-<ts>-<line>`` (``line`` = 1-based line number in the source file, so
re-imports derive the same id). ``reason`` is stored as '' when absent (no
reason recorded; the dedup key uses COALESCE(reason, '')).

Re-running never duplicates: upsert keys are
  trades     (book, ts, side, COALESCE(reason, ''))
  decisions  (decision_id, ts)
Unparseable lines are skipped and counted at exit — the import never crashes.

Field mapping (anything else lives on verbatim in raw_json):
  trades    ts = ts_ms (epoch ms), notional = notional | usd, book = row book
            if valid else filename heuristic (same rule as jev_summary._book_of
            so the replay cross-check is apples-to-apples).
  decisions ts = ts (epoch s), cost = raw.usage.cost, ok = not error,
            verdict_json = raw.answers, state_json = {"state_sha256": ...}
            (the client logs a state hash, never the state body).

Usage: .venv/bin/python scripts/jev_import.py [--runtime-dir runtime]
       [--db runtime/jevelin.db]
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

BOOKS = ("spot", "perps")
BANGKOK = ZoneInfo("Asia/Bangkok")
DECISIONS_FILE = "jev_decisions.jsonl"


def _synthetic_id(ts, seq) -> str:
    return f"synth-{ts}-{seq}"


def _book_of(obj: dict, path: Path) -> str:
    book = obj.get("book")
    if book in BOOKS:
        return book
    return "perps" if "perps" in path.name else "spot"


def _f(obj: dict, key):
    value = obj.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def trade_row(obj: dict, raw_line: str, seq: int, path: Path) -> dict:
    """Map one trades JSONL object to the store schema (lossless via raw_json)."""
    ts = obj.get("ts_ms")
    if not isinstance(ts, (int, float)):
        ts_val = obj.get("ts")
        ts = float(ts_val) * 1000.0 if isinstance(ts_val, (int, float)) else None
    notional = obj.get("notional")
    if not isinstance(notional, (int, float)):
        notional = obj.get("usd")
    return {
        "decision_id": obj.get("decision_id") or _synthetic_id(ts, seq),
        "ts": float(ts) if ts is not None else None,
        "book": _book_of(obj, path),
        "symbol": obj.get("symbol"),
        "side": obj.get("side"),
        "price": _f(obj, "price"),
        "qty": _f(obj, "qty"),
        "notional": float(notional) if isinstance(notional, (int, float)) else None,
        "fees": _f(obj, "fees"),
        "slippage": _f(obj, "slippage"),
        "realized_pnl": _f(obj, "realized_pnl"),
        "funding_paid": _f(obj, "funding_paid"),
        "reason": obj.get("reason") or "",  # '' = no reason recorded (dedup key)
        "veto_bitmask": obj.get("veto_bitmask")
        if isinstance(obj.get("veto_bitmask"), int) else None,
        "raw_json": raw_line,
    }


def decision_row(obj: dict, raw_line: str, seq: int) -> dict:
    """Map one jev_decisions.jsonl object to the store schema."""
    ts = obj.get("ts")
    if not isinstance(ts, (int, float)):
        ts_ms = obj.get("ts_ms")
        ts = float(ts_ms) / 1000.0 if isinstance(ts_ms, (int, float)) else None
    raw = obj.get("raw") if isinstance(obj.get("raw"), dict) else {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    cost = usage.get("cost")
    if not isinstance(cost, (int, float)):
        cost = obj.get("cost") if isinstance(obj.get("cost"), (int, float)) else None
    if "state" in obj:
        state_json = json.dumps(obj["state"])
    elif obj.get("state_sha256") is not None:
        state_json = json.dumps({"state_sha256": obj.get("state_sha256")})
    else:
        state_json = None
    answers = raw.get("answers")
    verdict_json = json.dumps(answers) if answers is not None \
        else json.dumps(obj["verdict"]) if isinstance(obj.get("verdict"), dict) \
        else None
    latency = obj.get("latency_ms")
    return {
        "ts": float(ts) if ts is not None else None,
        "decision_id": obj.get("decision_id") or _synthetic_id(ts, seq),
        "symbol": obj.get("symbol"),
        "state_json": state_json,
        "verdict_json": verdict_json,
        "cost": float(cost) if cost is not None else None,
        "latency_ms": float(latency) if isinstance(latency, (int, float)) else None,
        "ok": 0 if obj.get("error") else 1,
        "raw_json": raw_line,
    }

def _read_lines(path):
    """Yield (seq, raw_line) for non-blank lines; seq = 1-based file line number."""
    try:
        text = Path(path).read_text()
    except OSError:
        return
    for seq, raw in enumerate(text.split("\n"), 1):
        if raw.strip():
            yield seq, raw


def _blank_stats(kind, path, book=None) -> dict:
    return {"kind": kind, "file": str(path), "book": book, "lines": 0,
            "parsed": 0, "new": 0, "dupe": 0, "bad": 0}


def import_trades_file(conn, path) -> dict:
    """Import one *.trades.jsonl into trades. Never raises on bad lines."""
    path = Path(path)
    stats = _blank_stats("trades", path)
    for seq, raw in _read_lines(path):
        stats["lines"] += 1
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("line is not a JSON object")
        except ValueError:
            stats["bad"] += 1
            continue
        stats["parsed"] += 1
        row = trade_row(obj, raw, seq, path)
        if stats["book"] is None:
            stats["book"] = row["book"]
        if jev_store.upsert_trade(conn, row):
            stats["new"] += 1
        else:
            stats["dupe"] += 1
    return stats


def import_decisions_file(conn, path) -> dict:
    """Import one jev_decisions.jsonl into decisions. Never raises on bad lines."""
    path = Path(path)
    stats = _blank_stats("decisions", path)
    for seq, raw in _read_lines(path):
        stats["lines"] += 1
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("line is not a JSON object")
        except ValueError:
            stats["bad"] += 1
            continue
        stats["parsed"] += 1
        if jev_store.upsert_decision(conn, decision_row(obj, raw, seq)):
            stats["new"] += 1
        else:
            stats["dupe"] += 1
    return stats


def import_all(runtime_dir, conn) -> dict:
    """Import every supported JSONL in ``runtime_dir``. Idempotent."""
    runtime_dir = Path(runtime_dir)
    files = []
    decisions_path = runtime_dir / DECISIONS_FILE
    if decisions_path.exists():
        files.append(import_decisions_file(conn, decisions_path))
    for path in sorted(runtime_dir.glob("*.trades.jsonl")):
        files.append(import_trades_file(conn, path))
    totals = {"lines": 0, "parsed": 0, "new": 0, "dupe": 0, "bad": 0}
    for entry in files:
        for key in totals:
            totals[key] += entry[key]
    return {"files": files, "totals": totals}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Idempotent JSONL -> SQLite importer (M1). Reads logs only.")
    p.add_argument("--runtime-dir", default="runtime",
                   help="directory holding the JSONL logs")
    p.add_argument("--db", default=None,
                   help="store path (default: <runtime-dir>/jevelin.db)")
    args = p.parse_args(argv)
    runtime_dir = Path(args.runtime_dir)
    db_path = Path(args.db) if args.db else runtime_dir / "jevelin.db"
    conn = jev_store.connect(str(db_path))
    try:
        stats = import_all(runtime_dir, conn)
    finally:
        conn.close()
    stamp = datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Jevelin M1 import — {runtime_dir}/ -> {db_path} "
          f"(generated {stamp} Asia/Bangkok)")
    for entry in stats["files"]:
        book = f" (book={entry['book']})" if entry["book"] else ""
        print(f"{entry['kind']:<10} {entry['file']}{book}"
              f"  lines {entry['lines']}  parsed {entry['parsed']}"
              f"  new {entry['new']}  dupe {entry['dupe']}"
              f"  unparseable {entry['bad']}")
    t = stats["totals"]
    print(f"totals: lines {t['lines']}  parsed {t['parsed']}  new {t['new']}"
          f"  dupe {t['dupe']}  unparseable {t['bad']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())