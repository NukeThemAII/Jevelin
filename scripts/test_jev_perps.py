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

from jev_config import VetoFlags
from jev_gates import RiskConfig
from jev_paper import PaperPortfolio
from jev_perps import (
    FUNDING_PERIOD_MS, PerpsConfig, PerpsPortfolio, PerpsPortfolioState, decide_perps,
)
from paper_loop import parse_args, run_cycle

NOW = 1_800_000_000_000
CFG = PerpsConfig()
SYM = "BTC/USDT"
EXPECTED_FLAG = {
    "high_whipsaw": VetoFlags.WHIPSAW,
    "high_exhaustion": VetoFlags.EXHAUSTION,
    "low_confidence": VetoFlags.LOW_CONFIDENCE,
    "low_pump": VetoFlags.LOW_PUMP,
    "low_dump": VetoFlags.LOW_DUMP,
    "daily_loss_kill": VetoFlags.DAILY_LOSS_KILL,
    "cooldown": VetoFlags.COOLDOWN,
}


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
    base = {"pump_0_100": 10.0, "dump_0_100": 75.0, "phase": "breakout"}
    base.update(kw)
    return _verdict(**base)


def _flat(**kw):
    base = dict(has_position=False, equity_usd=10000.0, daily_pnl_pct=0.0,
                last_entry_ts_ms=None, side=None, exit_signal_cycles=0, cycles_held=0)
    base.update(kw)
    return PerpsPortfolioState(**base)


def _held(side, **kw):
    # default: min hold satisfied (age 4) so exit-rule assertions are not
    # confounded by the 3-cycle min hold
    base = {"cycles_held": 3}
    base.update(kw)
    return _flat(has_position=True, side=side, **base)


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
        self.assertEqual(act["size_fraction"], 0.10)  # conf 0.9 -> 100% tier of 10% cap
        self.assertEqual(act["size_tier"], 100)
        self.assertEqual(act["leverage"], 3.0)
        self.assertEqual(act["vetoed_by"], [])

        pf = self._pf()
        # money math below uses an explicit 0.09 margin fraction (M0 fixture)
        act = dict(act, size_fraction=0.09, leverage=10.0)  # attempt to exceed the cap
        res = pf.apply_action(act, SYM, 100.0, NOW, 0.00005)
        self.assertEqual(res["executed"], "enter_long")
        pos = pf.position
        self.assertEqual(pos["side"], "long")
        self.assertEqual(pos["leverage"], 3.0)  # clamped, never exceeds
        self.assertAlmostEqual(pos["margin_usd"], 900.0)  # 0.09 * 10000
        self.assertAlmostEqual(pos["notional_usd"], 2700.0)  # 900 * 3
        self.assertAlmostEqual(pos["qty"], 26.986506746626688)  # 2700 / 100.05 fill
        self.assertAlmostEqual(pos["entry_price"], 100.05)  # buy-side entry fills UP
        self.assertAlmostEqual(pos["stop_price"], 98.049)  # 100.05*(1-0.02)
        self.assertAlmostEqual(pos["liq_price"], 100.05 * (1 - 0.95 / 3))
        self.assertEqual(pos["funding_rate_at_entry"], 0.00005)
        self.assertEqual(pf.last_entry_ts_ms, NOW)
        self.assertAlmostEqual(res["fees"], 1.35)  # 0.0005 taker * 2700 notional
        self.assertAlmostEqual(res["slippage"], 1.3493253373312577)  # (100.05-100) * qty
        self.assertAlmostEqual(pf.equity, 9998.65)  # entry fee charged immediately
        self.assertAlmostEqual(pf.mark_to_market(110.0), 10267.165742128935)

    def test_size_fraction_hard_capped(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_long", "size_fraction": 0.5, "leverage": 2.0},
                        SYM, 100.0, NOW)
        self.assertAlmostEqual(pf.position["margin_usd"], 1000.0)  # capped at 10%
        self.assertEqual(pf.position["leverage"], 2.0)  # below cap -> honoured

    def test_short_enter_sizing_and_inverse_pnl(self):
        act = decide_perps(_short_v(), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "enter_short")
        self.assertEqual(act["size_fraction"], 0.10)  # conf 0.9 -> 100% tier
        self.assertEqual(act["size_tier"], 100)
        pf = self._pf()
        pf.apply_action(dict(act, size_fraction=0.09), SYM, 100.0, NOW)
        pos = pf.position
        self.assertEqual(pos["side"], "short")
        self.assertAlmostEqual(pos["qty"], 27.01350675337669)  # 2700 / 99.95 fill
        self.assertAlmostEqual(pos["entry_price"], 99.95)  # sell-side entry fills DOWN
        self.assertAlmostEqual(pos["stop_price"], 101.949)  # 99.95*1.02
        self.assertAlmostEqual(pos["liq_price"], 99.95 * (1 + 0.95 / 3))
        self.assertAlmostEqual(pf.mark_to_market(90.0), 10267.434392196097)
        self.assertAlmostEqual(pf.mark_to_market(101.0), 9970.285817908954)
        res = pf.apply_action({"action": "exit"}, SYM, 90.0, NOW + 1000)
        self.assertEqual(res["executed"], "exit")
        self.assertAlmostEqual(res["realized_pnl"], 265.00256878439217, places=6)  # net of fees
        self.assertAlmostEqual(pf.equity, 10265.002568784392, places=6)
        self.assertIsNone(pf.position)


class AutomaticExits(_TmpCase):
    def test_stop_loss_long(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_long", "size_fraction": 0.09}, SYM, 100.0, NOW)
        res = pf.apply_action({"action": "skip"}, SYM, 97.9, NOW + 1000)  # below 98
        self.assertEqual(res["executed"], "exit")
        self.assertEqual(res["detail"], "stop_loss")
        self.assertAlmostEqual(res["realized_pnl"], -62.01230802098903, places=6)  # net of fees
        self.assertAlmostEqual(pf.equity, 9937.987691979011, places=6)
        self.assertIsNone(pf.position)

    def test_stop_loss_short(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_short", "size_fraction": 0.09}, SYM, 100.0, NOW)
        res = pf.apply_action({"action": "skip"}, SYM, 102.5, NOW + 1000)  # above 102
        self.assertEqual(res["detail"], "stop_loss")
        self.assertAlmostEqual(res["realized_pnl"], -73.00401888444203, places=6)  # net of fees
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
                # whole margin lost + both fill fees (liq fill = computed liq price, as-is)
                realized, equity = {"long": (-902.2725, 9097.727499999999),
                                   "short": (-903.1275, 9096.8725)}[side]
                self.assertAlmostEqual(res["realized_pnl"], realized, places=6)
                self.assertAlmostEqual(pf.equity, equity, places=6)
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
        # round trip at quote 100 with fees+slippage, minus 0.81 funding
        self.assertAlmostEqual(pf.equity, 9993.792698650675, places=6)
        self.assertAlmostEqual(self._trades()[-1]["funding_paid"], 0.81)

    def test_short_receives_positive_funding_three_periods(self):
        pf, res = self._hold("short", 0.0001, 3 * FUNDING_PERIOD_MS + 5000)
        self.assertAlmostEqual(res["funding_paid"], -0.81)  # negative paid = received
        self.assertAlmostEqual(pf.equity, 9995.407298649325, places=6)

    def test_no_funding_under_8h(self):
        pf, res = self._hold("long", 0.0001, FUNDING_PERIOD_MS - 1)
        self.assertEqual(res["funding_paid"], 0.0)
        self.assertAlmostEqual(pf.equity, 9994.602698650675, places=6)  # costs only

    def test_funding_veto_long(self):
        act = decide_perps(_long_v(), _flat(), CFG, NOW, 0.0002)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"], ["funding", "low_dump"])  # all failing gates
        self.assertEqual(decide_perps(_long_v(), _flat(), CFG, NOW, 0.0001)["action"],
                         "enter_long")  # exactly at threshold is allowed
        self.assertEqual(decide_perps(_long_v(), _flat(), CFG, NOW, -0.0005)["action"],
                         "enter_long")

    def test_funding_veto_short(self):
        act = decide_perps(_short_v(), _flat(), CFG, NOW, -0.0002)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"], ["low_pump", "funding"])  # all failing gates
        self.assertEqual(decide_perps(_short_v(), _flat(), CFG, NOW, -0.0001)["action"],
                         "enter_short")
        self.assertEqual(decide_perps(_short_v(), _flat(), CFG, NOW, 0.0005)["action"],
                         "enter_short")

    def test_funding_none_fails_open(self):
        for fr in (None, float("nan"), "x"):
            self.assertEqual(decide_perps(_long_v(), _flat(), CFG, NOW, fr)["action"],
                             "enter_long")


class DecisionRules(unittest.TestCase):
    def test_short_requires_dump_65(self):
        act = decide_perps(_short_v(dump_0_100=64.9), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"], ["low_pump", "low_dump"])  # both sides blocked
        self.assertEqual(act["veto_bitmask"],
                         int(VetoFlags.LOW_PUMP | VetoFlags.LOW_DUMP))
        self.assertEqual(decide_perps(_short_v(dump_0_100=65.0), _flat(), CFG, NOW)["action"],
                         "enter_short")

    def test_phase_whitelist_blocks_all_sides(self):
        # B.3: entries need phase in {breakout, accumulation} — capitulation is
        # outside the set for BOTH sides; the capitulation flag stays (attribution).
        act = decide_perps(_long_v(phase="capitulation"), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"],
                         ["capitulation", "phase_not_in_entry_set", "low_dump"])
        act = decide_perps(_short_v(phase="capitulation"), _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "skip")
        self.assertEqual(act["vetoed_by"],
                         ["capitulation", "phase_not_in_entry_set", "low_pump"])
        # both signals high + capitulation -> still no entry
        act = decide_perps(_verdict(pump_0_100=80.0, dump_0_100=70.0, phase="capitulation"),
                           _flat(), CFG, NOW, None)
        self.assertEqual(act["action"], "skip")

    def test_entry_phase_whitelist(self):
        for phase in ("breakout", "accumulation"):
            self.assertEqual(decide_perps(_long_v(phase=phase), _flat(), CFG, NOW)["action"],
                             "enter_long")
        for phase in ("distribution", "ranging"):
            act = decide_perps(_long_v(phase=phase), _flat(), CFG, NOW, None)
            self.assertEqual(act["vetoed_by"][0], "phase_not_in_entry_set")

    def test_both_sides_prefer_stronger(self):
        self.assertEqual(decide_perps(_verdict(pump_0_100=70.0, dump_0_100=70.0), _flat(),
                                      CFG, NOW)["action"], "enter_long")
        self.assertEqual(decide_perps(_verdict(pump_0_100=65.0, dump_0_100=80.0), _flat(),
                                      CFG, NOW)["action"], "enter_short")

    def test_shared_entry_gates(self):
        # F-P1-4: ALL failing gates are recorded — the shared gate first, then the
        # side gate that blocks the opposite candidate side.
        for kw, veto in (({"whipsaw_prob": 0.51}, "high_whipsaw"),
                         ({"exhaustion_prob": 0.61}, "high_exhaustion"),
                         ({"confidence": 0.59}, "low_confidence")):
            act = decide_perps(_long_v(**kw), _flat(), CFG, NOW)
            self.assertEqual((act["action"], act["vetoed_by"]),
                             ("skip", [veto, "low_dump"]))
            self.assertEqual(act["veto_bitmask"],
                             int(EXPECTED_FLAG[veto] | VetoFlags.LOW_DUMP))
            act = decide_perps(_short_v(**kw), _flat(), CFG, NOW)
            self.assertEqual((act["action"], act["vetoed_by"]),
                             ("skip", [veto, "low_pump"]))
        act = decide_perps(_long_v(), _flat(last_entry_ts_ms=NOW - 1000), CFG, NOW)
        self.assertEqual(act["vetoed_by"], ["cooldown", "low_dump"])
        act = decide_perps(_long_v(), _flat(last_entry_ts_ms=NOW - 900_000), CFG, NOW)
        self.assertEqual(act["action"], "enter_long")
        self.assertEqual(act["veto_bitmask"], 0)  # an entry is never vetoed

    def test_no_flip(self):
        # holding long, strong short signal: exit only, never enter_short
        act = decide_perps(_short_v(), _held("long"), CFG, NOW)
        self.assertEqual(act["action"], "exit")
        # holding short, strong long signal: exit only, never enter_long
        act = decide_perps(_verdict(pump_0_100=80.0), _held("short"), CFG, NOW)
        self.assertEqual(act["action"], "exit")
        # holding long, long signal: hold (no pyramiding)
        act = decide_perps(_long_v(), _held("long"), CFG, NOW)
        self.assertEqual((act["action"], act["reason"]), ("skip", "hold"))
        act = decide_perps(_short_v(), _held("short"), CFG, NOW)
        self.assertEqual((act["action"], act["reason"]), ("skip", "hold"))
        # B.3 has no exhaustion exit: exhaustion alone holds (v1 exit superseded)
        for side in ("long", "short"):
            act = decide_perps(_verdict(pump_0_100=10.0, dump_0_100=10.0,
                                        exhaustion_prob=0.95), _held(side), CFG, NOW)
            self.assertEqual(act["action"], "skip", side)
        # min hold blocks signal exits before 3 cycles (stops/liq bypass elsewhere)
        act = decide_perps(_short_v(), _held("long", cycles_held=0), CFG, NOW)
        self.assertEqual((act["action"], act["reason"]), ("skip", "hold"))
        # portfolio level: enter while holding is refused
        with tempfile.TemporaryDirectory() as d:
            pf = PerpsPortfolio(10000.0, os.path.join(d, "p.json"))
            pf.apply_action({"action": "enter_long", "size_fraction": 0.09}, SYM, 100.0, NOW)
            res = pf.apply_action({"action": "enter_short", "size_fraction": 0.09}, SYM,
                                  100.0, NOW + 1)
            self.assertIsNone(res["executed"])
            self.assertEqual(pf.position["side"], "long")

    def test_daily_loss_kill_blocks_entries_not_exits(self):
        act = decide_perps(_long_v(), _flat(daily_pnl_pct=-5.0), CFG, NOW)
        self.assertEqual((act["action"], act["vetoed_by"]),
                         ("skip", ["daily_loss_kill", "low_dump"]))
        act = decide_perps(_short_v(), _flat(daily_pnl_pct=-5.0), CFG, NOW)
        self.assertEqual((act["action"], act["vetoed_by"]),
                         ("skip", ["daily_loss_kill", "low_pump"]))
        act = decide_perps(_short_v(), _held("long", daily_pnl_pct=-7.0), CFG, NOW)
        self.assertEqual(act["action"], "exit")
        act = decide_perps(_verdict(pump_0_100=80.0), _held("short", daily_pnl_pct=-7.0),
                           CFG, NOW)
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
            self.assertEqual((act["action"], act["vetoed_by"]), ("skip", [veto]), verdict)
        act = decide_perps(_long_v(), None, CFG, NOW)  # broken pf -> never raises
        self.assertEqual((act["action"], act["vetoed_by"]), ("skip", ["malformed"]))

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
                                    "day_start_equity", "fees_paid", "slippage_paid",
                                    "exit_signal_cycles", "cycles_held"})

        pf2 = PerpsPortfolio(555.0, self.state_path)
        self.assertTrue(pf2.load())
        self.assertEqual(pf2.equity, 9876.5)
        self.assertEqual(pf2.position, pf.position)
        self.assertEqual(pf2.last_entry_ts_ms, NOW)
        self.assertEqual(pf2.day, pf.day)
        self.assertEqual(pf2.day_start_equity, pf.day_start_equity)
        self.assertEqual(pf2.to_pf_state(100.0).side, "short")

        t = self._trades()[0]
        self.assertEqual(set(t), {"ts_ms", "decision_id", "book", "symbol", "side", "action",
                                  "price", "qty", "notional", "leverage", "realized_pnl",
                                  "funding_paid", "fees", "slippage", "reason", "size_tier"})
        self.assertEqual((t["side"], t["action"], t["leverage"]), ("short", "enter_short", 3.0))
        self.assertEqual(t["book"], "perps")
        self.assertAlmostEqual(t["fees"], 1.2)  # 0.0005 * 2400 notional
        self.assertAlmostEqual(t["slippage"], 1.2006003001500751)  # (100-99.95) * qty

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

    def test_hysteresis_counters_survive_restart(self):
        pf = self._pf()
        pf.apply_action({"action": "enter_long", "size_fraction": 0.09}, SYM, 100.0, NOW)
        act = decide_perps(_verdict(dump_0_100=70.0), pf.to_pf_state(100.0), CFG, NOW + 1)
        self.assertEqual((act["action"], act["exit_signal_cycles"], act["cycles_held"]),
                         ("skip", 1, 1))
        pf.apply_action(act, SYM, 100.0, NOW + 1)
        pf.save()

        pf2 = PerpsPortfolio(555.0, self.state_path)  # restart from the JSON fixture
        self.assertTrue(pf2.load())
        self.assertEqual((pf2.exit_signal_cycles, pf2.cycles_held), (1, 1))
        self.assertEqual(pf2.to_pf_state(100.0).side, "long")
        # the streak survives the restart: 2nd dump signal at age 2 (still min-hold)
        act2 = decide_perps(_verdict(dump_0_100=70.0), pf2.to_pf_state(100.0), CFG, NOW + 2)
        self.assertEqual((act2["action"], act2["exit_signal_cycles"]), ("skip", 2))
        pf2.apply_action(act2, SYM, 100.0, NOW + 2)
        # 3rd dump signal at age 3 -> the 2-consecutive rule fires
        act3 = decide_perps(_verdict(dump_0_100=70.0), pf2.to_pf_state(100.0), CFG, NOW + 3)
        self.assertEqual(act3["action"], "exit")
        res = pf2.apply_action(act3, SYM, 100.0, NOW + 3)
        self.assertEqual(res["executed"], "exit")
        self.assertEqual((pf2.exit_signal_cycles, pf2.cycles_held), (0, 0))  # reset


class RegimeGates(unittest.TestCase):
    """B.3 regime vetoes on the perps book."""

    def test_regime_chop_blocks_both_sides(self):
        for v, side_veto in ((_long_v(), "low_dump"), (_short_v(), "low_pump")):
            act = decide_perps(v, _flat(), CFG, NOW, None, regime="chop")
            self.assertEqual((act["action"], act["vetoed_by"]),
                             ("skip", ["regime_chop", side_veto]))
            self.assertEqual(act["veto_bitmask"],
                             int(VetoFlags.REGIME_CHOP | VetoFlags.LOW_DUMP
                                 if side_veto == "low_dump" else
                                 VetoFlags.REGIME_CHOP | VetoFlags.LOW_PUMP))

    def test_regime_counter_blocks_counter_side(self):
        act = decide_perps(_short_v(), _flat(), CFG, NOW, None, regime="trend_up")
        self.assertEqual((act["action"], act["vetoed_by"]),
                         ("skip", ["low_pump", "regime_counter"]))
        act = decide_perps(_long_v(), _flat(), CFG, NOW, None, regime="trend_down")
        self.assertEqual((act["action"], act["vetoed_by"]),
                         ("skip", ["regime_counter", "low_dump"]))
        self.assertEqual(decide_perps(_long_v(), _flat(), CFG, NOW, None,
                                      regime="trend_up")["action"], "enter_long")
        self.assertEqual(decide_perps(_short_v(), _flat(), CFG, NOW, None,
                                      regime="trend_down")["action"], "enter_short")

    def test_counter_trend_allow_config(self):
        cfg = PerpsConfig(counter_trend="allow")
        self.assertEqual(decide_perps(_short_v(), _flat(), cfg, NOW, None,
                                      regime="trend_up")["action"], "enter_short")

    def test_exits_never_regime_blocked(self):
        for regime in ("chop", "trend_up", "trend_down"):
            act = decide_perps(_verdict(dump_0_100=80.0), _held("long"), CFG, NOW, None,
                               regime=regime)
            self.assertEqual(act["action"], "exit", regime)


class FanoutPerps(unittest.TestCase):
    """B.3 whipsaw fan-out resolves as a shared gate on the perps book too."""

    def test_fanout_tie_vetoes_shared(self):
        v = _long_v(fan_out=1, whipsaw_prob=0.44, whipsaw_prob_2=0.5)
        act = decide_perps(v, _flat(), CFG, NOW, None)
        self.assertEqual(act["vetoed_by"], ["whipsaw_fanout_tie", "low_dump"])
        self.assertEqual(act["veto_bitmask"],
                         int(VetoFlags.WHIPSAW_FANOUT_TIE | VetoFlags.LOW_DUMP))

    def test_fanout_pass_pass_enters(self):
        v = _long_v(fan_out=1, whipsaw_prob=0.44, whipsaw_prob_2=0.42)
        self.assertEqual(decide_perps(v, _flat(), CFG, NOW, None)["action"], "enter_long")

    def test_fanout_fail_fail_vetoes(self):
        v = _long_v(fan_out=1, whipsaw_prob=0.5, whipsaw_prob_2=0.6)
        act = decide_perps(v, _flat(), CFG, NOW, None)
        self.assertEqual(act["vetoed_by"], ["high_whipsaw", "low_dump"])


class SizingTiers(unittest.TestCase):
    def test_tier_boundaries(self):
        for conf, frac, tier in ((0.65, 0.06, 60), (0.699, 0.06, 60), (0.70, 0.08, 80),
                                 (0.8499, 0.08, 80), (0.85, 0.10, 100)):
            act = decide_perps(_long_v(confidence=conf), _flat(), CFG, NOW, None)
            self.assertEqual((act["size_fraction"], act["size_tier"]),
                             (round(frac, 4), tier), conf)

    def test_trade_rows_carry_size_tier(self):
        with tempfile.TemporaryDirectory() as d:
            pf = PerpsPortfolio(10000.0, os.path.join(d, "p.json"))
            act = decide_perps(_long_v(), pf.to_pf_state(100.0), CFG, NOW, None)
            pf.apply_action(act, SYM, 100.0, NOW)
            rows = [json.loads(line) for line in
                    Path(d, "p.json.trades.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["size_tier"], 100)


class HysteresisExits(unittest.TestCase):
    """B.3 mirrored exit rule: pump x2 consecutive OR single >=75; min hold 3."""

    def test_long_exit_hysteresis(self):
        act = decide_perps(_verdict(dump_0_100=70.0), _held("long"), CFG, NOW)
        self.assertEqual((act["action"], act["exit_signal_cycles"]), ("skip", 1))
        act = decide_perps(_verdict(dump_0_100=70.0), _held("long", exit_signal_cycles=1),
                           CFG, NOW)
        self.assertEqual(act["action"], "exit")
        self.assertIn("consecutive", act["reason"])
        act = decide_perps(_verdict(dump_0_100=80.0), _held("long"), CFG, NOW)
        self.assertEqual(act["action"], "exit")

    def test_short_exit_hysteresis_mirrored(self):
        act = decide_perps(_verdict(pump_0_100=70.0), _held("short"), CFG, NOW)
        self.assertEqual((act["action"], act["exit_signal_cycles"]), ("skip", 1))
        act = decide_perps(_verdict(pump_0_100=70.0), _held("short", exit_signal_cycles=1),
                           CFG, NOW)
        self.assertEqual(act["action"], "exit")
        act = decide_perps(_verdict(pump_0_100=80.0), _held("short"), CFG, NOW)
        self.assertEqual(act["action"], "exit")

    def test_min_hold_blocks_early_signal_exits(self):
        for age in (0, 1):  # age_now in (1, 2) < 3
            act = decide_perps(_verdict(dump_0_100=90.0), _held("long", cycles_held=age),
                               CFG, NOW)
            self.assertEqual((act["action"], act["reason"]), ("skip", "hold"), age)

    def test_stops_bypass_min_hold(self):
        with tempfile.TemporaryDirectory() as d:
            pf = PerpsPortfolio(10000.0, os.path.join(d, "p.json"))
            pf.apply_action({"action": "enter_long", "size_fraction": 0.09}, SYM, 100.0, NOW)
            self.assertEqual(pf.cycles_held, 0)  # fresh position, min hold not met
            res = pf.apply_action({"action": "skip"}, SYM, 97.0, NOW + 5000)  # below stop
            self.assertEqual(res["detail"], "stop_loss")
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
        self.assertIn("decision=", text)  # M0: decision_id on every cycle line
        self.assertTrue(Path(self.perps.state_path).exists())

        # M0: ONE decision_id per cycle flows through both books' logs and trade rows.
        rows = [json.loads(l) for l in
                Path(self.tmp.name, "paper_decisions.jsonl").read_text().splitlines()]
        self.assertEqual([r["book"] for r in rows], ["spot", "perps"])
        self.assertEqual(len({r["decision_id"] for r in rows}), 1)
        spot_trades = [json.loads(l) for l in
                       Path(self.spot.state_path + ".trades.jsonl").read_text().splitlines()]
        perps_trades = [json.loads(l) for l in
                        Path(self.perps.state_path + ".trades.jsonl").read_text().splitlines()]
        self.assertEqual(spot_trades[0]["decision_id"], rows[0]["decision_id"])
        self.assertEqual(perps_trades[0]["decision_id"], rows[0]["decision_id"])
        self.assertEqual(rows[0]["veto_bitmask"], 0)

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
        self.assertIsNone(a.fee_rate)  # default -> built-in cost constants
        self.assertIsNone(a.slippage_rate)
        a = parse_args(["--no-perps", "--perps-state", "/tmp/x.json"])
        self.assertFalse(a.perps)
        self.assertEqual(a.perps_state, "/tmp/x.json")
        a = parse_args(["--fee-rate", "0.002", "--slippage-rate", "0.0"])
        self.assertEqual(a.fee_rate, 0.002)  # M0 override path
        self.assertEqual(a.slippage_rate, 0.0)

    def test_summary_shows_both_books(self):
        import jev_summary

        scorer = mock.Mock()
        scorer.score.return_value = _long_v()
        exchange = mock.Mock()
        exchange.fetch_ticker.return_value = {"last": 100.0}
        funding = mock.Mock()
        funding.fetch_funding_rate.return_value = {"fundingRate": 0.00005}
        with redirect_stdout(io.StringIO()):
            run_cycle(scorer, self.spot, RiskConfig(), exchange, SYM, NOW,
                      self.perps, CFG, funding)
        out = io.StringIO()
        with redirect_stdout(out):
            jev_summary.main(["--runtime-dir", self.tmp.name])
        text = out.getvalue()
        self.assertIn("spot", text)
        self.assertIn("perps", text)
        self.assertIn("round trips:", text)


# -- M5 portfolio risk gates (perps) ----------------------------------------

def _risk(**overrides):
    kw = dict(global_daily_kill=False, drawdown_halt=False,
              pair_remaining=10_000.0, basket_remaining_long=10_000.0,
              basket_remaining_short=10_000.0, min_position_pct=0.01)
    kw.update(overrides)
    return kw


class PortfolioRiskPerps(unittest.TestCase):
    """M5: pair/basket caps are side-aware; exits never risk-blocked."""

    def test_kill_blocks_both_sides(self):
        for v in (_long_v(), _short_v()):
            d = decide_perps(v, _flat(), CFG, NOW, None, regime="trend_up",
                             risk=_risk(global_daily_kill=True))
            self.assertEqual(d["action"], "skip")
            self.assertIn("global_daily_kill", d["vetoed_by"])

    def test_side_basket_caps(self):
        # long basket dry -> short entries still allowed on a strong short
        d = decide_perps(_short_v(), _flat(), CFG, NOW, None, regime="trend_down",
                         risk=_risk(basket_remaining_long=0.0))
        self.assertEqual(d["action"], "enter_short")
        # ... and the long side is vetoed with the basket_cap bit
        d = decide_perps(_long_v(), _flat(), CFG, NOW, None, regime="trend_up",
                         risk=_risk(basket_remaining_long=0.0))
        self.assertEqual(d["action"], "skip")
        self.assertIn("basket_cap", d["vetoed_by"])
        self.assertTrue(d["veto_bitmask"] & int(VetoFlags.BASKET_CAP))
        # both baskets dry -> shared veto on both sides
        for v in (_long_v(), _short_v()):
            d = decide_perps(v, _flat(), CFG, NOW, None, regime="trend_up",
                             risk=_risk(basket_remaining_long=0.0,
                                        basket_remaining_short=0.0))
            self.assertEqual(d["action"], "skip")
            self.assertIn("basket_cap", d["vetoed_by"])

    def test_margin_clamped_by_pair_cap(self):
        # margin target 0.10 x 10000 = 1000; pair remaining 250 -> 0.025
        d = decide_perps(_long_v(), _flat(), CFG, NOW, None, regime="trend_up",
                         risk=_risk(pair_remaining=250.0))
        self.assertEqual(d["action"], "enter_long")
        self.assertEqual(d["size_fraction"], 0.025)

    def test_margin_clamped_by_basket_cap(self):
        d = decide_perps(_short_v(), _flat(), CFG, NOW, None, regime="trend_down",
                         risk=_risk(basket_remaining_short=400.0))
        self.assertEqual(d["action"], "enter_short")
        self.assertEqual(d["size_fraction"], 0.04)

    def test_dust_veto(self):
        d = decide_perps(_long_v(), _flat(), CFG, NOW, None, regime="trend_up",
                         risk=_risk(pair_remaining=50.0))
        self.assertEqual(d["action"], "skip")
        self.assertIn("pair_cap", d["vetoed_by"])

    def test_exits_never_risk_blocked(self):
        d = decide_perps(_verdict(dump_0_100=80.0), _held("long"), CFG, NOW, None,
                         regime="trend_up",
                         risk=_risk(global_daily_kill=True, drawdown_halt=True))
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["vetoed_by"], [])
        self.assertEqual(d["veto_bitmask"], 0)


if __name__ == "__main__":
    unittest.main()
