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

from jev_config import atomic_write_json, new_decision_id
from jev_gates import RiskConfig, decide
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
        # Default costs: fee 0.10%/side, slippage 5 bps/side (buy fills UP).
        pf = self._pf()
        res = pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        self.assertEqual(res["executed"], "enter")
        self.assertEqual(res["usd"], 1800.0)  # 0.18 * 10000 equity
        self.assertAlmostEqual(res["qty"], 17.991004497751124)  # 1800 / 100.05 fill
        self.assertEqual(res["realized_pnl"], 0.0)
        self.assertAlmostEqual(res["fees"], 1.8)  # 0.001 * 1800
        self.assertAlmostEqual(res["slippage"], 0.899550224887505)  # (100.05-100) * qty
        self.assertAlmostEqual(pf.cash, 8198.2)  # 10000 - 1800 - 1.8 fee
        self.assertAlmostEqual(pf.position["qty"], 17.991004497751124)
        self.assertAlmostEqual(pf.position["entry_price"], 100.05)  # 100 * (1 + 0.0005)
        self.assertEqual(pf.position["entry_ts_ms"], NOW)
        self.assertEqual(pf.last_entry_ts_ms, NOW)
        self.assertAlmostEqual(pf.mark_to_market(100.0), 9997.300449775114)

    def test_exit_realized_pnl_exact(self):
        # Exit fills DOWN (110 * 0.9995); realized is net of BOTH fill fees.
        pf = self._pf()
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        res = pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000)
        self.assertEqual(res["executed"], "exit")
        self.assertAlmostEqual(res["qty"], 17.991004497751124)
        self.assertAlmostEqual(res["usd"], 1978.0209895052474)  # qty * 109.945 fill
        self.assertAlmostEqual(res["realized_pnl"], 174.24296851574232, places=6)
        self.assertAlmostEqual(res["fees"], 1.9780209895052474, places=6)
        self.assertAlmostEqual(res["slippage"], 0.9895052473761788, places=6)
        self.assertIsNone(pf.position)
        self.assertAlmostEqual(pf.cash, 10174.242968515742, places=6)

    def test_no_double_enter(self):
        pf = self._pf()
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        cash_before = pf.cash
        res = pf.apply_action({"action": "enter", "size_fraction": 0.5}, "BTC/USDT", 100.0, NOW + 5)
        self.assertIsNone(res["executed"])
        self.assertEqual(pf.cash, cash_before)  # unchanged
        self.assertAlmostEqual(pf.position["qty"], 17.991004497751124)  # original position intact

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
        self.assertAlmostEqual(trades[0]["qty"], 17.991004497751124)
        self.assertEqual(trades[0]["usd"], 1800.0)
        self.assertEqual(trades[0]["ts_ms"], NOW)
        self.assertAlmostEqual(trades[0]["fees"], 1.8)  # M0: per-fill fee cost
        self.assertAlmostEqual(trades[0]["slippage"], 0.899550224887505)

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
        # Mark at 200 -> equity = cash 8198.2 + 17.991...*200 = 11796.40...
        state = pf.to_pf_state(200.0)
        self.assertEqual(pf.day, _utc_today())
        self.assertAlmostEqual(pf.day_start_equity, 11796.400899550225)  # reset to marked equity
        self.assertAlmostEqual(state.equity_usd, 11796.400899550225)
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
        # conf 0.9 -> 100% tier -> 0.20 * 10000 target notional
        self.assertAlmostEqual(r1["result"]["qty"], 19.99000499750125)  # 2000 / 100.05
        self.assertEqual(r1["result"]["usd"], 2000.0)
        self.assertTrue(r1["has_position"])

        portfolio.cycles_held = 2  # M3 min hold: >= 3 cycles before signal exits
        out2 = io.StringIO()
        with redirect_stdout(out2):
            r2 = run_cycle(scorer, portfolio, CFG, exchange, "BTC/USDT", NOW + 2_000_000)
        self.assertEqual(r2["result"]["executed"], "exit")
        self.assertAlmostEqual(r2["result"]["realized_pnl"], 193.60329835082476, places=6)
        self.assertFalse(r2["has_position"])
        self.assertAlmostEqual(r2["equity"], 10193.603298350825, places=6)

        self.assertIn("action=enter", out1.getvalue())
        self.assertIn("action=exit", out2.getvalue())
        self.assertIn("has_position=True", out1.getvalue())
        self.assertIn("has_position=False", out2.getvalue())
        self.assertIn("decision=", out1.getvalue())  # M0: decision_id on every cycle line

        trades = [
            json.loads(line)
            for line in Path(self.state_path + ".trades.jsonl").read_text().splitlines()
        ]
        self.assertEqual([t["side"] for t in trades], ["buy", "sell"])

        # M0: decision ids flow end-to-end into trade rows AND the decision log.
        self.assertTrue(all(t.get("decision_id") for t in trades))
        decisions = [
            json.loads(line)
            for line in Path(self.tmp.name, "paper_decisions.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(decisions), 2)  # one row per book per cycle
        self.assertEqual([d["decision_id"] for d in decisions],
                         [t["decision_id"] for t in trades])
        self.assertEqual(decisions[0]["veto_bitmask"], 0)
        self.assertEqual(decisions[0]["book"], "spot")

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


class FeesSlippageAndCosts(unittest.TestCase):
    """M0 / F-P0-1: per-side fees + adverse slippage (defaults 0.10% + 5 bps)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "paper.json")

    def _trades(self):
        return [json.loads(line) for line in
                Path(self.state_path + ".trades.jsonl").read_text().splitlines()]

    def test_slippage_buy_up_sell_down(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000)
        buy, sell = self._trades()
        self.assertAlmostEqual(buy["price"], 100.05)    # buy fills UP: 100 * 1.0005
        self.assertAlmostEqual(sell["price"], 109.945)  # sell fills DOWN: 110 * 0.9995
        self.assertAlmostEqual(buy["fees"], 1.8)  # 0.001 * 1800
        self.assertAlmostEqual(sell["fees"], 1.9780209895052474, places=6)
        self.assertAlmostEqual(buy["slippage"], 0.899550224887505, places=6)
        self.assertAlmostEqual(sell["slippage"], 0.9895052473761788, places=6)

    def test_fees_reduce_equity_and_are_in_realized_pnl(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        self.assertAlmostEqual(pf.cash, 8198.2)  # 10000 - 1800 - 1.8 fee
        res = pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000)
        # realized is net of BOTH fees and both slippage legs
        self.assertAlmostEqual(res["realized_pnl"], 174.24296851574232, places=6)
        self.assertAlmostEqual(pf.cash, 10174.242968515742, places=6)

    def test_cumulative_fees_paid_in_state(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000)
        state = pf._state_dict()
        self.assertAlmostEqual(state["fees_paid"], 3.7780209895052472, places=6)  # 1.8 + 1.978
        self.assertAlmostEqual(state["slippage_paid"], 1.8890554722636838, places=6)
        pf.save()
        pf2 = PaperPortfolio(555.0, self.state_path)
        self.assertTrue(pf2.load())
        self.assertAlmostEqual(pf2.fees_paid, 3.7780209895052472, places=6)
        self.assertAlmostEqual(pf2.slippage_paid, 1.8890554722636838, places=6)

    def test_zero_rates_are_perfect_limit_fills(self):
        # slippage_rate=0 is explicitly allowed (perfect limit fills).
        pf = PaperPortfolio(10000.0, self.state_path, fee_rate=0.0, slippage_rate=0.0)
        res = pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW)
        self.assertEqual(res["qty"], 18.0)
        self.assertEqual(res["fees"], 0.0)
        self.assertEqual(res["slippage"], 0.0)
        self.assertEqual(pf.position["entry_price"], 100.0)
        self.assertAlmostEqual(pf.cash, 8200.0)
        res = pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000)
        self.assertAlmostEqual(res["realized_pnl"], 180.0)


class DecisionIdTests(unittest.TestCase):
    """M0 / F-P1-3: decision_id propagates into every trade record."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "paper.json")

    def _trades(self):
        return [json.loads(line) for line in
                Path(self.state_path + ".trades.jsonl").read_text().splitlines()]

    def test_decision_id_in_trade_rows(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.18}, "BTC/USDT", 100.0, NOW,
                        decision_id="decid0000001")
        pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000,
                        decision_id="decid0000002")
        trades = self._trades()
        self.assertEqual([t["decision_id"] for t in trades],
                         ["decid0000001", "decid0000002"])

    def test_decision_id_falls_back_to_action_dict(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.18,
                         "decision_id": "fromaction01"}, "BTC/USDT", 100.0, NOW)
        self.assertEqual(self._trades()[0]["decision_id"], "fromaction01")
        # absent everywhere -> None recorded, never raises
        pf.apply_action({"action": "exit"}, "BTC/USDT", 110.0, NOW + 1000)
        self.assertIsNone(self._trades()[-1]["decision_id"])

    def test_new_decision_id_unique(self):
        a, b = new_decision_id(), new_decision_id()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 12)


class AtomicWriteTests(unittest.TestCase):
    """M0 / F-P2: a crash mid-write leaves the previous state intact and parseable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "paper.json")

    def _old_state(self):
        PaperPortfolio(10000.0, self.state_path).save()
        return Path(self.state_path).read_text()

    def test_write_failure_leaves_old_state_intact(self):
        old = self._old_state()
        pf = PaperPortfolio(20000.0, self.state_path)
        with mock.patch("jev_config.Path.write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pf.save()
        now = Path(self.state_path).read_text()
        self.assertEqual(now, old)  # untouched
        self.assertEqual(json.loads(now)["cash"], 10000.0)  # parseable
        self.assertFalse(Path(self.state_path + ".tmp").exists())  # no stray tmp

    def test_replace_failure_leaves_old_state_intact(self):
        old = self._old_state()
        pf = PaperPortfolio(20000.0, self.state_path)
        with mock.patch("jev_config.os.replace", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                pf.save()
        self.assertEqual(Path(self.state_path).read_text(), old)
        self.assertEqual(json.loads(old)["cash"], 10000.0)
        self.assertFalse(Path(self.state_path + ".tmp").exists())


class HysteresisPersistence(unittest.TestCase):
    """M3: exit-signal counters + position age persist in the book state JSON."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "paper.json")

    def test_counters_persist_across_restart(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.20}, "BTC/USDT", 100.0, NOW)
        # one dump-signal cycle -> hold; counters advance and persist
        act = decide(_enter_verdict(dump_0_100=70.0), pf.to_pf_state(100.0), CFG, NOW + 1)
        self.assertEqual((act["action"], act["exit_signal_cycles"], act["cycles_held"]),
                         ("skip", 1, 1))
        pf.apply_action(act, "BTC/USDT", 100.0, NOW + 1)
        pf.save()

        pf2 = PaperPortfolio(555.0, self.state_path)  # restart from the JSON fixture
        self.assertTrue(pf2.load())
        self.assertEqual((pf2.exit_signal_cycles, pf2.cycles_held), (1, 1))
        # the streak is not forgotten across the restart (age 2 < 3 still holds)
        act2 = decide(_enter_verdict(dump_0_100=70.0), pf2.to_pf_state(100.0), CFG, NOW + 2)
        self.assertEqual((act2["action"], act2["exit_signal_cycles"], act2["cycles_held"]),
                         ("skip", 2, 2))
        pf2.apply_action(act2, "BTC/USDT", 100.0, NOW + 2)
        # third consecutive dump signal at age 3 -> exit fires
        act3 = decide(_enter_verdict(dump_0_100=70.0), pf2.to_pf_state(100.0), CFG, NOW + 3)
        self.assertEqual(act3["action"], "exit")

    def test_min_hold_blocks_early_signal_exit(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        pf.apply_action({"action": "enter", "size_fraction": 0.20}, "BTC/USDT", 100.0, NOW)
        act = decide(_enter_verdict(dump_0_100=75.0), pf.to_pf_state(100.0), CFG, NOW + 1)
        self.assertEqual((act["action"], act["reason"]), ("skip", "hold"))  # age 1 < 3

    def test_trade_rows_carry_size_tier(self):
        pf = PaperPortfolio(10000.0, self.state_path)
        act = decide(_enter_verdict(), pf.to_pf_state(100.0), CFG, NOW)
        self.assertEqual(act["size_tier"], 100)  # conf 0.9 -> 100% tier
        pf.apply_action(act, "BTC/USDT", 100.0, NOW)
        trades = [json.loads(line) for line in
                  Path(self.state_path + ".trades.jsonl").read_text().splitlines()]
        self.assertEqual(trades[0]["size_tier"], 100)


class SummaryScriptTests(unittest.TestCase):
    """M0 acceptance: jev_summary.py reads trades + decision logs offline."""

    def test_summary_reports_books_and_veto_table(self):
        import jev_summary

        with tempfile.TemporaryDirectory() as d:
            portfolio = PaperPortfolio(10000.0, os.path.join(d, "paper.json"))
            scorer = mock.Mock()
            scorer.score.side_effect = [_enter_verdict(), _exit_verdict()]
            exchange = mock.Mock()
            exchange.fetch_ticker.side_effect = [{"last": 100.0}, {"last": 110.0}]
            with redirect_stdout(io.StringIO()):
                run_cycle(scorer, portfolio, CFG, exchange, "BTC/USDT", NOW)
            portfolio.cycles_held = 2  # M3 min hold before the signal exit
            with redirect_stdout(io.StringIO()):
                run_cycle(scorer, portfolio, CFG, exchange, "BTC/USDT", NOW + 2_000_000)
            # one more cycle that is vetoed (low_pump -> bit 256 in the table)
            scorer.score.side_effect = None
            scorer.score.return_value = _enter_verdict(pump_0_100=10.0)
            with redirect_stdout(io.StringIO()):
                run_cycle(scorer, portfolio, CFG, exchange, "BTC/USDT", NOW + 4_000_000)

            out = io.StringIO()
            with redirect_stdout(out):
                jev_summary.main(["--runtime-dir", d])
        text = out.getvalue()
        self.assertIn("spot", text)
        self.assertIn("round trips:", text)
        self.assertIn("gross PnL:", text)
        self.assertIn("fees paid:", text)
        self.assertIn("slippage paid:", text)
        self.assertIn("net PnL:", text)
        self.assertIn("current equity:", text)
        self.assertIn("193.60", text)  # fee+slippage-aware net at the 100% size tier
        self.assertIn("low_pump", text)  # per-gate veto table from the bitmask


if __name__ == "__main__":
    unittest.main()