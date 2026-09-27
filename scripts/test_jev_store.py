#!/usr/bin/env python3
"""Tests for jev_store (M1 SQLite store) — stdlib only, no network, no runtime/ writes.

Run: .venv/bin/python scripts/test_jev_store.py -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_store


def _decision(**over):
    row = {
        "ts": 1800000000.123,
        "decision_id": "20260927-113332-ae9ee726",
        "symbol": "BTCUSDT",
        "state_json": "{\"state_sha256\": \"abc\"}",
        "verdict_json": "{\"ok\": true}",
        "cost": 1.5582e-05,
        "latency_ms": 402.5,
        "ok": 1,
        "raw_json": "{\"ts\": 1800000000.123}",
    }
    row.update(over)
    return row


def _trade(**over):
    row = {
        "decision_id": "20260927-113332-ae9ee726",
        "ts": 1800000000123,
        "book": "spot",
        "symbol": "BTCUSDT",
        "side": "buy",
        "price": 84000.0,
        "qty": 0.01,
        "notional": 840.0,
        "fees": 0.84,
        "slippage": 0.42,
        "realized_pnl": 0.0,
        "funding_paid": None,
        "reason": "entered",
        "veto_bitmask": None,
        "raw_json": "{\"ts_ms\": 1800000000123}",
    }
    row.update(over)
    return row


def _radar(**over):
    row = {
        "ts": 1800000000.0,
        "run_id": "20260927-200000-abcd1234",
        "rank": 1,
        "symbol": "WIF",
        "coingecko_id": "dogwifcoin",
        "price_usd": 1.23,
        "volume_24h": 99000000.0,
        "mcap": 1230000000.0,
        "ath_date": "2024-03-31T00:00:00.000Z",
        "passed": 1,
        "rejections_json": "[]",
        "raw_json": "{\"id\": \"dogwifcoin\"}",
    }
    row.update(over)
    return row


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = jev_store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_create_schema_idempotent(self):
        jev_store.create_schema(self.conn)
        jev_store.create_schema(self.conn)  # second call must not raise or duplicate
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("decisions", "trades", "positions", "marks", "radar_candidates"):
            self.assertIn(table, names)

    def test_required_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)")}
        for name in ("id", "decision_id", "ts", "book", "symbol", "side", "price",
                     "qty", "notional", "fees", "slippage", "realized_pnl",
                     "funding_paid", "reason", "veto_bitmask", "raw_json"):
            self.assertIn(name, cols)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(decisions)")}
        for name in ("id", "ts", "decision_id", "symbol", "state_json",
                     "verdict_json", "cost", "latency_ms", "ok"):
            self.assertIn(name, cols)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(radar_candidates)")}
        for name in ("ts", "run_id", "rank", "symbol", "coingecko_id", "price_usd",
                     "volume_24h", "mcap", "ath_date", "passed", "rejections_json",
                     "raw_json"):
            self.assertIn(name, cols)

    def test_wal_mode_on_file_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = jev_store.connect(str(Path(tmp) / "jevelin.db"))
            self.addCleanup(conn.close)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")


class DecisionCrud(unittest.TestCase):
    def setUp(self):
        self.conn = jev_store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_insert_and_get(self):
        self.assertTrue(jev_store.upsert_decision(self.conn, _decision()))
        got = jev_store.get_decision(self.conn, 1)
        self.assertIsNotNone(got)
        self.assertEqual(got["decision_id"], "20260927-113332-ae9ee726")
        self.assertEqual(got["ok"], 1)
        self.assertAlmostEqual(got["cost"], 1.5582e-05)

    def test_list_and_delete(self):
        jev_store.upsert_decision(self.conn, _decision())
        jev_store.upsert_decision(self.conn, _decision(ts=1800000001.5))
        self.assertEqual(len(jev_store.list_decisions(self.conn)), 2)
        self.assertTrue(jev_store.delete_decision(self.conn, 1))
        self.assertEqual(len(jev_store.list_decisions(self.conn)), 1)
        self.assertIsNone(jev_store.get_decision(self.conn, 1))

    def test_update_via_upsert(self):
        jev_store.upsert_decision(self.conn, _decision())
        self.assertFalse(jev_store.upsert_decision(self.conn, _decision(cost=9.9)))
        self.assertAlmostEqual(jev_store.get_decision(self.conn, 1)["cost"], 9.9)

    def test_upsert_idempotent_on_decision_id_and_ts(self):
        self.assertTrue(jev_store.upsert_decision(self.conn, _decision()))
        self.assertFalse(jev_store.upsert_decision(self.conn, _decision()))
        self.assertEqual(len(jev_store.list_decisions(self.conn)), 1)

    def test_distinct_rows_kept(self):
        jev_store.upsert_decision(self.conn, _decision())
        jev_store.upsert_decision(self.conn, _decision(ts=1800000002.5))
        jev_store.upsert_decision(self.conn, _decision(decision_id="other-id"))
        self.assertEqual(len(jev_store.list_decisions(self.conn)), 3)


class TradeCrud(unittest.TestCase):
    def setUp(self):
        self.conn = jev_store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_insert_and_get(self):
        self.assertTrue(jev_store.upsert_trade(self.conn, _trade()))
        got = jev_store.get_trade(self.conn, 1)
        self.assertIsNotNone(got)
        self.assertEqual(got["book"], "spot")
        self.assertEqual(got["side"], "buy")
        self.assertAlmostEqual(got["fees"], 0.84)

    def test_list_filter_by_book_and_delete(self):
        jev_store.upsert_trade(self.conn, _trade())
        jev_store.upsert_trade(self.conn, _trade(book="perps", side="long",
                                                 ts=1800000000456))
        self.assertEqual(len(jev_store.list_trades(self.conn)), 2)
        self.assertEqual(len(jev_store.list_trades(self.conn, book="perps")), 1)
        self.assertTrue(jev_store.delete_trade(self.conn, 1))
        self.assertEqual(len(jev_store.list_trades(self.conn)), 1)

    def test_upsert_idempotent_on_dedup_key(self):
        self.assertTrue(jev_store.upsert_trade(self.conn, _trade()))
        self.assertFalse(jev_store.upsert_trade(self.conn, _trade()))
        self.assertEqual(len(jev_store.list_trades(self.conn)), 1)

    def test_missing_reason_normalized_in_key(self):
        # reason NULL and reason "" are the same deterministic dedup key.
        self.assertTrue(jev_store.upsert_trade(self.conn, _trade(reason=None)))
        self.assertFalse(jev_store.upsert_trade(self.conn, _trade(reason="")))
        self.assertEqual(len(jev_store.list_trades(self.conn)), 1)

    def test_distinct_rows_kept(self):
        jev_store.upsert_trade(self.conn, _trade())
        jev_store.upsert_trade(self.conn, _trade(ts=1800000000999))
        jev_store.upsert_trade(self.conn, _trade(side="sell", reason="exited"))
        self.assertEqual(len(jev_store.list_trades(self.conn)), 3)

    def test_update_via_upsert(self):
        jev_store.upsert_trade(self.conn, _trade())
        self.assertFalse(jev_store.upsert_trade(self.conn, _trade(realized_pnl=42.0)))
        self.assertAlmostEqual(jev_store.get_trade(self.conn, 1)["realized_pnl"], 42.0)


class RadarCrud(unittest.TestCase):
    def setUp(self):
        self.conn = jev_store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_upsert_idempotent_on_run_and_id(self):
        self.assertTrue(jev_store.upsert_radar_candidate(self.conn, _radar()))
        self.assertFalse(jev_store.upsert_radar_candidate(self.conn, _radar()))
        rows = self.conn.execute("SELECT * FROM radar_candidates").fetchall()
        self.assertEqual(len(rows), 1)

    def test_new_run_is_new_row(self):
        jev_store.upsert_radar_candidate(self.conn, _radar())
        jev_store.upsert_radar_candidate(self.conn, _radar(run_id="other-run"))
        rows = self.conn.execute("SELECT * FROM radar_candidates").fetchall()
        self.assertEqual(len(rows), 2)

    def test_rejected_coin_stored(self):
        jev_store.upsert_radar_candidate(self.conn, _radar(
            rank=None, passed=0, rejections_json="[\"too_new\"]"))
        row = self.conn.execute("SELECT passed, rank FROM radar_candidates").fetchone()
        self.assertEqual(row["passed"], 0)
        self.assertIsNone(row["rank"])


if __name__ == "__main__":
    unittest.main()

