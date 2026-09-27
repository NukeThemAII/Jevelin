#!/usr/bin/env python3
"""Tests for jev_gates.decide — pure function, no network."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import VetoFlags, bit_names, bitmask_for
from jev_gates import PortfolioState, RiskConfig, decide

NOW = 1_800_000_000_000
CFG = RiskConfig()
RESULT_KEYS = {"action", "reason", "size_fraction", "vetoed_by", "veto_bitmask", "decision_id"}
# Independent expectation map: gate name -> its single flag bit.
EXPECTED_FLAG = {
    "no_verdict": VetoFlags.NO_VERDICT,
    "malformed": VetoFlags.MALFORMED,
    "daily_loss_kill": VetoFlags.DAILY_LOSS_KILL,
    "capitulation": VetoFlags.CAPITULATION_BLOCK,
    "high_whipsaw": VetoFlags.WHIPSAW,
    "high_exhaustion": VetoFlags.EXHAUSTION,
    "low_confidence": VetoFlags.LOW_CONFIDENCE,
    "cooldown": VetoFlags.COOLDOWN,
    "low_pump": VetoFlags.LOW_PUMP,
    "low_dump": VetoFlags.LOW_DUMP,
    "funding": VetoFlags.FUNDING_VETO,
}


def _verdict(**overrides):
    v = {
        "ok": True,
        "error": None,
        "symbol": "BTC/USDT",
        "pump_0_100": 66.7,
        "dump_0_100": 16.7,
        "phase": "breakout",
        "exhaustion_prob": 0.21,
        "whipsaw_prob": 0.4,
        "confidence": 0.86,
        "answers": {},
        "latency_ms": 123.0,
    }
    v.update(overrides)
    return v


def _flat(**overrides):
    kw = dict(has_position=False, equity_usd=10_000.0, daily_pnl_pct=0.0, last_entry_ts_ms=None)
    kw.update(overrides)
    return PortfolioState(**kw)


def _long(**overrides):
    return _flat(has_position=True, **overrides)


class EnterTests(unittest.TestCase):
    def test_enter_happy_path(self):
        d = decide(_verdict(), _flat(), CFG, NOW)
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["vetoed_by"], [])  # no gate vetoed an entry
        self.assertEqual(d["veto_bitmask"], 0)
        self.assertIsNone(d["decision_id"])
        self.assertEqual(d["size_fraction"], round(0.20 * 0.86, 4))
        self.assertEqual(set(d), RESULT_KEYS)

    def test_enter_at_exact_thresholds(self):
        v = _verdict(pump_0_100=60.0, whipsaw_prob=0.5, exhaustion_prob=0.6, confidence=0.6)
        d = decide(v, _flat(last_entry_ts_ms=NOW - 900_000), CFG, NOW)
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["size_fraction"], round(0.20 * 0.6, 4))


class VetoTests(unittest.TestCase):
    def assertVeto(self, d, gate):
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"], [gate])
        self.assertEqual(d["veto_bitmask"], int(EXPECTED_FLAG[gate]))
        self.assertEqual(d["size_fraction"], 0.0)
        self.assertIn(gate if gate != "no_verdict" else "no verdict", d["reason"])

    def test_no_verdict_not_ok(self):
        v = {"ok": False, "error": "timeout", "symbol": "BTC/USDT"}
        self.assertVeto(decide(v, _flat(), CFG, NOW), "no_verdict")

    def test_no_verdict_confidence_none(self):
        self.assertVeto(decide(_verdict(confidence=None), _flat(), CFG, NOW), "no_verdict")

    def test_no_verdict_blocks_exit_too(self):
        v = {"ok": False, "error": "timeout", "symbol": "BTC/USDT"}
        self.assertVeto(decide(v, _long(), CFG, NOW), "no_verdict")

    def test_daily_loss_kill(self):
        self.assertVeto(decide(_verdict(), _flat(daily_pnl_pct=-5.0), CFG, NOW), "daily_loss_kill")
        self.assertVeto(decide(_verdict(), _flat(daily_pnl_pct=-9.3), CFG, NOW), "daily_loss_kill")
        self.assertEqual(decide(_verdict(), _flat(daily_pnl_pct=-4.99), CFG, NOW)["action"], "enter")

    def test_low_pump(self):
        self.assertVeto(decide(_verdict(pump_0_100=59.9), _flat(), CFG, NOW), "low_pump")

    def test_high_whipsaw(self):
        self.assertVeto(decide(_verdict(whipsaw_prob=0.51), _flat(), CFG, NOW), "high_whipsaw")

    def test_high_exhaustion(self):
        self.assertVeto(decide(_verdict(exhaustion_prob=0.61), _flat(), CFG, NOW), "high_exhaustion")

    def test_low_confidence(self):
        self.assertVeto(decide(_verdict(confidence=0.59), _flat(), CFG, NOW), "low_confidence")

    def test_cooldown(self):
        pf = _flat(last_entry_ts_ms=NOW - 899_999)
        self.assertVeto(decide(_verdict(), pf, CFG, NOW), "cooldown")

    def test_capitulation(self):
        self.assertVeto(decide(_verdict(phase="capitulation"), _flat(), CFG, NOW), "capitulation")

    def test_malformed_missing_keys(self):
        v = _verdict()
        del v["pump_0_100"]
        self.assertVeto(decide(v, _flat(), CFG, NOW), "malformed")

    def test_malformed_bad_types(self):
        self.assertVeto(decide(_verdict(whipsaw_prob="high"), _flat(), CFG, NOW), "malformed")
        self.assertVeto(decide(_verdict(pump_0_100=float("nan")), _flat(), CFG, NOW), "malformed")
        self.assertVeto(decide(_verdict(phase=None), _flat(), CFG, NOW), "malformed")

    def test_malformed_non_dict_never_raises(self):
        for bad in (None, [], "x", 42):
            self.assertVeto(decide(bad, _flat(), CFG, NOW), "malformed")


class ExitTests(unittest.TestCase):
    def test_exit_on_dump(self):
        d = decide(_verdict(dump_0_100=60.0), _long(), CFG, NOW)
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["size_fraction"], 0.0)
        self.assertEqual(d["vetoed_by"], [])
        self.assertEqual(d["veto_bitmask"], 0)  # an exit is never vetoed

    def test_exit_on_exhaustion(self):
        d = decide(_verdict(exhaustion_prob=0.8), _long(), CFG, NOW)
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["size_fraction"], 0.0)

    def test_hold_otherwise(self):
        d = decide(_verdict(dump_0_100=59.9, exhaustion_prob=0.79), _long(), CFG, NOW)
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["reason"], "hold")
        self.assertEqual(d["size_fraction"], 0.0)
        self.assertEqual(d["vetoed_by"], [])  # hold is not a veto

    def test_no_entry_when_holding(self):
        d = decide(_verdict(), _long(), CFG, NOW)
        self.assertNotEqual(d["action"], "enter")

    def test_exit_allowed_under_daily_loss_kill(self):
        d = decide(_verdict(dump_0_100=83.3), _long(daily_pnl_pct=-12.0), CFG, NOW)
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["vetoed_by"], [])

    def test_exit_allowed_under_capitulation(self):
        d = decide(_verdict(phase="capitulation", dump_0_100=100.0), _long(), CFG, NOW)
        self.assertEqual(d["action"], "exit")


class BitmaskTests(unittest.TestCase):
    def test_multi_gate_failure_sets_every_bit(self):
        # 7 entry gates fail at once: every failing gate must be recorded, not just the first.
        v = _verdict(phase="capitulation", pump_0_100=10.0, whipsaw_prob=0.9,
                     exhaustion_prob=0.9, confidence=0.1)
        pf = _flat(daily_pnl_pct=-6.0, last_entry_ts_ms=NOW - 1000)
        d = decide(v, pf, CFG, NOW)
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"],
                         ["daily_loss_kill", "capitulation", "low_pump", "high_whipsaw",
                          "high_exhaustion", "low_confidence", "cooldown"])
        expected = int(VetoFlags.DAILY_LOSS_KILL | VetoFlags.CAPITULATION_BLOCK
                       | VetoFlags.LOW_PUMP | VetoFlags.WHIPSAW | VetoFlags.EXHAUSTION
                       | VetoFlags.LOW_CONFIDENCE | VetoFlags.COOLDOWN)
        self.assertEqual(d["veto_bitmask"], expected)  # 7 bits set
        self.assertEqual(bin(d["veto_bitmask"]).count("1"), 7)
        self.assertIn("daily_loss_kill", d["reason"])  # reason stays readable/first-gate

    def test_two_gate_failure(self):
        d = decide(_verdict(pump_0_100=10.0, confidence=0.2), _flat(), CFG, NOW)
        self.assertEqual(d["vetoed_by"], ["low_pump", "low_confidence"])
        self.assertEqual(d["veto_bitmask"],
                         int(VetoFlags.LOW_PUMP | VetoFlags.LOW_CONFIDENCE))
        self.assertEqual(bin(d["veto_bitmask"]).count("1"), 2)

    def test_bit_names_round_trip(self):
        self.assertEqual(bitmask_for([]), 0)
        self.assertEqual(int(bitmask_for(["low_pump"])), int(VetoFlags.LOW_PUMP))
        mask = bitmask_for(["low_pump", "cooldown"])
        self.assertEqual(set(bit_names(mask)), {"low_pump", "cooldown"})

    def test_decision_id_flows_into_result(self):
        d = decide(_verdict(), _flat(), CFG, NOW, decision_id="abc123def456")
        self.assertEqual(d["decision_id"], "abc123def456")
        v = _verdict()
        v["decision_id"] = "fromverdict01"
        self.assertEqual(decide(v, _flat(), CFG, NOW)["decision_id"], "fromverdict01")


if __name__ == "__main__":
    unittest.main()
