#!/usr/bin/env python3
"""Tests for jev_import (M1 JSONL -> SQLite importer) — no network, temp dirs only.

Run: .venv/bin/python scripts/test_jev_import.py -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_import
import jev_store

# -- fixture lines (shapes verified against the live writers) ----------------

# M0 spot row: jev_paper.PaperPortfolio._append_trade
M0_SPOT = {"ts_ms": 1800000000123, "decision_id": "20260927-113332-ae9ee726",
           "book": "spot", "symbol": "BTCUSDT", "side": "buy", "price": 84000.0,
           "qty": 0.01, "usd": 840.0, "realized_pnl": 0.0, "fees": 0.84,
           "slippage": 0.42, "reason": "entered"}

# pre-M0 spot row: ts_ms/symbol/side/price/qty/usd/realized_pnl only
PRE_SPOT = {"ts_ms": 1800000000456, "symbol": "BTCUSDT", "side": "sell",
            "price": 83000.0, "qty": 0.01, "usd": 830.0, "realized_pnl": -10.0}

# M0 perps row: jev_perps.PerpsPortfolio._append_trade
M0_PERPS = {"ts_ms": 1800000000789, "decision_id": "20260927-113332-ae9ee726",
            "book": "perps", "symbol": "BTCUSDT", "side": "long", "action": "exited",
            "price": 84000.0, "qty": 0.02, "notional": 1680.0, "leverage": 3.0,
            "realized_pnl": 1.5, "funding_paid": 0.01, "fees": 0.84,
            "slippage": 0.42, "reason": "long exit: dump 66.0 >= 60.0"}

# pre-M0 perps row: no decision_id/fees/slippage
PRE_PERPS = {"ts_ms": 1800000000987, "symbol": "BTCUSDT", "side": "long",
             "action": "enter_long", "price": 84000.0, "qty": 0.02,
             "notional": 1680.0, "leverage": 3.0, "realized_pnl": 0.0,
             "funding_paid": 0.0, "reason": "all gates passed (long): pump=65"}

# M0 decision row: jev_client.JevClient._write_log
DEC_M0 = {"ts": 1800000000.123, "decision_id": "20260927-113332-ae9ee726",
          "state_sha256": "abc123", "questions": {"pump": {"type": "score"}},
          "raw": {"model": "typesafe/jev-1.13",
                  "answers": {"pump": {"type": "score", "score": 1.9}},
                  "usage": {"input_tokens": 371, "output_tokens": 36,
                            "cost": 1.5582e-05},
                  "id": "gen-dec-1", "provider": "TypeSafe"},
          "error": None}

# pre-M0 decision row: no decision_id
DEC_PRE = {"ts": 1800000001.5, "state_sha256": "def456", "questions": {},
           "raw": {"answers": {}, "usage": {"cost": 1.0e-05}}, "error": None}

# errored decision row
DEC_BAD = {"ts": 1800000002.5, "decision_id": "20260927-114000-ffffffff",
           "state_sha256": "zzz", "questions": {}, "raw": None,
           "error": "HTTP 500: boom"}

BAD_LINE = "{not json at all"


def _line(obj):
    return json.dumps(obj)


class ImportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.conn = jev_store.connect(str(self.runtime / "jevelin.db"))
        self.addCleanup(self.conn.close)

    def _write(self, name, lines):
        (self.runtime / name).write_text("".join(line + "\n" for line in lines))

    def _import(self):
        return jev_import.import_all(self.runtime, self.conn)


class TradesImport(ImportTestBase):
    def setUp(self):
        super().setUp()
        self._write("paper_btc.json.trades.jsonl", [_line(M0_SPOT), _line(PRE_SPOT)])
        self._write("perps_btc.json.trades.jsonl", [_line(M0_PERPS), _line(PRE_PERPS)])

    def test_counts(self):
        stats = self._import()
        self.assertEqual(stats["totals"]["parsed"], 4)
        self.assertEqual(stats["totals"]["new"], 4)
        self.assertEqual(stats["totals"]["bad"], 0)

    def test_raw_json_verbatim(self):
        self._import()
        rows = jev_store.list_trades(self.conn)
        raws = sorted(r["raw_json"] for r in rows)
        expected = sorted([_line(M0_SPOT), _line(PRE_SPOT),
                           _line(M0_PERPS), _line(PRE_PERPS)])
        self.assertEqual(raws, expected)

    def test_book_attribution(self):
        self._import()
        by_book = {r["book"] for r in jev_store.list_trades(self.conn)}
        self.assertEqual(by_book, {"spot", "perps"})
        spot = jev_store.list_trades(self.conn, book="spot")
        self.assertEqual({r["ts"] for r in spot},
                         {M0_SPOT["ts_ms"], PRE_SPOT["ts_ms"]})

    def test_m0_fields_mapped(self):
        self._import()
        rows = {r["ts"]: r for r in jev_store.list_trades(self.conn)}
        m0 = rows[M0_SPOT["ts_ms"]]
        self.assertAlmostEqual(m0["fees"], 0.84)
        self.assertAlmostEqual(m0["slippage"], 0.42)
        self.assertEqual(m0["decision_id"], "20260927-113332-ae9ee726")
        self.assertAlmostEqual(m0["notional"], 840.0)  # from "usd"
        perps = rows[M0_PERPS["ts_ms"]]
        self.assertAlmostEqual(perps["funding_paid"], 0.01)
        self.assertAlmostEqual(perps["notional"], 1680.0)

    def test_pre_m0_fields_null(self):
        self._import()
        rows = {r["ts"]: r for r in jev_store.list_trades(self.conn)}
        pre = rows[PRE_SPOT["ts_ms"]]
        self.assertIsNone(pre["fees"])
        self.assertIsNone(pre["slippage"])
        self.assertIsNone(pre["veto_bitmask"])
        self.assertIsNone(pre["funding_paid"])
        self.assertEqual(pre["reason"], "")  # normalized empty, not fabricated
        perps = rows[PRE_PERPS["ts_ms"]]
        self.assertIsNone(perps["fees"])
        self.assertIsNone(perps["slippage"])

    def test_synthetic_decision_id_stable(self):
        self._import()
        rows = {r["ts"]: r for r in jev_store.list_trades(self.conn)}
        self.assertEqual(rows[PRE_SPOT["ts_ms"]]["decision_id"],
                         f"synth-{PRE_SPOT['ts_ms']}-2")
        self.assertEqual(rows[PRE_PERPS["ts_ms"]]["decision_id"],
                         f"synth-{PRE_PERPS['ts_ms']}-2")
        self._import()  # re-import keeps the same synthetic ids (no dupes)
        rows = {r["ts"]: r for r in jev_store.list_trades(self.conn)}
        self.assertEqual(rows[PRE_SPOT["ts_ms"]]["decision_id"],
                         f"synth-{PRE_SPOT['ts_ms']}-2")

    def test_reimport_no_dupes(self):
        first = self._import()
        self.assertEqual(first["totals"]["new"], 4)
        second = self._import()
        self.assertEqual(second["totals"]["new"], 0)
        self.assertEqual(second["totals"]["dupe"], 4)
        self.assertEqual(len(jev_store.list_trades(self.conn)), 4)

    def test_unparseable_skipped_and_counted(self):
        self._write("paper_btc.json.trades.jsonl",
                    [_line(M0_SPOT), BAD_LINE, _line(PRE_SPOT)])
        stats = self._import()
        spot = [f for f in stats["files"] if "paper_btc" in f["file"]][0]
        self.assertEqual(spot["lines"], 3)
        self.assertEqual(spot["parsed"], 2)
        self.assertEqual(spot["bad"], 1)
        self.assertEqual(stats["totals"]["bad"], 1)
        # spot rows imported, perps file untouched: 4 rows total
        self.assertEqual(len(jev_store.list_trades(self.conn)), 4)

    def test_cli_exit_zero(self):
        code = jev_import.main(["--runtime-dir", str(self.runtime),
                                "--db", str(self.runtime / "jevelin.db")])
        self.assertEqual(code, 0)


class DecisionsImport(ImportTestBase):
    def setUp(self):
        super().setUp()
        self._write("jev_decisions.jsonl",
                    [_line(DEC_M0), _line(DEC_PRE), _line(DEC_BAD)])

    def test_counts(self):
        stats = self._import()
        dec = [f for f in stats["files"] if f["kind"] == "decisions"][0]
        self.assertEqual(dec["lines"], 3)
        self.assertEqual(dec["parsed"], 3)
        self.assertEqual(dec["new"], 3)

    def test_mapping(self):
        self._import()
        rows = {r["ts"]: r for r in jev_store.list_decisions(self.conn)}
        m0 = rows[DEC_M0["ts"]]
        self.assertEqual(m0["decision_id"], "20260927-113332-ae9ee726")
        self.assertAlmostEqual(m0["cost"], 1.5582e-05)
        self.assertEqual(m0["ok"], 1)
        self.assertIsNone(m0["latency_ms"])  # not recorded by the client
        self.assertEqual(json.loads(m0["verdict_json"]),
                         {"pump": {"type": "score", "score": 1.9}})
        self.assertIn("abc123", m0["state_json"])
        self.assertEqual(m0["raw_json"], _line(DEC_M0))
        bad = rows[DEC_BAD["ts"]]
        self.assertEqual(bad["ok"], 0)
        self.assertIsNone(bad["cost"])

    def test_synthetic_decision_id(self):
        self._import()
        rows = {r["ts"]: r for r in jev_store.list_decisions(self.conn)}
        self.assertEqual(rows[DEC_PRE["ts"]]["decision_id"],
                         f"synth-{DEC_PRE['ts']}-2")

    def test_reimport_no_dupes(self):
        self._import()
        second = self._import()
        self.assertEqual(second["totals"]["new"], 0)
        self.assertEqual(len(jev_store.list_decisions(self.conn)), 3)


class EmptyRuntime(ImportTestBase):
    def test_missing_files_ok(self):
        stats = self._import()
        self.assertEqual(stats["totals"]["parsed"], 0)
        self.assertEqual(stats["totals"]["bad"], 0)


class MultiPairTrades(ImportTestBase):
    """M5: same-ts fills from DIFFERENT pairs must never collide in the store."""

    def _write_three(self):
        rows = [dict(M0_SPOT, symbol=s) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
        self._write("paper_btc.json.trades.jsonl", [_line(rows[0])])
        self._write("paper_eth.json.trades.jsonl", [_line(rows[1])])
        self._write("paper_sol.json.trades.jsonl", [_line(rows[2])])

    def test_same_ts_different_symbols_all_import(self):
        self._write_three()
        stats = self._import()
        self.assertEqual(stats["totals"]["new"], 3)
        self.assertEqual(stats["totals"]["bad"], 0)
        self.assertEqual({r["symbol"] for r in jev_store.list_trades(self.conn)},
                         {"BTCUSDT", "ETHUSDT", "SOLUSDT"})

    def test_reimport_no_dupes_multi_pair(self):
        self._write_three()
        self._import()
        second = self._import()
        self.assertEqual(second["totals"]["new"], 0)
        self.assertEqual(len(jev_store.list_trades(self.conn)), 3)

    def test_same_symbol_same_ts_still_dedupes(self):
        # unchanged single-pair semantics: the same row never duplicates
        self._write("paper_btc.json.trades.jsonl", [_line(M0_SPOT)])
        self._import()
        self._import()
        self.assertEqual(len(jev_store.list_trades(self.conn)), 1)

    def test_legacy_dedup_index_is_migrated(self):
        # simulate an M1-era DB whose unique key lacks the symbol column
        self.conn.execute("DROP INDEX idx_trades_dedup")
        self.conn.execute("CREATE UNIQUE INDEX idx_trades_dedup ON trades "
                          "(book, ts, side, COALESCE(reason, ''))")
        self.conn.commit()
        jev_store.create_schema(self.conn)  # migration replaces the legacy key
        self._write_three()
        stats = self._import()
        self.assertEqual(stats["totals"]["new"], 3)
        second = self._import()
        self.assertEqual(second["totals"]["new"], 0)


if __name__ == "__main__":
    unittest.main()