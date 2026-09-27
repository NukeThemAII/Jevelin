#!/usr/bin/env python3
"""Tests for jev_calibrate (M4 calibration harness) — fixtures only, no network.

Every counterfactual expectation is hand-computed from the M0 fill constants
(spot 0.001 fee + 0.0005 adverse slippage; perps 0.0005 taker) and the
config/v2.yaml gate numbers (confidence tiers 60/80/100% of cap at 0.70/0.85,
exit bars 65/75, min hold 3 cycles, perps stop 2%); each fixture spells the
arithmetic out. Assertions run to 6 decimal places.

Run: .venv/bin/python scripts/test_jev_calibrate.py -v
"""
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_calibrate
import jev_import
import jev_store
from jev_config import (PERPS_TAKER_FEE_RATE, SLIPPAGE_RATE, SPOT_FEE_RATE,
                        V2Config, bitmask_for)

CFG = V2Config()  # dataclass defaults == config/v2.yaml (parity-tested)


def _verdict(pump=50.0, dump=50.0, conf=0.70, phase="breakout",
             whipsaw=0.30, exhaustion=0.30, **over):
    v = {"ok": True, "pump_0_100": pump, "dump_0_100": dump, "phase": phase,
         "exhaustion_prob": exhaustion, "whipsaw_prob": whipsaw,
         "confidence": conf}
    v.update(over)
    return v


def _decision(book="spot", ts_ms=1800000000000, price=100.0, equity=10000.0,
              verdict=None, action="skip", veto_bitmask=0, vetoed_by=(),
              decision_id="d-0", symbol="BTCUSDT", **over):
    row = {"ts_ms": ts_ms, "decision_id": decision_id, "book": book,
           "symbol": symbol, "price": price, "equity": equity, "action": action,
           "executed": None, "veto_bitmask": int(veto_bitmask),
           "vetoed_by": list(vetoed_by), "reason": "", "regime": "trend_up",
           "fan_out": False,
           "verdict": verdict if verdict is not None else _verdict()}
    row.update(over)
    return row


# -- fixture 1: spot long that would have won ---------------------------------
# vetoed entry: price 100, equity 10000, conf 0.72 -> tier 80% -> size 0.16
# -> usd 1600; entry fill 100*(1+slip); qty = 1600/fill; entry fee = 1600*fee
# walk: prices 105/108/110 (dump 20 -> hold) then 112 with dump 76 >= 75
# -> single-tick exit at age 4; exit fill 112*(1-slip); fee = qty*fill*fee
F1_USD = 0.20 * 0.80 * 10000.0          # 1600.0
F1_ENTRY_FILL = 100.0 * (1.0 + SLIPPAGE_RATE)
F1_QTY = F1_USD / F1_ENTRY_FILL
F1_ENTRY_FEE = F1_USD * SPOT_FEE_RATE
F1_EXIT_FILL = 112.0 * (1.0 - SLIPPAGE_RATE)
F1_EXIT_FEE = (F1_QTY * F1_EXIT_FILL) * SPOT_FEE_RATE
F1_GROSS = (F1_EXIT_FILL - F1_ENTRY_FILL) * F1_QTY
F1_NET = F1_GROSS - F1_ENTRY_FEE - F1_EXIT_FEE

# -- fixture 2: perps short that would have stopped out -----------------------
# vetoed entry: price 100, equity 10000, conf 0.90 -> tier 100% -> margin 0.10
# -> margin 1000, leverage 3, notional 3000; short entry fills DOWN: 100*(1-slip)
# qty = 3000/fill; entry fee = 3000*taker; stop = fill*1.02 (2% short stop)
# walk: 101 (hold) then 102.5 >= stop -> stop exit at age 2 (stops bypass the
# 3-cycle min hold); short exit fills UP: 102.5*(1+slip); fee = qty*fill*taker
F2_MARGIN = 0.10 * 1.00 * 10000.0       # 1000.0
F2_LEVERAGE = 3.0
F2_NOTIONAL = F2_MARGIN * F2_LEVERAGE   # 3000.0
F2_ENTRY_FILL = 100.0 * (1.0 - SLIPPAGE_RATE)
F2_QTY = F2_NOTIONAL / F2_ENTRY_FILL
F2_ENTRY_FEE = F2_NOTIONAL * PERPS_TAKER_FEE_RATE
F2_STOP = F2_ENTRY_FILL * 1.02
F2_EXIT_FILL = 102.5 * (1.0 + SLIPPAGE_RATE)
F2_EXIT_FEE = (F2_QTY * F2_EXIT_FILL) * PERPS_TAKER_FEE_RATE
F2_GROSS = (F2_ENTRY_FILL - F2_EXIT_FILL) * F2_QTY   # short: entry - exit
F2_NET = F2_GROSS - F2_ENTRY_FEE - F2_EXIT_FEE

# -- fixture 3: spot long still open at data end -----------------------------
# conf 0.85 -> tier 100% -> size 0.20 -> usd 2000 at price 100; walk 101 then
# 102 (dump 10 -> hold) and the data ends -> mark at 102 (no exit fill/fee):
# net = (102 - entry fill)*qty - entry fee
F3_USD = 0.20 * 1.00 * 10000.0          # 2000.0
F3_ENTRY_FILL = 100.0 * (1.0 + SLIPPAGE_RATE)
F3_QTY = F3_USD / F3_ENTRY_FILL
F3_ENTRY_FEE = F3_USD * SPOT_FEE_RATE
F3_MARK = 102.0
F3_GROSS = (F3_MARK - F3_ENTRY_FILL) * F3_QTY
F3_NET = F3_GROSS - F3_ENTRY_FEE

# -- fixture 4: min hold blocks an early >=75 dump ----------------------------
# conf 0.68 -> tier 60% -> size 0.12 -> usd 1200; walk: dump 80 at age 1 (>= 75
# but min hold 3 blocks it), dump 20 (streak reset), dump 66 (streak 1), then
# dump 68 at age 4 (streak 2) -> 2-consecutive exit at price 104
F4_USD = 0.20 * 0.60 * 10000.0          # 1200.0
F4_ENTRY_FILL = 100.0 * (1.0 + SLIPPAGE_RATE)
F4_QTY = F4_USD / F4_ENTRY_FILL
F4_ENTRY_FEE = F4_USD * SPOT_FEE_RATE
F4_EXIT_FILL = 104.0 * (1.0 - SLIPPAGE_RATE)
F4_EXIT_FEE = (F4_QTY * F4_EXIT_FILL) * SPOT_FEE_RATE
F4_GROSS = (F4_EXIT_FILL - F4_ENTRY_FILL) * F4_QTY
F4_NET = F4_GROSS - F4_ENTRY_FEE - F4_EXIT_FEE

class CounterfactualEngine(unittest.TestCase):
    """counterfactual_entry / walk_forward_exit: pure math, 6 dp exact."""

    def test_spot_entry_math(self):
        entry = jev_calibrate.counterfactual_entry(
            _decision(price=100.0, equity=10000.0,
                      verdict=_verdict(pump=70.0, conf=0.72)), CFG)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["side"], "long")
        self.assertEqual(entry["book"], "spot")
        self.assertEqual(entry["size_tier"], 80)
        self.assertAlmostEqual(entry["size_fraction"], 0.16, places=6)
        self.assertAlmostEqual(entry["usd"], F1_USD, places=6)
        self.assertAlmostEqual(entry["fill"], F1_ENTRY_FILL, places=6)
        self.assertAlmostEqual(entry["qty"], F1_QTY, places=6)
        self.assertAlmostEqual(entry["entry_fee"], F1_ENTRY_FEE, places=6)

    def test_perps_entry_math(self):
        entry = jev_calibrate.counterfactual_entry(
            _decision(book="perps", price=100.0, equity=10000.0,
                      verdict=_verdict(pump=10.0, dump=70.0, conf=0.90)), CFG)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["side"], "short")
        self.assertAlmostEqual(entry["margin"], F2_MARGIN, places=6)
        self.assertAlmostEqual(entry["leverage"], F2_LEVERAGE, places=6)
        self.assertAlmostEqual(entry["notional"], F2_NOTIONAL, places=6)
        self.assertAlmostEqual(entry["fill"], F2_ENTRY_FILL, places=6)
        self.assertAlmostEqual(entry["qty"], F2_QTY, places=6)
        self.assertAlmostEqual(entry["entry_fee"], F2_ENTRY_FEE, places=6)
        self.assertAlmostEqual(entry["stop_price"], F2_STOP, places=6)

    def test_spot_long_win_exact_net(self):
        entry = jev_calibrate.counterfactual_entry(
            _decision(price=100.0, equity=10000.0,
                      verdict=_verdict(pump=70.0, conf=0.72)), CFG)
        after = [
            _decision(ts_ms=1800000300000, price=105.0,
                      verdict=_verdict(pump=40.0, dump=20.0)),
            _decision(ts_ms=1800000600000, price=108.0,
                      verdict=_verdict(pump=40.0, dump=20.0)),
            _decision(ts_ms=1800000900000, price=110.0,
                      verdict=_verdict(pump=40.0, dump=20.0)),
            _decision(ts_ms=1800001200000, price=112.0,
                      verdict=_verdict(pump=40.0, dump=76.0)),
        ]
        res = jev_calibrate.walk_forward_exit(after, entry, CFG, "spot")
        self.assertEqual(res["path"], "single >=75")
        self.assertFalse(res["open"])
        self.assertEqual(res["cycles_held"], 4)
        self.assertAlmostEqual(res["gross"], F1_GROSS, places=6)
        self.assertAlmostEqual(res["net"], F1_NET, places=6)

    def test_perps_short_stopped_exact_net(self):
        entry = jev_calibrate.counterfactual_entry(
            _decision(book="perps", price=100.0, equity=10000.0,
                      verdict=_verdict(pump=10.0, dump=70.0, conf=0.90)), CFG)
        after = [
            _decision(book="perps", ts_ms=1800000300000, price=101.0,
                      verdict=_verdict(pump=10.0, dump=10.0)),
            _decision(book="perps", ts_ms=1800000600000, price=102.5,
                      verdict=_verdict(pump=10.0, dump=10.0)),
        ]
        res = jev_calibrate.walk_forward_exit(after, entry, CFG, "perps")
        self.assertEqual(res["path"], "stop")
        self.assertFalse(res["open"])
        self.assertEqual(res["cycles_held"], 2)  # stop bypasses min hold 3
        self.assertAlmostEqual(res["exit_fill"], F2_EXIT_FILL, places=6)
        self.assertAlmostEqual(res["gross"], F2_GROSS, places=6)
        self.assertAlmostEqual(res["net"], F2_NET, places=6)

    def test_still_open_marks_to_last_price(self):
        entry = jev_calibrate.counterfactual_entry(
            _decision(price=100.0, equity=10000.0,
                      verdict=_verdict(pump=70.0, conf=0.85)), CFG)
        after = [
            _decision(ts_ms=1800000300000, price=101.0,
                      verdict=_verdict(pump=40.0, dump=10.0)),
            _decision(ts_ms=1800000600000, price=F3_MARK,
                      verdict=_verdict(pump=40.0, dump=10.0)),
        ]
        res = jev_calibrate.walk_forward_exit(after, entry, CFG, "spot")
        self.assertEqual(res["path"], "open")
        self.assertTrue(res["open"])
        self.assertAlmostEqual(res["gross"], F3_GROSS, places=6)
        self.assertAlmostEqual(res["net"], F3_NET, places=6)

    def test_min_hold_blocks_early_hard_dump(self):
        entry = jev_calibrate.counterfactual_entry(
            _decision(price=100.0, equity=10000.0,
                      verdict=_verdict(pump=70.0, conf=0.68)), CFG)
        after = [
            _decision(ts_ms=1800000300000, price=101.0,   # dump 80 >= 75 but
                      verdict=_verdict(pump=40.0, dump=80.0)),  # age 1: blocked
            _decision(ts_ms=1800000600000, price=102.0,
                      verdict=_verdict(pump=40.0, dump=20.0)),
            _decision(ts_ms=1800000900000, price=103.0,
                      verdict=_verdict(pump=40.0, dump=66.0)),
            _decision(ts_ms=1800001200000, price=104.0,
                      verdict=_verdict(pump=40.0, dump=68.0)),
        ]
        res = jev_calibrate.walk_forward_exit(after, entry, CFG, "spot")
        self.assertEqual(res["path"], "2-consecutive")  # not the age-1 hard dump
        self.assertEqual(res["cycles_held"], 4)
        self.assertEqual(res["exit_ts_ms"], 1800001200000)
        self.assertAlmostEqual(res["gross"], F4_GROSS, places=6)
        self.assertAlmostEqual(res["net"], F4_NET, places=6)

    def test_no_qualifying_side_signal_returns_none(self):
        # neither pump >= 65 nor dump >= 65: the veto cost nothing (no entry
        # would have fired) -> no counterfactual
        self.assertIsNone(jev_calibrate.counterfactual_entry(
            _decision(verdict=_verdict(pump=60.0, dump=40.0)), CFG))
        self.assertIsNone(jev_calibrate.counterfactual_entry(
            _decision(book="perps",
                      verdict=_verdict(pump=60.0, dump=40.0)), CFG))
        # spot is long-only: dump alone never enters the spot book
        self.assertIsNone(jev_calibrate.counterfactual_entry(
            _decision(verdict=_verdict(pump=40.0, dump=90.0)), CFG))

    def test_missing_data_fails_open(self):
        self.assertIsNone(jev_calibrate.counterfactual_entry(
            _decision(price=None), CFG))
        self.assertIsNone(jev_calibrate.counterfactual_entry(
            _decision(equity=None), CFG))
        self.assertIsNone(jev_calibrate.counterfactual_entry(
            _decision(verdict=None), CFG))

class AggregationMath(unittest.TestCase):
    """PF / win-rate / bucket math — hand-checked."""

    def test_profit_factor(self):
        self.assertIsNone(jev_calibrate.profit_factor([]))
        self.assertAlmostEqual(jev_calibrate.profit_factor([10.0, -5.0, -5.0]),
                               1.0, places=6)
        self.assertAlmostEqual(jev_calibrate.profit_factor([10.0, -4.0]),
                               2.5, places=6)
        self.assertTrue(math.isinf(jev_calibrate.profit_factor([5.0, 3.0])))
        self.assertAlmostEqual(jev_calibrate.profit_factor([-2.0]), 0.0, places=6)

    def test_win_rate(self):
        self.assertIsNone(jev_calibrate.win_rate([]))
        self.assertAlmostEqual(jev_calibrate.win_rate([1.0, -1.0, 1.0]),
                               2.0 / 3.0, places=6)
        self.assertAlmostEqual(jev_calibrate.win_rate([-1.0]), 0.0, places=6)

    def test_confidence_bucket_labels(self):
        self.assertEqual(jev_calibrate.confidence_bucket(0.65, CFG), "[0.65,0.70)")
        self.assertEqual(jev_calibrate.confidence_bucket(0.70, CFG), "[0.70,0.85)")
        self.assertEqual(jev_calibrate.confidence_bucket(0.84, CFG), "[0.70,0.85)")
        self.assertEqual(jev_calibrate.confidence_bucket(0.85, CFG), ">=0.85")
        self.assertEqual(jev_calibrate.confidence_bucket(0.50, CFG), "<0.65")
        self.assertEqual(jev_calibrate.confidence_bucket(None, CFG), "unknown")

    def test_bucket_stats_hand_checked(self):
        trips = [
            {"confidence": 0.68, "net": 10.0}, {"confidence": 0.66, "net": -5.0},
            {"confidence": 0.72, "net": -4.0}, {"confidence": 0.80, "net": -6.0},
            {"confidence": 0.84, "net": 8.0},
            {"confidence": 0.90, "net": 20.0},
            {"confidence": 0.50, "net": 3.0},
            {"confidence": None, "net": 5.0},
        ]
        rows = {r["bucket"]: r for r in jev_calibrate.bucket_stats(trips, CFG)}
        b1 = rows["[0.65,0.70)"]
        self.assertEqual(b1["count"], 2)
        self.assertAlmostEqual(b1["win_rate"], 0.5, places=6)
        self.assertAlmostEqual(b1["avg_net"], 2.5, places=6)
        self.assertAlmostEqual(b1["pf"], 2.0, places=6)   # 10 / 5
        b2 = rows["[0.70,0.85)"]
        self.assertEqual(b2["count"], 3)
        self.assertAlmostEqual(b2["win_rate"], 1.0 / 3.0, places=6)
        self.assertAlmostEqual(b2["avg_net"], (-2.0) / 3.0, places=6)
        self.assertAlmostEqual(b2["pf"], 0.8, places=6)   # 8 / 10
        b3 = rows[">=0.85"]
        self.assertEqual(b3["count"], 1)
        self.assertAlmostEqual(b3["win_rate"], 1.0, places=6)
        self.assertAlmostEqual(b3["avg_net"], 20.0, places=6)
        self.assertTrue(math.isinf(b3["pf"]))             # no losers
        self.assertEqual(rows["<0.65"]["count"], 1)
        self.assertEqual(rows["unknown"]["count"], 1)

class FanoutStats(unittest.TestCase):
    """Fan-out flip counting on fixtures with both samples stored."""

    def test_fanout_flip_counting(self):
        rows = [
            # w1 passes alone, w2 fails -> split -> tie (fail-closed): FLIP to veto
            _decision(decision_id="f-a",
                      verdict=_verdict(whipsaw=0.42, whipsaw_prob_2=0.55),
                      fan_out=True),
            # both samples pass -> no flip (single pass, fan-out pass)
            _decision(decision_id="f-b",
                      verdict=_verdict(whipsaw=0.44, whipsaw_prob_2=0.41),
                      fan_out=True),
            # both samples fail -> no flip (single veto, fan-out veto)
            _decision(decision_id="f-c",
                      verdict=_verdict(whipsaw=0.50, whipsaw_prob_2=0.52),
                      fan_out=True),
            # fan-out engaged but 2nd sample unusable -> tie (fail-closed),
            # excluded from flip counting (both samples NOT stored)
            _decision(decision_id="f-d",
                      verdict=_verdict(whipsaw=0.48), fan_out=True),
        ]
        s = jev_calibrate.fanout_stats(rows, CFG)
        self.assertEqual(s["fanout_calls"], 4)
        self.assertEqual(s["ties"], 2)               # f-a (split) + f-d (unusable)
        self.assertAlmostEqual(s["tie_rate"], 0.5, places=6)
        self.assertEqual(s["both_stored"], 3)
        self.assertEqual(s["single_pass_fanout_veto"], 1)  # f-a
        self.assertEqual(s["single_fail_fanout_pass"], 0)  # rule cannot flip up


class HysteresisStats(unittest.TestCase):
    def test_exit_paths_and_median_hold(self):
        trips = [
            {"book": "spot", "path": "2-consecutive", "hold_ms": 1200000},
            {"book": "spot", "path": "single >=75", "hold_ms": 600000},
            {"book": "perps", "path": "stop", "hold_ms": 300000},
            {"book": "perps", "path": "liq", "hold_ms": 900000},
        ]
        s = jev_calibrate.hysteresis_stats(trips)
        self.assertEqual(s["paths"]["2-consecutive"]["spot"], 1)
        self.assertEqual(s["paths"]["single >=75"]["spot"], 1)
        self.assertEqual(s["paths"]["stop"]["perps"], 1)
        self.assertEqual(s["paths"]["liq"]["perps"], 1)
        self.assertAlmostEqual(s["median_hold_min"]["spot"], 15.0, places=6)
        self.assertAlmostEqual(s["median_hold_min"]["perps"], 10.0, places=6)
        self.assertAlmostEqual(s["median_hold_min"]["all"], 12.5, places=6)

class VetoAttribution(unittest.TestCase):
    """Counterfactual attributed to EVERY gate in the bitmask (overlaps by design)."""

    def _rows(self):
        walk = [
            _decision(ts_ms=1800000300000, decision_id="d-1", price=105.0,
                      verdict=_verdict(pump=40.0, dump=20.0)),
            _decision(ts_ms=1800000600000, decision_id="d-2", price=108.0,
                      verdict=_verdict(pump=40.0, dump=20.0)),
            _decision(ts_ms=1800000900000, decision_id="d-3", price=110.0,
                      verdict=_verdict(pump=40.0, dump=20.0)),
            _decision(ts_ms=1800001200000, decision_id="d-4", price=112.0,
                      verdict=_verdict(pump=40.0, dump=76.0)),
        ]
        no_side = _decision(ts_ms=1799999000000, decision_id="d-n",
                            price=100.0,
                            verdict=_verdict(pump=60.0, dump=40.0),
                            action="skip", veto_bitmask=bitmask_for(["low_pump"]),
                            vetoed_by=["low_pump"])
        entry = _decision(ts_ms=1800000000000, decision_id="d-0", price=100.0,
                          equity=10000.0, verdict=_verdict(pump=70.0, conf=0.72),
                          action="skip",
                          veto_bitmask=bitmask_for(["cooldown", "low_confidence"]),
                          vetoed_by=["cooldown", "low_confidence"])
        return [no_side, entry] + walk

    def test_counterfactual_attributed_to_every_gate(self):
        s = jev_calibrate.veto_attribution(self._rows(), [], CFG)
        self.assertEqual(s["evaluated"], 1)
        self.assertEqual(s["no_side_signal"], 1)
        self.assertEqual(s["position_open"], 0)
        for gate in ("cooldown", "low_confidence"):
            g = s["gates"][gate]
            self.assertEqual(g["vetoes"], 1)
            self.assertAlmostEqual(g["counterfactual_pnl"], F1_NET, places=6)
            self.assertAlmostEqual(g["avg_per_veto"], F1_NET, places=6)
        low_pump = s["gates"]["low_pump"]
        self.assertEqual(low_pump["vetoes"], 1)      # no side signal -> value 0
        self.assertAlmostEqual(low_pump["counterfactual_pnl"], 0.0, places=6)

    def test_position_open_excludes_the_decision(self):
        trades = [
            {"book": "spot", "symbol": "BTCUSDT", "ts_ms": 1799999500000,
             "side": "buy", "action": None, "realized_pnl": 0.0,
             "funding_paid": None},
            {"book": "spot", "symbol": "BTCUSDT", "ts_ms": 1800001000000,
             "side": "sell", "action": None, "realized_pnl": 1.0,
             "funding_paid": None},
        ]
        s = jev_calibrate.veto_attribution(self._rows(), trades, CFG)
        self.assertEqual(s["position_open"], 1)   # d-0 falls inside the interval
        self.assertEqual(s["evaluated"], 0)
        self.assertEqual(s["no_side_signal"], 1)  # d-n is before the entry
        self.assertEqual(s["gates"]["cooldown"]["vetoes"], 0)

def _gate_line(ts_ms, decision_id, book, price, action, bitmask, vetoed_by,
               verdict, equity=10000.0, regime="trend_up", fan_out=0,
               reason="", symbol="BTCUSDT"):
    """One paper_decisions.jsonl line (shape verified against the live writer)."""
    return {"ts_ms": ts_ms, "decision_id": decision_id, "book": book,
            "symbol": symbol, "price": price, "action": action, "executed": None,
            "veto_bitmask": int(bitmask), "vetoed_by": list(vetoed_by),
            "reason": reason, "equity": equity, "fees": 0.0, "slippage": 0.0,
            "realized_pnl": 0.0, "funding_paid": 0.0, "verdict": verdict,
            "regime": regime, "fan_out": fan_out}


# fixture-1 fixture rows for the store: one vetoed entry + the walk
F1_ROWS = [
    _gate_line(1800000000000, "d-0", "spot", 100.0, "skip",
               bitmask_for(["cooldown"]), ["cooldown"],
               _verdict(pump=70.0, dump=20.0, conf=0.72)),
    _gate_line(1800000300000, "d-1", "spot", 105.0, "skip", 0, [],
               _verdict(pump=40.0, dump=20.0, conf=0.50)),
    _gate_line(1800000600000, "d-2", "spot", 108.0, "skip", 0, [],
               _verdict(pump=40.0, dump=20.0, conf=0.50)),
    _gate_line(1800000900000, "d-3", "spot", 110.0, "skip", 0, [],
               _verdict(pump=40.0, dump=20.0, conf=0.50)),
    _gate_line(1800001200000, "d-4", "spot", 112.0, "skip", 0, [],
               _verdict(pump=40.0, dump=76.0, conf=0.50)),
]


class StoreFixture(unittest.TestCase):
    """tmp store + JSONL fixture; jev_import fills gate_decisions (M4 data path)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.db = self.runtime / "jevelin.db"

    def _write(self, name, rows):
        (self.runtime / name).write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    def _import(self):
        conn = jev_store.connect(str(self.db))
        self.addCleanup(conn.close)
        jev_import.import_all(self.runtime, conn)
        return conn

    def _run(self, *extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = jev_calibrate.main(["--db", str(self.db)] + list(extra))
        return code, buf.getvalue()


class StoreReport(StoreFixture):
    """End-to-end: JSONL fixture -> store -> markdown report."""

    def setUp(self):
        super().setUp()
        self._write("paper_decisions.jsonl", F1_ROWS)

    def test_gate_rows_imported_idempotently(self):
        conn = self._import()
        rows = jev_store.list_gate_decisions(conn)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["decision_id"], "d-0")
        self.assertEqual(json.loads(rows[0]["verdict_json"])["pump_0_100"], 70.0)
        self._import()  # re-import never duplicates
        self.assertEqual(len(jev_store.list_gate_decisions(conn)), 5)

    def test_report_sections_in_order(self):
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        headers = [l for l in out.splitlines() if l.startswith("## ")]
        self.assertEqual([h.split(". ", 1)[1] for h in headers],
                         ["Overview", "Regime distribution",
                          "Per-gate veto attribution", "Confidence calibration curve",
                          "Fan-out stats", "Hysteresis stats", "Re-tune proposals"])
        self.assertIn("Asia/Bangkok", out)

    def test_overview_cross_check_ok(self):
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        line = [l for l in out.splitlines() if "cross-check" in l][0]
        self.assertIn("OK", line)

    def test_veto_value_table_shows_counterfactual(self):
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        line = [l for l in out.splitlines() if l.startswith("| cooldown")][0]
        self.assertIn(f"{F1_NET:.2f}", line)
        self.assertIn("overlaps by design", out)

    def test_calibration_run_written_per_run(self):
        self._import()
        code, _out = self._run("--since", "2026-09-27")
        self.assertEqual(code, 0)
        conn = jev_store.connect(str(self.db))
        self.addCleanup(conn.close)
        runs = jev_store.list_calibration_runs(conn)
        self.assertEqual(len(runs), 1)
        params = json.loads(runs[0]["params"])
        self.assertEqual(params["since"], "2026-09-27")
        metrics = json.loads(runs[0]["metrics_json"])
        self.assertIn("books", metrics)
        self.assertIn("spot", metrics["books"])

    def test_out_file_written(self):
        self._import()
        out_path = self.runtime / "report.md"
        code, _ = self._run("--out", str(out_path))
        self.assertEqual(code, 0)
        text = out_path.read_text()
        self.assertIn("# Jevelin M4 calibration report", text)
        self.assertIn("## 7. Re-tune proposals", text)

    def test_insufficient_data_suppresses_proposals(self):
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("insufficient data for proposals", out)
        self.assertNotIn("PROPOSAL", out)


class FailOpen(StoreFixture):
    """missing/empty store -> clear error, exit non-zero, no fabricated numbers."""

    def test_empty_store_clean_error(self):
        conn = jev_store.connect(str(self.db))  # schema only, zero rows
        self.addCleanup(conn.close)
        code, out = self._run()
        self.assertNotEqual(code, 0)
        self.assertIn("no recorded data", out)
        self.assertEqual(jev_store.list_calibration_runs(conn), [])

    def test_missing_store_clean_error(self):
        code, out = self._run()
        self.assertNotEqual(code, 0)
        self.assertIn("not found", out)
        self.assertFalse(self.db.exists())  # never fabricate a store

class Proposals(StoreFixture):
    """>= 50 round trips + strongly negative veto value -> deterministic PROPOSAL."""

    def _build(self):
        # 50 spot round trips (entry decision ids link to the gate rows below)
        trades = []
        for i in range(50):
            e = 1790000000000 + i * 2000
            trades.append({"ts_ms": e, "decision_id": f"g-{i}", "book": "spot",
                           "symbol": "BTCUSDT", "side": "buy", "price": 100.0,
                           "qty": 10.0, "usd": 1000.0, "realized_pnl": 0.0,
                           "fees": 1.0, "slippage": 0.5, "reason": "entered"})
            trades.append({"ts_ms": e + 1000, "decision_id": f"x-{i}",
                           "book": "spot", "symbol": "BTCUSDT", "side": "sell",
                           "price": 100.5, "qty": 10.0, "usd": 1005.0,
                           "realized_pnl": 3.0, "fees": 1.005, "slippage": 0.5,
                           "reason": "exited"})
        self._write("paper_btc.json.trades.jsonl", trades)
        # 60 vetoed spot decisions: price declines 0.5 per cycle and every walk
        # exits 3 cycles later (dump 76 >= single bar at min hold) ~1.5 lower,
        # so every counterfactual is a consistent loser (~-28 USD each)
        rows = []
        for i in range(60):
            rows.append(_gate_line(1800000000000 + i * 300000, f"g-{i}", "spot",
                                   100.0 - 0.5 * i, "skip",
                                   bitmask_for(["cooldown"]), ["cooldown"],
                                   _verdict(pump=70.0, dump=76.0, conf=0.70),
                                   reason="cooldown: 100ms since last entry"))
        self._write("paper_decisions.jsonl", rows)

    def test_proposal_tighten_after_50_trips(self):
        self._build()
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("PROPOSAL", out)
        self.assertIn("tighten", out)
        self.assertIn("HUMAN", out)
        line = [l for l in out.splitlines() if "cooldown" in l and "PROPOSAL" in l][0]
        self.assertIn("cooldown", line)

    def test_no_proposal_when_within_threshold(self):
        self._build()
        # flat prices: counterfactuals lose only fees/slippage (~-5 USD each,
        # well inside the +/-2%-of-equity-per-10-vetoes threshold)
        path = self.runtime / "paper_decisions.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines() if l]
        for row in rows:
            row["price"] = 100.0
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn("PROPOSAL", out)
        self.assertIn("no gate crosses", out)

    def test_proposal_relax_on_strongly_positive(self):
        self._build()
        # prices rise 1.0/cycle: counterfactuals win big consistently (the
        # exit is 3 cycles later -> ~+3 USD notional move per unit)
        path = self.runtime / "paper_decisions.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines() if l]
        for i, row in enumerate(rows):
            row["price"] = 100.0 + 1.0 * i
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("PROPOSAL", out)
        self.assertIn("relax", out)
        self.assertIn("HUMAN", out)


class MultiPairReport(StoreFixture):
    """M5: multi-pair data calibrates; the veto table shows the M5 risk flags."""

    def test_multi_pair_report_and_new_flags(self):
        rows_btc = [dict(r, symbol="BTCUSDT") for r in F1_ROWS]
        rows_eth = [dict(r, symbol="ETHUSDT",
                         decision_id="e" + str(r["decision_id"])[1:])
                    for r in F1_ROWS]
        # one ETH decision vetoed by the four M5 portfolio-risk gates
        rows_eth.insert(0, _gate_line(
            1800000000000, "e-m5", "spot", 100.0, "skip",
            bitmask_for(["global_daily_kill", "drawdown_halt",
                         "pair_cap", "basket_cap"]),
            ["global_daily_kill", "drawdown_halt", "pair_cap", "basket_cap"],
            _verdict(pump=70.0, dump=20.0, conf=0.72), symbol="ETHUSDT"))
        self._write("paper_decisions.jsonl", rows_btc + rows_eth)
        self._import()
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("per book/symbol", out)
        line = [l for l in out.splitlines() if "cross-check" in l][0]
        self.assertIn("OK", line)
        # the 4 new flags appear in the veto table with their counts
        for flag in ("pair_cap", "basket_cap", "global_daily_kill", "drawdown_halt"):
            row = [l for l in out.splitlines() if l.startswith(f"| {flag} |")][0]
            self.assertIn("| 1 |", row)
        # counterfactual attribution is per (book, symbol): one cooldown veto
        # per symbol -> 2 vetoes total
        cooldown = [l for l in out.splitlines() if l.startswith("| cooldown |")][0]
        self.assertIn("| 2 |", cooldown)


if __name__ == "__main__":
    unittest.main()









