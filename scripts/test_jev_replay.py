#!/usr/bin/env python3
"""Tests for jev_replay (M1 store replay + JSONL cross-check) — no network.

Fixture store rows are built with the M0 fill constants (spot 0.001 fee,
0.0005 slippage; perps 0.0005 taker fee) and the expectations are
hand-computed from those constants.

Run: .venv/bin/python scripts/test_jev_replay.py -v
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_import
import jev_replay
import jev_store
from jev_config import PERPS_TAKER_FEE_RATE, SLIPPAGE_RATE, SPOT_FEE_RATE

# -- spot round trip, hand-computed with the M0 constants -------------------
# entry: price 100, qty 10, target usd 1000 -> fill 100*(1+slip), fee usd*fee
SPOT_ENTRY_FILL = 100.0 * (1.0 + SLIPPAGE_RATE)
SPOT_ENTRY_FEE = 1000.0 * SPOT_FEE_RATE
SPOT_ENTRY_SLIP = (SPOT_ENTRY_FILL - 100.0) * 10.0
# exit: price 110 -> fill 110*(1-slip), fee proceeds*fee
SPOT_EXIT_FILL = 110.0 * (1.0 - SLIPPAGE_RATE)
SPOT_EXIT_FEE = (SPOT_EXIT_FILL * 10.0) * SPOT_FEE_RATE
SPOT_EXIT_SLIP = (110.0 - SPOT_EXIT_FILL) * 10.0
SPOT_REALIZED = ((SPOT_EXIT_FILL - SPOT_ENTRY_FILL) * 10.0
                 - SPOT_ENTRY_FEE - SPOT_EXIT_FEE)

# -- perps long round trip with funding ------------------------------------
PERPS_ENTRY_FILL = 200.0 * (1.0 + SLIPPAGE_RATE)   # long entry fills UP
PERPS_ENTRY_FEE = 3000.0 * PERPS_TAKER_FEE_RATE
PERPS_ENTRY_SLIP = (PERPS_ENTRY_FILL - 200.0) * 15.0
PERPS_EXIT_FILL = 210.0 * (1.0 - SLIPPAGE_RATE)    # long exit fills DOWN
PERPS_EXIT_FEE = (15.0 * PERPS_EXIT_FILL) * PERPS_TAKER_FEE_RATE
PERPS_EXIT_SLIP = (210.0 - PERPS_EXIT_FILL) * 15.0
PERPS_REALIZED = ((PERPS_EXIT_FILL - PERPS_ENTRY_FILL) * 15.0
                  - PERPS_ENTRY_FEE - PERPS_EXIT_FEE)
PERPS_FUNDING = 0.25

SPOT_LINES = [
    {"ts_ms": 1800000000000, "decision_id": "d-1", "book": "spot",
     "symbol": "BTCUSDT", "side": "buy", "price": SPOT_ENTRY_FILL, "qty": 10.0,
     "usd": 1000.0, "realized_pnl": 0.0, "fees": SPOT_ENTRY_FEE,
     "slippage": SPOT_ENTRY_SLIP, "reason": "entered"},
    {"ts_ms": 1800000060000, "decision_id": "d-2", "book": "spot",
     "symbol": "BTCUSDT", "side": "sell", "price": SPOT_EXIT_FILL, "qty": 10.0,
     "usd": SPOT_EXIT_FILL * 10.0, "realized_pnl": SPOT_REALIZED,
     "fees": SPOT_EXIT_FEE, "slippage": SPOT_EXIT_SLIP, "reason": "exited"},
]
PERPS_LINES = [
    {"ts_ms": 1800000000000, "decision_id": "d-1", "book": "perps",
     "symbol": "BTCUSDT", "side": "long", "action": "enter_long",
     "price": PERPS_ENTRY_FILL, "qty": 15.0, "notional": 3000.0, "leverage": 3.0,
     "realized_pnl": 0.0, "funding_paid": 0.0, "fees": PERPS_ENTRY_FEE,
     "slippage": PERPS_ENTRY_SLIP, "reason": "all gates passed (long)"},
    {"ts_ms": 1800000060000, "decision_id": "d-2", "book": "perps",
     "symbol": "BTCUSDT", "side": "long", "action": "exited",
     "price": PERPS_EXIT_FILL, "qty": 15.0, "notional": 3000.0, "leverage": 3.0,
     "realized_pnl": PERPS_REALIZED, "funding_paid": PERPS_FUNDING,
     "fees": PERPS_EXIT_FEE, "slippage": PERPS_EXIT_SLIP, "reason": "long exit"},
]

class ReplayMath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.db = self.runtime / "jevelin.db"
        (self.runtime / "paper_btc.json.trades.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in SPOT_LINES))
        (self.runtime / "perps_btc.json.trades.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in PERPS_LINES))
        self.conn = jev_store.connect(str(self.db))
        self.addCleanup(self.conn.close)
        jev_import.import_all(self.runtime, self.conn)

    def test_spot_math_matches_hand_computed(self):
        s = jev_replay.store_book_stats(self.conn)["spot"]
        self.assertEqual(s["trips"], 1)
        self.assertAlmostEqual(s["fees"], SPOT_ENTRY_FEE + SPOT_EXIT_FEE, places=9)
        self.assertAlmostEqual(s["slippage"], SPOT_ENTRY_SLIP + SPOT_EXIT_SLIP, places=9)
        # gross is quote-price PnL before costs
        self.assertAlmostEqual(s["gross"], (110.0 - 100.0) * 10.0, places=9)
        self.assertAlmostEqual(s["net"], SPOT_REALIZED, places=9)

    def test_perps_math_matches_hand_computed(self):
        s = jev_replay.store_book_stats(self.conn)["perps"]
        # both rows are side=long; the action (enter_long/exited) counts the trip
        self.assertEqual(s["trips"], 1)
        self.assertAlmostEqual(s["fees"], PERPS_ENTRY_FEE + PERPS_EXIT_FEE, places=9)
        self.assertAlmostEqual(s["slippage"], PERPS_ENTRY_SLIP + PERPS_EXIT_SLIP, places=9)
        self.assertAlmostEqual(s["gross"], (210.0 - 200.0) * 15.0, places=9)
        self.assertAlmostEqual(s["funding"], PERPS_FUNDING, places=9)
        self.assertAlmostEqual(s["net"], PERPS_REALIZED - PERPS_FUNDING, places=9)

    def test_cross_check_agrees_with_jsonl(self):
        rows = jev_replay.compare_summary(self.conn, self.runtime)
        self.assertTrue(rows)
        self.assertTrue(all(match for _b, _m, _s, _j, match in rows))

    def test_print_summary_exit_zero_on_match(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = jev_replay.print_summary(str(self.db), self.runtime)
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("spot", out)
        self.assertIn("perps", out)
        self.assertIn("net PnL", out)

    def test_print_summary_flags_mismatch(self):
        self.conn.execute("UPDATE trades SET realized_pnl = realized_pnl + 5.0 "
                          "WHERE side = 'sell'")
        self.conn.commit()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = jev_replay.print_summary(str(self.db), self.runtime)
        self.assertEqual(code, 1)
        self.assertIn("DIFF", buf.getvalue())

    def test_dump_trades_paged(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = jev_replay.main(["--dump", "trades", "--limit", "1",
                                    "--db", str(self.db),
                                    "--runtime-dir", str(self.runtime)])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("showing", out)
        self.assertIn("1-1 of 4", out)

    def test_dump_decisions(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = jev_replay.main(["--dump", "decisions",
                                    "--db", str(self.db),
                                    "--runtime-dir", str(self.runtime)])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()