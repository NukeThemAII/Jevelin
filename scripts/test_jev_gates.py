#!/usr/bin/env python3
"""Tests for jev_gates.decide — pure function, no network."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_gates import PortfolioState, RiskConfig, decide

NOW = 1_800_000_000_000
CFG = RiskConfig()


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
        self.assertIsNone(d["vetoed_by"])
        self.assertEqual(d["size_fraction"], round(0.20 * 0.86, 4))
        self.assertEqual(set(d), {"action", "reason", "size_fraction", "vetoed_by"})

    def test_enter_at_exact_thresholds(self):
        v = _verdict(pump_0_100=60.0, whipsaw_prob=0.5, exhaustion_prob=0.6, confidence=0.6)
        d = decide(v, _flat(last_entry_ts_ms=NOW - 900_000), CFG, NOW)
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["size_fraction"], round(0.20 * 0.6, 4))


class VetoTests(unittest.TestCase):
    def assertVeto(self, d, gate):
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"], gate)
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
        self.assertIsNone(d["vetoed_by"])

    def test_exit_on_exhaustion(self):
        d = decide(_verdict(exhaustion_prob=0.8), _long(), CFG, NOW)
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["size_fraction"], 0.0)

    def test_hold_otherwise(self):
        d = decide(_verdict(dump_0_100=59.9, exhaustion_prob=0.79), _long(), CFG, NOW)
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["reason"], "hold")
        self.assertEqual(d["size_fraction"], 0.0)
        self.assertIsNone(d["vetoed_by"])

    def test_no_entry_when_holding(self):
        d = decide(_verdict(), _long(), CFG, NOW)
        self.assertNotEqual(d["action"], "enter")

    def test_exit_allowed_under_daily_loss_kill(self):
        d = decide(_verdict(dump_0_100=83.3), _long(daily_pnl_pct=-12.0), CFG, NOW)
        self.assertEqual(d["action"], "exit")
        self.assertIsNone(d["vetoed_by"])

    def test_exit_allowed_under_capitulation(self):
        d = decide(_verdict(phase="capitulation", dump_0_100=100.0), _long(), CFG, NOW)
        self.assertEqual(d["action"], "exit")


if __name__ == "__main__":
    unittest.main()
