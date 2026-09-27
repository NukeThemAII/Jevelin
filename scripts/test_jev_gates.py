#!/usr/bin/env python3
"""Tests for jev_gates.decide (M3 recalibration) — pure function, no network.

Design numbers AS WRITTEN (docs/V2-DESIGN.md B.3): entry pump>=65, phase in
{breakout, accumulation}, whipsaw<=0.45, exhaustion<=0.55, conf>=0.65, regime
!= chop; exit = dump>=65 x 2 consecutive OR single >=75, min hold 3 cycles;
sizing tiers 0.65-0.70 -> 60% of cap, 0.70-0.85 -> 80%, >=0.85 -> 100%.
Run: .venv/bin/python scripts/test_jev_gates.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import VetoFlags, bit_names, bitmask_for
from jev_gates import PortfolioState, RiskConfig, decide

NOW = 1_800_000_000_000
CFG = RiskConfig()
RESULT_KEYS = {"action", "reason", "size_fraction", "size_tier", "vetoed_by",
               "veto_bitmask", "decision_id", "exit_signal_cycles", "cycles_held"}
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
    "whipsaw_fanout_tie": VetoFlags.WHIPSAW_FANOUT_TIE,
    "phase_not_in_entry_set": VetoFlags.PHASE_NOT_IN_ENTRY_SET,
    "regime_chop": VetoFlags.REGIME_CHOP,
    "regime_counter": VetoFlags.REGIME_COUNTER,
}


def _verdict(**overrides):
    v = {
        "ok": True,
        "error": None,
        "symbol": "BTC/USDT",
        "pump_0_100": 70.0,
        "dump_0_100": 10.0,
        "phase": "breakout",
        "exhaustion_prob": 0.21,
        "whipsaw_prob": 0.3,
        "confidence": 0.86,
        "answers": {},
        "latency_ms": 123.0,
    }
    v.update(overrides)
    return v


def _flat(**overrides):
    kw = dict(has_position=False, equity_usd=10_000.0, daily_pnl_pct=0.0,
              last_entry_ts_ms=None, exit_signal_cycles=0, cycles_held=0)
    kw.update(overrides)
    return PortfolioState(**kw)


def _long(**overrides):
    return _flat(has_position=True, **overrides)


class EnterThresholds(unittest.TestCase):
    """B.3 entry gates at their exact new defaults."""

    def test_enter_happy_path(self):
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up")
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["vetoed_by"], [])  # no gate vetoed an entry
        self.assertEqual(d["veto_bitmask"], 0)
        self.assertIsNone(d["decision_id"])
        self.assertEqual(d["size_fraction"], 0.20)  # conf 0.86 -> 100% of cap
        self.assertEqual(d["size_tier"], 100)
        self.assertEqual(d["exit_signal_cycles"], 0)
        self.assertEqual(d["cycles_held"], 0)
        self.assertEqual(set(d), RESULT_KEYS)

    def test_enter_at_exact_thresholds(self):
        v = _verdict(pump_0_100=65.0, whipsaw_prob=0.45, exhaustion_prob=0.55,
                     confidence=0.65)
        d = decide(v, _flat(last_entry_ts_ms=NOW - 900_000), CFG, NOW)
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["size_fraction"], round(0.20 * 0.60, 4))  # 60% tier

    def test_just_below_new_bars_veto(self):
        v = _verdict(pump_0_100=64.9, whipsaw_prob=0.451, exhaustion_prob=0.551,
                     confidence=0.649)
        d = decide(v, _flat(), CFG, NOW)
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"],
                         ["low_pump", "high_whipsaw", "high_exhaustion",
                          "low_confidence"])

    def test_legacy_no_regime_still_enters(self):
        # paper_loop (deprecated v1 fallback) passes no regime -> no regime gate
        d = decide(_verdict(), _flat(), CFG, NOW)
        self.assertEqual(d["action"], "enter")


class SizingTierTests(unittest.TestCase):
    """Confidence banding: 60% / 80% / 100% of the same cap (B.3)."""

    def tier_of(self, conf):
        d = decide(_verdict(confidence=conf), _flat(), CFG, NOW)
        return d["size_fraction"], d["size_tier"]

    def test_tier_boundaries_exact(self):
        self.assertEqual(self.tier_of(0.65), (round(0.20 * 0.60, 4), 60))
        self.assertEqual(self.tier_of(0.699), (round(0.20 * 0.60, 4), 60))
        self.assertEqual(self.tier_of(0.70), (round(0.20 * 0.80, 4), 80))
        self.assertEqual(self.tier_of(0.8499), (round(0.20 * 0.80, 4), 80))
        self.assertEqual(self.tier_of(0.85), (round(0.20 * 1.00, 4), 100))
        self.assertEqual(self.tier_of(0.99), (round(0.20 * 1.00, 4), 100))


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
        self.assertVeto(decide(_verdict(pump_0_100=64.9), _flat(), CFG, NOW), "low_pump")

    def test_high_whipsaw(self):
        self.assertVeto(decide(_verdict(whipsaw_prob=0.451), _flat(), CFG, NOW), "high_whipsaw")

    def test_high_exhaustion(self):
        self.assertVeto(decide(_verdict(exhaustion_prob=0.551), _flat(), CFG, NOW), "high_exhaustion")

    def test_low_confidence(self):
        self.assertVeto(decide(_verdict(confidence=0.649), _flat(), CFG, NOW), "low_confidence")

    def test_cooldown(self):
        pf = _flat(last_entry_ts_ms=NOW - 899_999)
        self.assertVeto(decide(_verdict(), pf, CFG, NOW), "cooldown")

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


class PhaseTests(unittest.TestCase):
    """B.3: phase must be in {breakout, accumulation}; capitulation keeps its flag."""

    def test_phase_not_in_entry_set(self):
        for phase in ("distribution", "ranging", "sideways"):
            d = decide(_verdict(phase=phase), _flat(), CFG, NOW)
            self.assertEqual((d["action"], d["vetoed_by"]),
                             ("skip", ["phase_not_in_entry_set"]), phase)
            self.assertEqual(d["veto_bitmask"], int(VetoFlags.PHASE_NOT_IN_ENTRY_SET))

    def test_entry_phases_allowed(self):
        for phase in ("breakout", "accumulation"):
            self.assertEqual(decide(_verdict(phase=phase), _flat(), CFG, NOW)["action"], "enter")

    def test_capitulation_sets_both_flags(self):
        d = decide(_verdict(phase="capitulation"), _flat(), CFG, NOW)
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"], ["capitulation", "phase_not_in_entry_set"])
        self.assertEqual(d["veto_bitmask"],
                         int(VetoFlags.CAPITULATION_BLOCK
                             | VetoFlags.PHASE_NOT_IN_ENTRY_SET))


class RegimeTests(unittest.TestCase):
    """B.3 regime vetoes: chop blocks all entries; counter-trend side blocked."""

    def test_regime_chop_blocks(self):
        d = decide(_verdict(), _flat(), CFG, NOW, regime="chop")
        self.assertEqual((d["action"], d["vetoed_by"]), ("skip", ["regime_chop"]))
        self.assertEqual(d["veto_bitmask"], int(VetoFlags.REGIME_CHOP))

    def test_regime_counter_blocks_long_in_trend_down(self):
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_down")
        self.assertEqual((d["action"], d["vetoed_by"]), ("skip", ["regime_counter"]))
        self.assertEqual(d["veto_bitmask"], int(VetoFlags.REGIME_COUNTER))

    def test_trend_up_allows_long(self):
        self.assertEqual(decide(_verdict(), _flat(), CFG, NOW,
                                regime="trend_up")["action"], "enter")

    def test_counter_trend_allow_config(self):
        cfg = RiskConfig(counter_trend="allow")
        self.assertEqual(decide(_verdict(), _flat(), cfg, NOW,
                                regime="trend_down")["action"], "enter")

    def test_exits_never_regime_blocked(self):
        for regime in ("chop", "trend_up", "trend_down"):
            d = decide(_verdict(dump_0_100=80.0), _long(cycles_held=3), CFG, NOW,
                       regime=regime)
            self.assertEqual(d["action"], "exit", regime)


class HysteresisExitTests(unittest.TestCase):
    """B.3 exit rule: dump>=65 x 2 consecutive OR single >=75; min hold 3 cycles."""

    def test_one_dump_signal_holds(self):
        d = decide(_verdict(dump_0_100=70.0), _long(cycles_held=3), CFG, NOW)
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["reason"], "hold")
        self.assertEqual(d["exit_signal_cycles"], 1)  # counted, not yet fired
        self.assertEqual(d["cycles_held"], 4)
        self.assertEqual(d["vetoed_by"], [])  # hold is not a veto

    def test_two_consecutive_dump_signals_exit(self):
        d = decide(_verdict(dump_0_100=70.0), _long(cycles_held=3, exit_signal_cycles=1),
                   CFG, NOW)
        self.assertEqual(d["action"], "exit")
        self.assertIn("consecutive", d["reason"])
        self.assertEqual(d["size_fraction"], 0.0)
        self.assertEqual(d["vetoed_by"], [])
        self.assertEqual(d["veto_bitmask"], 0)  # an exit is never vetoed

    def test_single_hard_bar_exits(self):
        d = decide(_verdict(dump_0_100=75.0), _long(cycles_held=3), CFG, NOW)
        self.assertEqual(d["action"], "exit")
        d = decide(_verdict(dump_0_100=100.0), _long(cycles_held=5), CFG, NOW)
        self.assertEqual(d["action"], "exit")

    def test_min_hold_blocks_early_signal_exits(self):
        # position age in cycles < 3: even the >=75 bar must wait (stops bypass)
        for cycles_held, exit_signal in ((0, 0), (0, 1), (1, 0), (1, 2)):
            d = decide(_verdict(dump_0_100=80.0),
                       _long(cycles_held=cycles_held, exit_signal_cycles=exit_signal),
                       CFG, NOW)
            self.assertEqual(d["action"], "skip", (cycles_held, exit_signal))
            self.assertEqual(d["reason"], "hold")

    def test_min_hold_boundary_allows_exit(self):
        # age = cycles_held + 1 = 3 -> the 3rd cycle after entry may exit
        d = decide(_verdict(dump_0_100=80.0), _long(cycles_held=2), CFG, NOW)
        self.assertEqual(d["action"], "exit")

    def test_signal_counter_resets_without_signal(self):
        d = decide(_verdict(dump_0_100=30.0), _long(cycles_held=3, exit_signal_cycles=2),
                   CFG, NOW)
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["exit_signal_cycles"], 0)  # streak broken
        self.assertEqual(d["cycles_held"], 4)

    def test_hold_otherwise(self):
        d = decide(_verdict(dump_0_100=64.9, exhaustion_prob=0.79), _long(cycles_held=9),
                   CFG, NOW)
        self.assertEqual((d["action"], d["reason"]), ("skip", "hold"))
        self.assertEqual(d["size_fraction"], 0.0)

    def test_no_entry_when_holding(self):
        d = decide(_verdict(), _long(), CFG, NOW)
        self.assertNotEqual(d["action"], "enter")

    def test_exit_allowed_under_daily_loss_kill(self):
        d = decide(_verdict(dump_0_100=83.3), _long(daily_pnl_pct=-12.0, cycles_held=3),
                   CFG, NOW)
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["vetoed_by"], [])

    def test_exit_allowed_under_capitulation(self):
        d = decide(_verdict(phase="capitulation", dump_0_100=100.0), _long(cycles_held=3),
                   CFG, NOW)
        self.assertEqual(d["action"], "exit")


class FanoutTests(unittest.TestCase):
    """B.3 whipsaw self-consistency fan-out: majority vote, split = fail-closed."""

    def test_fanout_pass_pass_enters(self):
        v = _verdict(fan_out=1, whipsaw_prob=0.44, whipsaw_prob_2=0.43)
        d = decide(v, _flat(), CFG, NOW)
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["vetoed_by"], [])

    def test_fanout_both_at_bar_pass(self):
        v = _verdict(fan_out=1, whipsaw_prob=0.45, whipsaw_prob_2=0.45)
        self.assertEqual(decide(v, _flat(), CFG, NOW)["action"], "enter")

    def test_fanout_fail_fail_vetoes_high_whipsaw(self):
        v = _verdict(fan_out=1, whipsaw_prob=0.5, whipsaw_prob_2=0.6)
        d = decide(v, _flat(), CFG, NOW)
        self.assertEqual((d["action"], d["vetoed_by"]), ("skip", ["high_whipsaw"]))

    def test_fanout_split_tie_vetoes(self):
        v = _verdict(fan_out=1, whipsaw_prob=0.44, whipsaw_prob_2=0.5)
        d = decide(v, _flat(), CFG, NOW)
        self.assertEqual((d["action"], d["vetoed_by"]),
                         ("skip", ["whipsaw_fanout_tie"]))
        self.assertEqual(d["veto_bitmask"], int(VetoFlags.WHIPSAW_FANOUT_TIE))
        v = _verdict(fan_out=1, whipsaw_prob=0.7, whipsaw_prob_2=0.2)  # reversed split
        self.assertEqual(decide(v, _flat(), CFG, NOW)["vetoed_by"], ["whipsaw_fanout_tie"])

    def test_fanout_missing_second_sample_fail_closed(self):
        v = _verdict(fan_out=1, whipsaw_prob=0.44)  # 2nd call failed -> unverifiable
        d = decide(v, _flat(), CFG, NOW)
        self.assertEqual((d["action"], d["vetoed_by"]),
                         ("skip", ["whipsaw_fanout_tie"]))

    def test_single_sample_outside_band(self):
        self.assertEqual(decide(_verdict(whipsaw_prob=0.45), _flat(), CFG, NOW)["action"],
                         "enter")
        d = decide(_verdict(whipsaw_prob=0.46), _flat(), CFG, NOW)
        self.assertEqual(d["vetoed_by"], ["high_whipsaw"])

    def test_second_sample_data_without_flag_counts(self):
        v = _verdict(whipsaw_prob=0.44, whipsaw_prob_2=0.5)
        self.assertEqual(decide(v, _flat(), CFG, NOW)["vetoed_by"], ["whipsaw_fanout_tie"])


class BitmaskTests(unittest.TestCase):
    def test_multi_gate_failure_sets_every_bit(self):
        # 9 entry gates fail at once: every failing gate must be recorded, not just the first.
        v = _verdict(phase="capitulation", pump_0_100=10.0, whipsaw_prob=0.9,
                     exhaustion_prob=0.9, confidence=0.1)
        pf = _flat(daily_pnl_pct=-6.0, last_entry_ts_ms=NOW - 1000)
        d = decide(v, pf, CFG, NOW, regime="chop")
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"],
                         ["daily_loss_kill", "regime_chop", "capitulation",
                          "phase_not_in_entry_set", "low_pump", "high_whipsaw",
                          "high_exhaustion", "low_confidence", "cooldown"])
        expected = int(VetoFlags.DAILY_LOSS_KILL | VetoFlags.REGIME_CHOP
                       | VetoFlags.CAPITULATION_BLOCK | VetoFlags.PHASE_NOT_IN_ENTRY_SET
                       | VetoFlags.LOW_PUMP | VetoFlags.WHIPSAW | VetoFlags.EXHAUSTION
                       | VetoFlags.LOW_CONFIDENCE | VetoFlags.COOLDOWN)
        self.assertEqual(d["veto_bitmask"], expected)  # 9 bits set
        self.assertEqual(bin(d["veto_bitmask"]).count("1"), 9)
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


# -- M5 portfolio risk gates (jev_risk.entry_budget contract) ----------------

def _risk(**overrides):
    kw = dict(global_daily_kill=False, drawdown_halt=False,
              pair_remaining=10_000.0, basket_remaining_long=10_000.0,
              basket_remaining_short=10_000.0, min_position_pct=0.01)
    kw.update(overrides)
    return kw


class PortfolioRiskGates(unittest.TestCase):
    """M5: kill/halt/pair/basket caps veto entries; exits are never blocked."""

    def test_kill_and_halt_flags(self):
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up",
                   risk=_risk(global_daily_kill=True, drawdown_halt=True))
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"][0], "global_daily_kill")
        self.assertIn("drawdown_halt", d["vetoed_by"])
        self.assertEqual(d["veto_bitmask"],
                         int(VetoFlags.GLOBAL_DAILY_KILL | VetoFlags.DRAWDOWN_HALT))

    def test_exhausted_caps_veto_with_bits(self):
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up",
                   risk=_risk(pair_remaining=0.0, basket_remaining_long=0.0))
        self.assertEqual(d["vetoed_by"], ["pair_cap", "basket_cap"])
        self.assertEqual(d["veto_bitmask"],
                         int(VetoFlags.PAIR_CAP | VetoFlags.BASKET_CAP))

    def test_check_order_kill_first(self):
        d = decide(_verdict(pump_0_100=10.0), _flat(), CFG, NOW,
                   regime="chop", risk=_risk(global_daily_kill=True,
                                             pair_remaining=0.0))
        self.assertEqual(d["vetoed_by"][0], "global_daily_kill")

    def test_fail_closed_on_unavailable_risk(self):
        # missing capacities count as 0 (fail-closed): entries blocked
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up", risk={})
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"], ["pair_cap", "basket_cap"])

    def test_size_clamped_by_pair_cap(self):
        # tier target 0.20 (conf 0.86 -> 100% tier); pair remaining 500 of
        # 10000 equity -> 0.05; tier label unchanged, size clamped.
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up",
                   risk=_risk(pair_remaining=500.0))
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["size_fraction"], 0.05)
        self.assertEqual(d["size_tier"], 100)

    def test_size_clamped_by_basket_cap(self):
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up",
                   risk=_risk(basket_remaining_long=300.0))
        self.assertEqual(d["size_fraction"], 0.03)

    def test_dust_veto_binding_flag(self):
        # 50 deployable < 1% x 10000 = 100 floor -> the binding constraint name
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up",
                   risk=_risk(pair_remaining=50.0, basket_remaining_long=5000.0))
        self.assertEqual(d["action"], "skip")
        self.assertEqual(d["vetoed_by"], ["pair_cap"])
        d = decide(_verdict(), _flat(), CFG, NOW, regime="trend_up",
                   risk=_risk(pair_remaining=5000.0, basket_remaining_long=50.0))
        self.assertEqual(d["vetoed_by"], ["basket_cap"])

    def test_exits_never_risk_blocked(self):
        d = decide(_verdict(dump_0_100=80.0), _long(cycles_held=3), CFG, NOW,
                   regime="trend_up",
                   risk=_risk(global_daily_kill=True, drawdown_halt=True))
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["vetoed_by"], [])
        self.assertEqual(d["veto_bitmask"], 0)


if __name__ == "__main__":
    unittest.main()
