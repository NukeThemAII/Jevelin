#!/usr/bin/env python3
"""Tests for jev_perps (PerpsPortfolio + decide_perps) and the dual-book paper_loop cycle.

No network, no orders: exchanges are mocks. Run: .venv/bin/python scripts/test_jev_perps.py -v
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
from jev_paper import PaperPortfolio
from jev_perps import (
    FUNDING_PERIOD_MS, PerpsConfig, PerpsPortfolio, PerpsPortfolioState, decide_perps,
)
from paper_loop import parse_args, run_cycle

NOW = 1_800_000_000_000
CFG = PerpsConfig()
SYM = "BTC/USDT"


def _verdict(**overrides):
    v = {
        "ok": True, "error": None, "symbol": SYM,
        "pump_0_100": 70.0, "dump_0_100": 10.0, "phase": "breakout",
        "exhaustion_prob": 0.2, "whipsaw_prob": 0.3, "confidence": 0.9,
        "answers": {}, "latency_ms": 10.0,
    }
    v.update(overrides)
    return v


def _long_v(**kw):
    return _verdict(**kw)


def _short_v(**kw):
    base = {"pump_0_100": 10.0, "dump_0_100": 75.0, "phase": "distribution"}
    base.update(kw)
    return _verdict(**base)


def _flat(**kw):
    base = dict(has_position=False, equity_usd=10000.0, daily_pnl_pct=0.0,
                last_entry_ts_ms=None, side=None)
    base.update(kw)
    return PerpsPortfolioState(**base)


def _held(side, **kw):
    return _flat(has_position=True, side=side, **kw)


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "perps.json")

    def _pf(self, equity=10000.0):
        return PerpsPortfolio(equity, self.state_path)

    def _trades(self):
        return [json.loads(l) for l in
                Path(self.state_path + ".trades.jsonl").read_text().splitlines()]


class EnterSizing(_TmpCase):
    def test_long_enter_sizing_and_leverage_clamp(self):
        act = decide_perps(_long_v(), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "enter_long")
        self.assertEqual(act["size_fraction"], 0.09)  # round(0.10 * 0.9, 4)
        self.assertEqual(act["leverage"], 3.0)
        self.assertIsNone(act["vetoed_by"])

        pf = self._pf()
        act = dict(act, leverage=10.0)  # attempt to exceed the cap
        res = pf.apply_action(act, SYM, 100.0, NOW, 0.00005)
        self.assertEqual(res["executed"], "enter_long")
        pos = pf.position
        self.assertEqual(pos["side"], "long")
        self.assertEqual(pos["leverage"], 3.0)  # clamped, never exceeds
        self.assertAlmostEqual(pos["margin_usd"], 900.0)  # 0.09 * 10000
        self.assertAlmostEqual(pos["notional_usd"], 2700.0)  # 900 * 3
        self.assertAlmostEqual(pos["qty"], 27.0)  # 2700 / 100
        self.assertAlmostEqual(pos["stop_price"], 98.0)  # 100*(1-0.02)
        self.assertAlmostEqual(pos["liq_price"], 100.0 * (1 - 0.95 / 3))
        self.assertEqual(pos["funding_rate_at_entry"], 0.00005)
        self.assertEqual(pf.last_entry_ts_ms, NOW)
        self.assertAlmostEqual(pf.mark_to_market(110.0), 10270.0)  # +10*27

    def test_size_fraction_hard_capped(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_long", "size_fraction": 0.5, "leverage": 2.0},
                        SYM, 100.0, NOW)
        self.assertAlmostEqual(pf.position["margin_usd"], 1000.0)  # capped at 10%
        self.assertEqual(pf.position["leverage"], 2.0)  # below cap -> honoured

    def test_short_enter_sizing_and_inverse_pnl(self):
        act = decide_perps(_short_v(), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "enter_short")
        self.assertEqual(act["size_fraction"], 0.09)
        pf = self._pf()
        pf.apply_action(act, SYM, 100.0, NOW)
        pos = pf.position
        self.assertEqual(pos["side"], "short")
        self.assertAlmostEqual(pos["qty"], 27.0)
        self.assertAlmostEqual(pos["stop_price"], 102.0)
        self.assertAlmostEqual(pos["liq_price"], 100.0 * (1 + 0.95 / 3))
        self.assertAlmostEqual(pf.mark_to_market(90.0), 10270.0)  # (100-90)*27
        self.assertAlmostEqual(pf.mark_to_market(101.0), 9973.0)  # (100-101)*27
        res = pf.apply_action({"action": "exit"}, SYM, 90.0, NOW + 1000)
        self.assertEqual(res["executed"], "exit")
        self.assertAlmostEqual(res["realized_pnl"], 270.0)
        self.assertAlmostEqual(pf.equity, 10270.0)
        self.assertIsNone(pf.position)


class AutomaticExits(_TmpCase):
    def test_stop_loss_long(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_long", "size_fraction": 0.09}, SYM, 100.0, NOW)
        res = pf.apply_action({"action": "skip"}, SYM, 97.9, NOW + 1000)  # below 98
        self.assertEqual(res["executed"], "exit")
        self.assertEqual(res["detail"], "stop_loss")
        self.assertAlmostEqual(res["realized_pnl"], -2.1 * 27)
        self.assertAlmostEqual(pf.equity, 10000.0 - 2.1 * 27)
        self.assertIsNone(pf.position)

    def test_stop_loss_short(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_short", "size_fraction": 0.09}, SYM, 100.0, NOW)
        res = pf.apply_action({"action": "skip"}, SYM, 102.5, NOW + 1000)  # above 102
        self.assertEqual(res["detail"], "stop_loss")
        self.assertAlmostEqual(res["realized_pnl"], -2.5 * 27)
        self.assertIsNone(pf.position)
        self.assertEqual(self._trades()[-1]["action"], "stop_loss")

    def test_no_stop_inside_band(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_long", "size_fraction": 0.09}, SYM, 100.0, NOW)
        res = pf.apply_action({"action": "skip"}, SYM, 98.5, NOW + 1000)
        self.assertIsNone(res["executed"])
        self.assertIsNotNone(pf.position)

    def test_liquidation_loses_whole_margin_both_sides(self):
        for side, gap_price in (("long", 60.0), ("short", 140.0)):
            with self.subTest(side=side):
                pf = PerpsPortfolio(10000.0, self.state_path + side)
                pf.apply_action({"action": f"enter_{side}", "size_fraction": 0.09},
                                SYM, 100.0, NOW)
                # liquidation has priority over stop and over an explicit exit
                res = pf.apply_action({"action": "exit"}, SYM, gap_price, NOW + 1000)
                self.assertEqual(res["executed"], "exit")
                self.assertEqual(res["detail"], "liquidated")
                self.assertAlmostEqual(res["realized_pnl"], -900.0)
                self.assertAlmostEqual(pf.equity, 9100.0)
                self.assertIsNone(pf.position)


class Funding(_TmpCase):
    def _hold(self, side, rate, duration_ms):
        pf = self._pf()
        pf.apply_action({"action": f"enter_{side}", "size_fraction": 0.09}, SYM, 100.0, NOW,
                        rate)
        return pf, pf.apply_action({"action": "exit"}, SYM, 100.0, NOW + duration_ms)

    def test_long_pays_positive_funding_three_periods(self):
        pf, res = self._hold("long", 0.0001, 3 * FUNDING_PERIOD_MS + 5000)
        self.assertAlmostEqual(res["funding_paid"], 0.0001 * 2700 * 3)  # 0.81 paid
        self.assertAlmostEqual(pf.equity, 10000.0 - 0.81)
        self.assertAlmostEqual(self._trades()[-1]["funding_paid"], 0.81)

    def test_short_receives_positive_funding_three_periods(self):
        pf, res = self._hold("short", 0.0001, 3 * FUNDING_PERIOD_MS + 5000)
        self.assertAlmostEqual(res["funding_paid"], -0.81)  # negative paid = received
        self.assertAlmostEqual(pf.equity, 10000.0 + 0.81)

    def test_no_funding_under_8h(self):
        pf, res = self._hold("long", 0.0001, FUNDING_PERIOD_MS - 1)
        self.assertEqual(res["funding_paid"], 0.0)
        self.assertAlmostEqual(pf.equity, 10000.0)

    def test_funding_veto_long(self):
        act = decide_perps(_long_v(), _flat(), CFG, NOW, 0.0002)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"], "funding")
        self.assertEqual(decide_perps(_long_v(), _flat(), CFG, NOW, 0.0001)["action"],
                         "enter_long")  # exactly at threshold is allowed
        self.assertEqual(decide_perps(_long_v(), _flat(), CFG, NOW, -0.0005)["action"],
                         "enter_long")

    def test_funding_veto_short(self):
        act = decide_perps(_short_v(), _flat(), CFG, NOW, -0.0002)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"], "funding")
        self.assertEqual(decide_perps(_short_v(), _flat(), CFG, NOW, -0.0001)["action"],
                         "enter_short")
        self.assertEqual(decide_perps(_short_v(), _flat(), CFG, NOW, 0.0005)["action"],
                         "enter_short")

    def test_funding_none_fails_open(self):
        for fr in (None, float("nan"), "x"):
            self.assertEqual(decide_perps(_long_v(), _flat(), CFG, NOW, fr)["action"],
                             "enter_long")


class DecisionRules(unittest.TestCase):
    def test_short_requires_dump_60(self):
        act = decide_perps(_short_v(dump_0_100=59.9), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "skip")
        self.assertIn(act["vetoed_by"], ("low_dump", "low_pump"))
        self.assertEqual(decide_perps(_short_v(dump_0_100=60.0), _flat(), CFG, NOW)["action"],
                         "enter_short")

    def test_capitulation_blocks_long_only(self):
        act = decide_perps(_long_v(phase="capitulation"), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"], "capitulation")
        act = decide_perps(_short_v(phase="capitulation"), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "enter_short")
        # both signals high + capitulation -> long blocked, short taken
        act = decide_perps(_verdict(pump_0_100=80.0, dump_0_100=70.0, phase="capitulation"),
                           _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "enter_short")

    def test_both_sides_prefer_stronger(self):
        self.assertEqual(decide_perps(_verdict(pump_0_100=70.0, dump_0_100=70.0), _flat(),
                                      CFG, NOW)["action"], "enter_long")
        self.assertEqual(decide_perps(_verdict(pump_0_100=65.0, dump_0_100=80.0), _flat(),
                                      CFG, NOW)["action"], "enter_short")

    def test_shared_entry_gates(self):
        for kw, veto in (({"whipsaw_prob": 0.51}, "high_whipsaw"),
                         ({"exhaustion_prob": 0.61}, "high_exhaustion"),
                         ({"confidence": 0.59}, "low_confidence")):
            for maker in (_long_v, _short_v):
                act = decide_perps(maker(**kw), _flat(), CFG, NOW)
                self.assertEqual((act["action"], act["vetoed_by"]), ("skip", veto))
        act = decide_perps(_long_v(), _flat(last_entry_ts_ms=NOW - 1000), CFG, NOW)
        self.assertEqual(act["vetoed_by"], "cooldown")
        act = decide_perps(_long_v(), _flat(last_entry_ts_ms=NOW - 900_000), CFG, NOW)
        self.assertEqual(act["action"], "enter_long")

    def test_no_flip(self):
        # holding long, strong short signal: exit only, never enter_short
        act = decide_perps(_short_v(), _held("long"), CFG, NOW)
        self.assertEqual(act["action"], "exit")
        # holding short, strong long signal: exit only, never enter_long
        act = decide_perps(_long_v(), _held("short"), CFG, NOW)
        self.assertEqual(act["action"], "exit")
        # holding long, long signal: hold (no pyramiding)
        act = decide_perps(_long_v(), _held("long"), CFG, NOW)
        self.assertEqual((act["action"], act["reason"]), ("skip", "hold"))
        act = decide_perps(_short_v(), _held("short"), CFG, NOW)
        self.assertEqual((act["action"], act["reason"]), ("skip", "hold"))
        # exhaustion exits both sides
        for side in ("long", "short"):
            act = decide_perps(_verdict(pump_0_100=10.0, exhaustion_prob=0.85), _held(side),
                               CFG, NOW)
            self.assertEqual(act["action"], "exit")
        # portfolio level: enter while holding is refused
        with tempfile.TemporaryDirectory() as d:
            pf = PerpsPortfolio(10000.0, os.path.join(d, "p.json"))
            pf.apply_action({"action": "enter_long", "size_fraction": 0.09}, SYM, 100.0, NOW)
            res = pf.apply_action({"action": "enter_short", "size_fraction": 0.09}, SYM,
                                  100.0, NOW + 1)
            self.assertIsNone(res["executed"])
            self.assertEqual(pf.position["side"], "long")

    def test_daily_loss_kill_blocks_entries_not_exits(self):
        for maker in (_long_v, _short_v):
            act = decide_perps(maker(), _flat(daily_pnl_pct=-5.0), CFG, NOW)
            self.assertEqual((act["action"], act["vetoed_by"]), ("skip", "daily_loss_kill"))
        act = decide_perps(_short_v(), _held("long", daily_pnl_pct=-7.0), CFG, NOW)
        self.assertEqual(act["action"], "exit")
        act = decide_perps(_long_v(), _held("short", daily_pnl_pct=-7.0), CFG, NOW)
        self.assertEqual(act["action"], "exit")

    def test_malformed_fail_open(self):
        cases = [
            (None, "malformed"), ("x", "malformed"), ([], "malformed"),
            ({"ok": False, "error": "boom"}, "no_verdict"),
            (_verdict(confidence=None), "no_verdict"),
            (_verdict(pump_0_100=float("nan")), "malformed"),
            (_verdict(dump_0_100=float("inf")), "malformed"),
            (_verdict(whipsaw_prob="0.1"), "malformed"),
            (_verdict(confidence=True), "malformed"),
            (_verdict(phase=3), "malformed"),
        ]
        v = _verdict()
        del v["exhaustion_prob"]
        cases.append((v, "malformed"))
        for verdict, veto in cases:
            act = decide_perps(verdict, _flat(), CFG, NOW, None)
            self.assertEqual((act["action"], act["vetoed_by"]), ("skip", veto), verdict)
        act = decide_perps(_long_v(), None, CFG, NOW)  # broken pf -> never raises
        self.assertEqual((act["action"], act["vetoed_by"]), ("skip", "malformed"))

    def test_apply_action_bad_input_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            pf = PerpsPortfolio(10000.0, os.path.join(d, "p.json"))
            for price in (0, -1, None, "x", float("nan")):
                res = pf.apply_action({"action": "enter_long", "size_fraction": 0.05}, SYM,
                                      price, NOW)
                self.assertIsNone(res["executed"])
            for act in (None, {"action": "skip"}, {"action": "exit"},
                        {"action": "enter_long", "size_fraction": "x"},
                        {"action": "enter_long", "size_fraction": 0.05, "leverage": -2}):
                self.assertIsNone(pf.apply_action(act, SYM, 100.0, NOW)["executed"])
            self.assertIsNone(pf.position)
            self.assertEqual(pf.equity, 10000.0)


class Persistence(_TmpCase):
    def test_roundtrip(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_short", "size_fraction": 0.08, "reason": "t"},
                        SYM, 100.0, NOW, -0.00003)
        pf.equity = 9876.5
        pf.save()
        self.assertFalse(Path(self.state_path + ".tmp").exists())  # atomic replace
        raw = json.loads(Path(self.state_path).read_text())
        self.assertEqual(set(raw), {"equity", "position", "last_entry_ts_ms", "day",
                                    "day_start_equity"})

        pf2 = PerpsPortfolio(555.0, self.state_path)
        self.assertTrue(pf2.load())
        self.assertEqual(pf2.equity, 9876.5)
        self.assertEqual(pf2.position, pf.position)
        self.assertEqual(pf2.last_entry_ts_ms, NOW)
        self.assertEqual(pf2.day, pf.day)
        self.assertEqual(pf2.day_start_equity, pf.day_start_equity)
        self.assertEqual(pf2.to_pf_state(100.0).side, "short")

        t = self._trades()[0]
        self.assertEqual(set(t), {"ts_ms", "symbol", "side", "action", "price", "qty",
                                  "notional", "leverage", "realized_pnl", "funding_paid",
                                  "reason"})
        self.assertEqual((t["side"], t["action"], t["leverage"]), ("short", "enter_short", 3.0))

    def test_load_restores_missing_stop(self):
        Path(self.state_path).write_text(json.dumps({
            "equity": 10000.0, "last_entry_ts_ms": NOW, "day": "2099-01-01",
            "day_start_equity": 10000.0,
            "position": {"symbol": SYM, "side": "long", "margin_usd": 500.0,
                         "leverage": 9.0, "notional_usd": 1500.0, "qty": 15.0,
                         "entry_price": 100.0, "entry_ts_ms": NOW},
        }))
        pf = self._pf()
        self.assertTrue(pf.load())
        self.assertEqual(pf.position["leverage"], 3.0)  # clamped on load
        self.assertAlmostEqual(pf.position["stop_price"], 98.0)  # stop is mandatory
        self.assertAlmostEqual(pf.position["liq_price"], 100.0 * (1 - 0.95 / 3))

    def test_load_missing_or_corrupt(self):
        pf = PerpsPortfolio(10000.0, os.path.join(self.tmp.name, "nope.json"))
        self.assertFalse(pf.load())
        Path(self.state_path).write_text("{not json")
        pf = self._pf()
        self.assertFalse(pf.load())
        self.assertEqual(pf.equity, 10000.0)
        self.assertIsNone(pf.position)


class DualBookCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spot = PaperPortfolio(10000.0, os.path.join(self.tmp.name, "spot.json"))
        self.perps = PerpsPortfolio(10000.0, os.path.join(self.tmp.name, "perps.json"))

    def test_one_verdict_drives_both_books(self):
        scorer = mock.Mock()
        scorer.score.return_value = _long_v()
        exchange = mock.Mock()
        exchange.fetch_ticker.return_value = {"last": 100.0}
        funding = mock.Mock()
        funding.fetch_funding_rate.return_value = {"fundingRate": 0.00005}
        out = io.StringIO()
        with redirect_stdout(out):
            r = run_cycle(scorer, self.spot, RiskConfig(), exchange, SYM, NOW,
                          self.perps, CFG, funding)
        self.assertEqual(scorer.score.call_count, 1)  # ONE verdict per cycle
        self.assertEqual(r["result"]["executed"], "enter")
        self.assertEqual(r["perps"]["result"]["executed"], "enter_long")
        self.assertEqual(r["perps"]["funding_rate"], 0.00005)
        self.assertEqual(self.perps.position["funding_rate_at_entry"], 0.00005)
        text = out.getvalue()
        self.assertIn("spot: ", text)
        self.assertIn("perps: ", text)
        self.assertIn("funding=0.000050", text)
        self.assertTrue(Path(self.perps.state_path).exists())

    def test_funding_failure_fails_open(self):
        scorer = mock.Mock()
        scorer.score.return_value = _short_v()
        exchange = mock.Mock()
        exchange.fetch_ticker.return_value = {"last": 100.0}
        funding = mock.Mock()
        funding.fetch_funding_rate.side_effect = RuntimeError("net down")
        out = io.StringIO()
        with redirect_stdout(out):
            r = run_cycle(scorer, self.spot, RiskConfig(), exchange, SYM, NOW,
                          self.perps, CFG, funding)
        self.assertIsNone(r["perps"]["funding_rate"])
        self.assertEqual(r["perps"]["result"]["executed"], "enter_short")
        self.assertEqual(r["result"]["executed"], None)  # spot: long-only, low pump
        self.assertIn("funding=na", out.getvalue())

    def test_perps_disabled_keeps_spot_only(self):
        scorer = mock.Mock()
        scorer.score.return_value = _long_v()
        exchange = mock.Mock()
        exchange.fetch_ticker.return_value = {"last": 100.0}
        with redirect_stdout(io.StringIO()) as out:
            r = run_cycle(scorer, self.spot, RiskConfig(), exchange, SYM, NOW)
        self.assertNotIn("perps", r)
        self.assertNotIn("perps:", out.getvalue())

    def test_cli_args(self):
        a = parse_args([])
        self.assertTrue(a.perps)
        self.assertEqual(a.perps_state, "runtime/perps_btc.json")
        self.assertEqual(a.state, "runtime/paper_btc.json")
        a = parse_args(["--no-perps", "--perps-state", "/tmp/x.json"])
        self.assertFalse(a.perps)
        self.assertEqual(a.perps_state, "/tmp/x.json")


if __name__ == "__main__":
    unittest.main()
