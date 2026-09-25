#!/usr/bin/env python3
"""Tests for jev_paper.PaperPortfolio and paper_loop.run_cycle — no network, no orders.

Run: .venv/bin/python scripts/test_jev_paper.py -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_gates import RiskConfig
from jev_paper import PaperPortfolio, _utc_today
from paper_loop import run_cycle

NOW = 1_800_000_000_000
CFG = RiskConfig()


def _enter_verdict(**overrides):
    v = {
        "ok": True,
        "error": None,
        "symbol": "BTC/USDT",
        "pump_0_100": 70.0,
        "dump_0_100": 10.0,
        "phase": "breakout",
        "exhaustion_prob": 0.2,
        "whipsaw_prob": 0.3,
        "confidence": 0.9,
        "answers": {},
        "latency_ms": 10.0,
    }
    v.update(overrides)
    return v


def _exit_verdict(**overrides):
    return _enter_verdict(dump_0_100=75.0, **overrides)


class PaperPortfolioAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "paper.json")

    def _pf(self, equity=10000.0):
        return PaperPortfolio(equity, self.state_path)

    def test_enter_accounting_exact(self):
        pf = self._pf()
        res = pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        self.assertEqual(res["executed"], "enter")
        self.assertEqual(res["usd"], 1800.0)  # 0.18 * 10000 equity
        self.assertEqual(res["qty"], 18.0)  # 1800 / 100
        self.assertEqual(res["realized_pnl"], 0.0)
        self.assertEqual(pf.cash, 8200.0)  # 10000 - 1800
        self.assertEqual(pf.position["qty"], 18.0)
        self.assertEqual(pf.position["entry_price"], 100.0)
        self.assertEqual(pf.position["entry_ts_ms"], NOW)
        self.assertEqual(pf.last_entry_ts_ms, NOW)
        self.assertEqual(pf.mark_to_market(100.0), 10000.0)  # 8200 + 18*100

    def test_exit_realized_pnl_exact(self):
        pf = self._pf()
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        res = pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000)
        self.assertEqual(res["executed"], "exit")
        self.assertEqual(res["qty"], 18.0)
        self.assertEqual(res["usd"], 1980.0)  # 18 * 110
        self.assertEqual(res["realized_pnl"], 180.0)  # (110 - 100) * 18
        self.assertIsNone(pf.position)
        self.assertEqual(pf.cash, 10180.0)  # 8200 + 1980

    def test_no_double_enter(self):
        pf = self._pf()
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        cash_before = pf.cash
        res = pf.apply_action({"action": "enter", "size_fraction": 0.5}, "BTC/USDT", 100.0, NOW + 5)
        self.assertIsNone(res["executed"])
        self.assertEqual(pf.cash, cash_before)  # unchanged
        self.assertEqual(pf.position["qty"], 18.0)  # original position intact

    def test_exit_while_flat_ignored(self):
        pf = self._pf()
        res = pf.apply_action({"action": "exit"}, "BTC/USDT", 100.0, NOW)
        self.assertIsNone(res["executed"])
        self.assertEqual(pf.cash, 10000.0)
        self.assertIsNone(pf.position)

    def test_bad_input_never_raises(self):
        pf = self._pf()
        for bad_price in (0, -5, float("nan"), None, "x"):
            res = pf.apply_action({"action": "enter", "size_fraction": 0.5}, "BTC/USDT", bad_price, NOW)
            self.assertIsNone(res["executed"])
        for bad_size in (0, -0.2, None, "x", float("inf")):
            res = pf.apply_action({"action": "enter", "size_fraction": bad_size}, "BTC/USDT", 100.0, NOW)
            self.assertIsNone(res["executed"])
        self.assertIsNone(pf.apply_action(None, "BTC/USDT", 100.0, NOW)["executed"])
        self.assertIsNone(pf.apply_action({"action": "skip"}, "BTC/USDT", 100.0, NOW)["executed"])
        self.assertEqual(pf.cash, 10000.0)  # nothing executed
        self.assertIsNone(pf.position)


class PersistenceAndRollover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "paper.json")

    def test_persistence_roundtrip(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        pf.save()

        pf2 = PaperPortfolio(555.0, self.state_path)  # different initial proves load() wins
        self.assertTrue(pf2.load())
        self.assertEqual(pf2.cash, pf.cash)
        self.assertEqual(pf2.position["qty"], pf.position["qty"])
        self.assertEqual(pf2.position["entry_price"], pf.position["entry_price"])
        self.assertEqual(pf2.position["entry_ts_ms"], NOW)
        self.assertEqual(pf2.day, pf.day)
        self.assertEqual(pf2.day_start_equity, pf.day_start_equity)
        self.assertEqual(pf2.last_entry_ts_ms, NOW)

        # trade log appended
        trades = [
            json.loads(line)
            for line in Path(self.state_path + ".trades.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["side"], "buy")
        self.assertEqual(trades[0]["qty"], 18.0)
        self.assertEqual(trades[0]["usd"], 1800.0)
        self.assertEqual(trades[0]["ts_ms"], NOW)

    def test_load_missing_file_keeps_defaults(self):
        pf = PaperPortfolio(10000.0, os.path.join(self.tmp.name, "nope.json"))
        self.assertFalse(pf.load())
        self.assertEqual(pf.cash, 10000.0)
        self.assertIsNone(pf.position)

    def test_daily_rollover_resets_baseline(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        # Simulate a stale prior-day baseline.
        pf.day = "2000-01-01"
        pf.day_start_equity = 12345.0
        # Mark at 200 -> equity = cash 8200 + 18*200 = 11800.
        state = pf.to_pf_state(200.0)
        self.assertEqual(pf.day, _utc_today())
        self.assertEqual(pf.day_start_equity, 11800.0)  # reset to current marked equity
        self.assertEqual(state.equity_usd, 11800.0)
        self.assertEqual(state.daily_pnl_pct, 0.0)  # fresh baseline => 0
        self.assertTrue(state.has_position)

    def test_daily_pnl_percent_same_day(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.day_start_equity = 8000.0  # same day, no rollover
        state = pf.to_pf_state(100.0)  # flat -> equity = cash = 10000
        self.assertAlmostEqual(state.daily_pnl_pct, 25.0)  # (10000-8000)/8000*100
        self.assertEqual(state.equity_usd, 10000.0)
        self.assertFalse(state.has_position)


class RunCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "paper.json")

    def test_run_cycle_enter_then_exit(self):
        portfolio = PaperPortfolio(10000.0, self.state_path)
        scorer = mock.Mock()
        scorer.score.side_effect = [_enter_verdict(), _exit_verdict()]
        exchange = mock.Mock()
        exchange.fetch_ticker.side_effect = [{"last": 100.0}, {"last": 110.0}]

        out1 = io.StringIO()
        with redirect_stdout(out1):
            r1 = run_cycle(scorer, portfolio, CFG, exchange, "BTC/USDT", NOW)
        self.assertEqual(r1["result"]["executed"], "enter")
        self.assertEqual(r1["result"]["qty"], 18.0)  # 0.18*10000 / 100
        self.assertEqual(r1["result"]["usd"], 1800.0)
        self.assertTrue(r1["has_position"])

        out2 = io.StringIO()
        with redirect_stdout(out2):
            r2 = run_cycle(scorer, portfolio, CFG, exchange, "BTC/USDT", NOW + 2_000_000)
        self.assertEqual(r2["result"]["executed"], "exit")
        self.assertEqual(r2["result"]["realized_pnl"], 180.0)  # (110-100)*18
        self.assertFalse(r2["has_position"])
        self.assertEqual(r2["equity"], 10180.0)  # 8200 + 1980

        self.assertIn("action=enter", out1.getvalue())
        self.assertIn("action=exit", out2.getvalue())
        self.assertIn("has_position=True", out1.getvalue())
        self.assertIn("has_position=False", out2.getvalue())

        trades = [
            json.loads(line)
            for line in Path(self.state_path + ".trades.jsonl").read_text().splitlines()
        ]
        self.assertEqual([t["side"] for t in trades], ["buy", "sell"])

    def test_run_cycle_error_fail_open(self):
        portfolio = PaperPortfolio(10000.0, self.state_path)
        scorer = mock.Mock()
        scorer.score.side_effect = RuntimeError("boom")
        exchange = mock.Mock()
        out = io.StringIO()
        with redirect_stdout(out):
            r = run_cycle(scorer, portfolio, CFG, exchange, "BTC/USDT", NOW)
        self.assertIn("cycle error:", out.getvalue())
        self.assertIn("boom", out.getvalue())
        self.assertIn("error", r)
        # Portfolio untouched; the loop would simply continue to the next cycle.
        self.assertEqual(portfolio.cash, 10000.0)
        self.assertIsNone(portfolio.position)

    def test_run_cycle_keyboardinterrupt_propagates(self):
        portfolio = PaperPortfolio(10000.0, self.state_path)
        scorer = mock.Mock()
        scorer.score.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run_cycle(scorer, portfolio, CFG, mock.Mock(), "BTC/USDT", NOW)


if __name__ == "__main__":
    unittest.main()