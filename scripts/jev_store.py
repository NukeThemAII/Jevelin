#!/usr/bin/env python3
"""jev_store — SQLite store for the M1 replay/calibration backbone (stdlib only).

DB at ``runtime/jevelin.db`` (override per call; tests use ``:memory:`` or temp
dirs), WAL mode, ``create_schema()`` idempotent. Table columns follow
docs/V2-DESIGN.md B.5 plus the M1 radar table:

  decisions(id, ts, decision_id, symbol, state_json, verdict_json, cost,
            latency_ms, ok, raw_json)
  trades(id, decision_id, ts, book, symbol, side, price, qty, notional, fees,
         slippage, realized_pnl, funding_paid, reason, veto_bitmask, raw_json)
  positions(id, book, symbol, side, qty, entry_price, stop_price, liq_price,
            entry_ts, close_ts)
  marks(ts, symbol, price, equity, book)
  radar_candidates(ts, run_id, rank, symbol, coingecko_id, price_usd,
                   volume_24h, mcap, ath_date, passed, rejections_json, raw_json)

``decision_id`` is TEXT and ``veto_bitmask`` is INTEGER. ``raw_json`` holds the
verbatim source line (lossless import) wherever the source row carries fields
beyond the schema.

Deterministic upsert keys (idempotent re-imports never duplicate):
  trades   — UNIQUE (book, ts, side, COALESCE(reason, ''))   # the M1 trade key
  decisions — UNIQUE (decision_id, ts)
  radar_candidates — UNIQUE (run_id, coingecko_id)

NOTE: ``trades.ts`` is epoch MILLISECONDS (as the source rows carry ``ts_ms``);
``decisions.ts`` is epoch SECONDS (as ``jev_client`` logs ``time.time()``).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "runtime/jevelin.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ts REAL,
    decision_id TEXT NOT NULL,
    symbol TEXT,
    state_json TEXT,
    verdict_json TEXT,
    cost REAL,
    latency_ms REAL,
    ok INTEGER,
    raw_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_dedup
    ON decisions (decision_id, ts);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    decision_id TEXT,
    ts REAL,
    book TEXT,
    symbol TEXT,
    side TEXT,
    price REAL,
    qty REAL,
    notional REAL,
    fees REAL,
    slippage REAL,
    realized_pnl REAL,
    funding_paid REAL,
    reason TEXT,
    veto_bitmask INTEGER,
    raw_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_dedup
    ON trades (book, ts, side, COALESCE(reason, ''));

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    book TEXT,
    symbol TEXT,
    side TEXT,
    qty REAL,
    entry_price REAL,
    stop_price REAL,
    liq_price REAL,
    entry_ts REAL,
    close_ts REAL
);

CREATE TABLE IF NOT EXISTS marks (
    ts REAL,
    symbol TEXT,
    price REAL,
    equity REAL,
    book TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_marks_dedup
    ON marks (ts, symbol, book);

CREATE TABLE IF NOT EXISTS radar_candidates (
    ts REAL,
    run_id TEXT,
    rank INTEGER,
    symbol TEXT,
    coingecko_id TEXT,
    price_usd REAL,
    volume_24h REAL,
    mcap REAL,
    ath_date TEXT,
    passed INTEGER,
    rejections_json TEXT,
    raw_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_radar_dedup
    ON radar_candidates (run_id, coingecko_id);
"""


def connect(db_path=DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (and create) the store; sets WAL and installs the schema."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    create_schema(conn)
    return conn


def create_schema(conn) -> None:
    """Idempotent schema creation."""
    conn.executescript(_SCHEMA)
    conn.commit()


_DECISION_COLS = ("ts", "decision_id", "symbol", "state_json", "verdict_json",
                  "cost", "latency_ms", "ok", "raw_json")
_TRADE_COLS = ("decision_id", "ts", "book", "symbol", "side", "price", "qty",
               "notional", "fees", "slippage", "realized_pnl", "funding_paid",
               "reason", "veto_bitmask", "raw_json")
_RADAR_COLS = ("ts", "run_id", "rank", "symbol", "coingecko_id", "price_usd",
               "volume_24h", "mcap", "ath_date", "passed", "rejections_json",
               "raw_json")


def _row_dict(row) -> dict:
    return dict(row) if row is not None else None


def _upsert(conn, table, cols, key_cols, key_where, key_vals, row) -> bool:
    """Insert ``row`` or update the existing key row. True iff newly inserted."""
    exists = conn.execute(
        f"SELECT rowid FROM {table} WHERE {key_where}", key_vals).fetchone()
    if exists is not None:
        set_cols = [c for c in cols if c not in key_cols]
        assignments = ", ".join(f"{c} = ?" for c in set_cols)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE rowid = ?",
            [row.get(c) for c in set_cols] + [exists[0]])
        conn.commit()
        return False
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
        [row.get(c) for c in cols])
    conn.commit()
    return True


def upsert_decision(conn, row: dict) -> bool:
    """Upsert one decisions row; key = (decision_id, ts). True iff new."""
    return _upsert(conn, "decisions", _DECISION_COLS, ("decision_id", "ts"),
                   "decision_id = ? AND ts = ?",
                   (row.get("decision_id"), row.get("ts")), row)


def upsert_trade(conn, row: dict) -> bool:
    """Upsert one trades row; key = (book, ts, side, COALESCE(reason, ''))."""
    return _upsert(conn, "trades", _TRADE_COLS, ("book", "ts", "side", "reason"),
                   "book = ? AND ts = ? AND side = ? AND COALESCE(reason, '') = ?",
                   (row.get("book"), row.get("ts"), row.get("side"),
                    row.get("reason") or ""), row)


def upsert_radar_candidate(conn, row: dict) -> bool:
    """Upsert one radar_candidates row; key = (run_id, coingecko_id)."""
    return _upsert(conn, "radar_candidates", _RADAR_COLS,
                   ("run_id", "coingecko_id"),
                   "run_id = ? AND coingecko_id = ?",
                   (row.get("run_id"), row.get("coingecko_id")), row)


def get_decision(conn, row_id):
    return _row_dict(conn.execute(
        "SELECT * FROM decisions WHERE id = ?", (row_id,)).fetchone())


def get_trade(conn, row_id):
    return _row_dict(conn.execute(
        "SELECT * FROM trades WHERE id = ?", (row_id,)).fetchone())


def list_decisions(conn, limit=None, offset=0) -> list:
    sql = "SELECT * FROM decisions ORDER BY ts, id"
    args = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args = [int(limit), int(offset)]
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def list_trades(conn, book=None, limit=None, offset=0) -> list:
    sql = "SELECT * FROM trades"
    args = []
    if book is not None:
        sql += " WHERE book = ?"
        args.append(book)
    sql += " ORDER BY ts, id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def delete_decision(conn, row_id) -> bool:
    cur = conn.execute("DELETE FROM decisions WHERE id = ?", (row_id,))
    conn.commit()
    return cur.rowcount > 0


def delete_trade(conn, row_id) -> bool:
    cur = conn.execute("DELETE FROM trades WHERE id = ?", (row_id,))
    conn.commit()
    return cur.rowcount > 0
