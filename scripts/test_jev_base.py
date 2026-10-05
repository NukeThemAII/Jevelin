#!/usr/bin/env python3
"""Tests for jev_base: price-only base strategy + Jev veto-only rule. No network.

Run: .venv/bin/python scripts/test_jev_base.py -v
"""
import math
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_base import (  # noqa: E402
    BaseConfig,
    Position,
    VetoConfig,
    after_close,
    donchian_prior,
    ema_series,
    entry_signal,
    indicators,
    jev_veto,
    momentum_z,
    open_position,
    stop_exit,
    trend_ok,
    wilder_atr,
)

H = 3_600_000
T0 = 1_780_000_000_000


def _bars(closes, spread=1.0, t0=T0, step=H):
    """[ts, open, high, low, close]: open = previous close, high/low = +-spread."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([t0 + i * step, o, max(o, c) + spread, min(o, c) - spread, c])
        prev = c
    return out


def _verdict(**kw):
    v = {"ok": True, "pump_0_100": 30.0, "dump_0_100": 20.0, "phase": "breakout",
         "exhaustion_prob": 0.3, "whipsaw_prob": 0.4, "confidence": 0.6}
    v.update(kw)
    return v


class Indicators(unittest.TestCase):
    def test_atr_constant_range(self):
        h, l, c = [11.0] * 20, [9.0] * 20, [10.0] * 20
        atr = wilder_atr(h, l, c, 14)
        self.assertEqual(atr[:13], [None] * 13)
        self.assertAlmostEqual(atr[13], 2.0)
        self.assertAlmostEqual(atr[19], 2.0)

    def test_atr_wilder_recursion_and_gap(self):
        h = [11.0, 11.0, 11.0, 20.0]
        l = [9.0, 9.0, 9.0, 19.0]
        c = [10.0, 10.0, 10.0, 19.5]
        atr = wilder_atr(h, l, c, 3)
        self.assertAlmostEqual(atr[2], 2.0)
        # TR[3] = max(1, |20-10|, |19-10|) = 10 -> (2*2 + 10)/3
        self.assertAlmostEqual(atr[3], 14.0 / 3.0)

    def test_ema_sma_seed_then_recursion(self):
        e = ema_series([1.0, 2.0, 3.0, 4.0], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)
        self.assertAlmostEqual(e[3], 4.0 * 0.5 + 2.0 * 0.5)

    def test_donchian_excludes_current_bar(self):
        up, lo = donchian_prior([1.0, 5.0, 3.0, 9.0], [0.5, 4.0, 2.0, 1.0], 2)
        self.assertEqual(up[:2], [None, None])
        self.assertEqual((up[2], lo[2]), (5.0, 0.5))
        self.assertEqual((up[3], lo[3]), (5.0, 2.0))

    def test_momentum_z_is_drift_t_stat(self):
        closes = [100.0 * math.exp(0.01 * i + (0.005 if i % 2 else 0.0))
                  for i in range(30)]
        z = momentum_z(closes, 10)
        self.assertEqual(z[:10], [None] * 10)
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(20, 30)]
        mean = sum(rets) / 10
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / 9)
        self.assertAlmostEqual(z[29], mean / sd * math.sqrt(10))

    def test_momentum_z_flat_tape_is_none(self):
        self.assertIsNone(momentum_z([100.0] * 20, 5)[-1])


class EntrySignal(unittest.TestCase):
    CFG = BaseConfig(entry_period=5, atr_period=3, trend="none")

    def _ind(self, closes, cfg=None, spread=1.0):
        return indicators(_bars(closes, spread), cfg or self.CFG)

    def test_long_on_close_above_prior_high(self):
        ind = self._ind([100.0] * 6 + [103.0])
        self.assertEqual(entry_signal(ind, 6, self.CFG), "long")

    def test_no_entry_at_channel(self):
        ind = self._ind([100.0] * 6 + [101.0])      # prior high = 101 (100 + spread)
        self.assertIsNone(entry_signal(ind, 6, self.CFG))

    def test_buffer_in_atr_units(self):
        cfg = replace(self.CFG, entry_buffer_atr=1.0)
        ind = self._ind([100.0] * 6 + [102.5], cfg)  # ATR = 2, needs > 101 + 2
        self.assertIsNone(entry_signal(ind, 6, cfg))
        ind = self._ind([100.0] * 6 + [103.5], cfg)
        self.assertEqual(entry_signal(ind, 6, cfg), "long")

    def test_short_mirror_and_side_switch(self):
        ind = self._ind([100.0] * 6 + [97.0])
        self.assertEqual(entry_signal(ind, 6, self.CFG), "short")
        cfg = replace(self.CFG, sides=("long",))
        self.assertIsNone(entry_signal(self._ind([100.0] * 6 + [97.0], cfg), 6, cfg))

    def test_warmup_is_none(self):
        ind = self._ind([100.0, 103.0])
        self.assertIsNone(entry_signal(ind, 1, self.CFG))

    def test_ema_trend_blocks_counter_trend(self):
        cfg = replace(self.CFG, trend="ema", trend_fast=2, trend_slow=20)
        base = [100.0] * 6 + [103.0]                      # breakout at index 26
        down = [140.0 - i * 35.0 / 19.0 for i in range(20)] + base
        ind = self._ind(down, cfg)
        self.assertIsNotNone(ind["ema_slow"][26])
        self.assertGreater(ind["ema_slow"][26], 103.0)
        self.assertIsNone(entry_signal(ind, 26, cfg))       # close < slow EMA
        up = [60.0 + i * 35.0 / 19.0 for i in range(20)] + base
        ind = self._ind(up, cfg)
        self.assertEqual(entry_signal(ind, 26, cfg), "long")

    def test_ema_trend_needs_close_and_fast_on_the_trade_side(self):
        cfg = replace(self.CFG, trend="ema")
        def ind(fast, slow, close):
            return {"ema_fast": [fast], "ema_slow": [slow], "close": [close]}
        self.assertTrue(trend_ok("long", ind(105.0, 100.0, 101.0), 0, cfg, None))
        self.assertFalse(trend_ok("long", ind(105.0, 100.0, 99.0), 0, cfg, None))
        self.assertFalse(trend_ok("long", ind(99.0, 100.0, 101.0), 0, cfg, None))
        self.assertTrue(trend_ok("short", ind(95.0, 100.0, 99.0), 0, cfg, None))
        self.assertFalse(trend_ok("short", ind(95.0, 100.0, 101.0), 0, cfg, None))
        self.assertFalse(trend_ok("short", ind(101.0, 100.0, 99.0), 0, cfg, None))
        self.assertFalse(trend_ok("long", ind(None, 100.0, 101.0), 0, cfg, None))

    def test_regime_trend_filter_fails_closed(self):
        cfg = replace(self.CFG, trend="regime")
        ind = self._ind([100.0] * 6 + [103.0], cfg)
        self.assertEqual(entry_signal(ind, 6, cfg, regime="trend_up"), "long")
        for regime in ("chop", "trend_down", None):
            self.assertIsNone(entry_signal(ind, 6, cfg, regime=regime), regime)
        ind = self._ind([100.0] * 6 + [97.0], cfg)
        self.assertEqual(entry_signal(ind, 6, cfg, regime="trend_down"), "short")

    def test_tsmom_enters_on_cross_only(self):
        cfg = BaseConfig(entry="tsmom", entry_period=5, entry_z=2.0, atr_period=3,
                         trend="none")
        closes = [100.0, 100.5, 100.0, 100.5, 100.0, 100.5, 100.0,
                  101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        ind = indicators(_bars(closes), cfg)
        z = ind["z"]
        fired = [i for i in range(len(closes)) if entry_signal(ind, i, cfg) == "long"]
        self.assertTrue(fired, z)
        first = fired[0]
        self.assertGreaterEqual(z[first], 2.0)
        self.assertTrue(z[first - 1] is None or z[first - 1] < 2.0)
        self.assertEqual(len(fired), 1, (fired, z))


class PositionManagement(unittest.TestCase):
    CFG = BaseConfig(stop_atr=2.0, trail_atr=3.0)

    def test_initial_stop_and_risk(self):
        p = open_position("long", 100.0, T0, atr=5.0, cfg=self.CFG)
        self.assertEqual((p.stop, p.extreme, p.init_risk), (90.0, 100.0, 10.0))
        s = open_position("short", 100.0, T0, atr=5.0, cfg=self.CFG)
        self.assertEqual((s.stop, s.extreme), (110.0, 100.0))

    def test_stop_gap_fills_at_open(self):
        p = Position("long", T0, 100.0, stop=95.0, extreme=100.0, init_risk=5.0)
        self.assertEqual(stop_exit(p, 94.0, 96.0, 93.0), (94.0, "stop"))

    def test_stop_touch_fills_at_stop(self):
        p = Position("long", T0, 100.0, stop=95.0, extreme=100.0, init_risk=5.0)
        self.assertEqual(stop_exit(p, 99.0, 101.0, 94.5), (95.0, "stop"))
        self.assertIsNone(stop_exit(p, 99.0, 101.0, 95.5))

    def test_short_stop_mirror(self):
        p = Position("short", T0, 100.0, stop=105.0, extreme=100.0, init_risk=5.0)
        self.assertEqual(stop_exit(p, 106.0, 107.0, 104.0), (106.0, "stop"))
        self.assertEqual(stop_exit(p, 101.0, 105.5, 99.0), (105.0, "stop"))
        self.assertIsNone(stop_exit(p, 101.0, 104.0, 99.0))

    def test_chandelier_trail_ratchets(self):
        p = Position("long", T0, 100.0, stop=90.0, extreme=100.0, init_risk=10.0)
        ind = {"high": [120.0, 110.0], "low": [100.0, 100.0], "close": [118.0, 105.0],
               "atr": [5.0, 10.0], "exit_upper": [None, None], "exit_lower": [None, None]}
        self.assertIsNone(after_close(p, ind, 0, self.CFG))
        self.assertEqual((p.extreme, p.stop), (120.0, 105.0))   # 120 - 3*5
        self.assertIsNone(after_close(p, ind, 1, self.CFG))
        self.assertEqual(p.stop, 105.0)        # ATR doubled (120 - 30 = 90): never loosens
        self.assertEqual(p.bars_held, 2)

    def test_short_trail(self):
        p = Position("short", T0, 100.0, stop=110.0, extreme=100.0, init_risk=10.0)
        ind = {"high": [95.0, 90.0], "low": [80.0, 85.0], "close": [82.0, 88.0],
               "atr": [4.0, 8.0], "exit_upper": [None, None], "exit_lower": [None, None]}
        after_close(p, ind, 0, self.CFG)
        self.assertEqual((p.extreme, p.stop), (80.0, 92.0))
        after_close(p, ind, 1, self.CFG)
        self.assertEqual((p.extreme, p.stop), (80.0, 92.0))     # 80 + 24 = 104: no loosen

    def test_channel_exit(self):
        cfg = replace(self.CFG, exit_period=3)
        p = Position("long", T0, 100.0, stop=80.0, extreme=100.0, init_risk=20.0)
        ind = {"high": [101.0], "low": [97.0], "close": [97.5], "atr": [1.0],
               "exit_upper": [110.0], "exit_lower": [98.0]}
        self.assertEqual(after_close(p, ind, 0, cfg), "channel")

    def test_off_regime_trail_tightens_and_ratchets(self):
        # g3 per-regime exits: trail_atr while the bar reads the trade's trend,
        # trail_atr_off otherwise; the stop still never loosens.
        cfg = replace(self.CFG, trail_atr=5.0, trail_atr_off=3.0)
        p = Position("long", T0, 100.0, stop=70.0, extreme=100.0, init_risk=30.0)
        ind = {"high": [120.0, 120.0, 130.0], "low": [100.0, 100.0, 110.0],
               "close": [118.0, 115.0, 128.0], "atr": [5.0, 5.0, 5.0],
               "exit_upper": [None] * 3, "exit_lower": [None] * 3}
        after_close(p, ind, 0, cfg, regime="trend_up")
        self.assertEqual(p.stop, 95.0)                            # 120 - 5*5
        after_close(p, ind, 1, cfg, regime="chop")
        self.assertEqual(p.stop, 105.0)                           # 120 - 3*5
        after_close(p, ind, 2, cfg, regime="trend_up")
        self.assertEqual(p.stop, 105.0)                           # 130 - 25 = 105: held

    def test_off_regime_trail_fails_closed_and_mirrors_shorts(self):
        cfg = replace(self.CFG, trail_atr=5.0, trail_atr_off=3.0)
        ind = {"high": [100.0], "low": [80.0], "close": [82.0], "atr": [4.0],
               "exit_upper": [None], "exit_lower": [None]}
        for regime, stop in ((None, 92.0), ("trend_up", 92.0), ("trend_down", 100.0)):
            with self.subTest(regime=regime):
                p = Position("short", T0, 100.0, stop=120.0, extreme=100.0, init_risk=20.0)
                after_close(p, ind, 0, cfg, regime=regime)
                self.assertEqual(p.stop, stop)     # only trend_down is with-trend

    def test_off_regime_trail_zero_is_the_legacy_trail(self):
        p = Position("long", T0, 100.0, stop=90.0, extreme=100.0, init_risk=10.0)
        ind = {"high": [120.0], "low": [100.0], "close": [118.0], "atr": [5.0],
               "exit_upper": [None], "exit_lower": [None]}
        after_close(p, ind, 0, self.CFG, regime="chop")
        self.assertEqual(p.stop, 105.0)                           # 120 - 3*5, unchanged
        with self.assertRaises(ValueError):
            BaseConfig(trail_atr_off=-1.0)

    def test_time_stop(self):
        cfg = replace(self.CFG, max_hold_bars=2)
        p = Position("long", T0, 100.0, stop=80.0, extreme=100.0, init_risk=20.0)
        ind = {"high": [101.0, 101.0], "low": [99.0, 99.0], "close": [100.0, 100.0],
               "atr": [1.0, 1.0], "exit_upper": [None, None], "exit_lower": [None, None]}
        self.assertIsNone(after_close(p, ind, 0, cfg))
        self.assertEqual(after_close(p, ind, 1, cfg), "time")


class JevVeto(unittest.TestCase):
    V = VetoConfig()

    def test_pre_registered_defaults(self):
        # Pre-registered 2026-10-04 from base rates only (no outcome data):
        # each bar blocks a minority of the 09-27..09-30 cycles.
        self.assertEqual((self.V.long_veto_dump, self.V.short_veto_pump), (65.0, 65.0))
        self.assertEqual((self.V.veto_exhaustion, self.V.long_veto_whipsaw), (0.55, 0.65))
        self.assertEqual(self.V.veto_phases, ("capitulation",))
        self.assertTrue(self.V.fail_closed)

    def test_clean_verdict_allows_both_sides(self):
        self.assertEqual(jev_veto("long", _verdict(), self.V), [])
        self.assertEqual(jev_veto("short", _verdict(), self.V), [])

    def test_long_vetoes(self):
        self.assertEqual(jev_veto("long", _verdict(dump_0_100=65.0), self.V), ["jev_dump"])
        self.assertEqual(jev_veto("long", _verdict(whipsaw_prob=0.65), self.V),
                         ["jev_whipsaw"])
        self.assertEqual(jev_veto("long", _verdict(exhaustion_prob=0.6), self.V),
                         ["jev_exhaustion"])

    def test_short_side_ignores_upside_whipsaw(self):
        v = _verdict(whipsaw_prob=0.9, pump_0_100=70.0)
        self.assertEqual(jev_veto("short", v, self.V), ["jev_pump"])

    def test_capitulation_blocks_both(self):
        v = _verdict(phase="capitulation")
        self.assertEqual(jev_veto("long", v, self.V), ["jev_phase"])
        self.assertEqual(jev_veto("short", v, self.V), ["jev_phase"])

    def test_all_reasons_reported(self):
        v = _verdict(dump_0_100=80.0, exhaustion_prob=0.7, whipsaw_prob=0.7,
                     phase="capitulation")
        self.assertEqual(jev_veto("long", v, self.V),
                         ["jev_dump", "jev_exhaustion", "jev_whipsaw", "jev_phase"])

    def test_unavailable_and_malformed(self):
        self.assertEqual(jev_veto("long", None, self.V), ["jev_unavailable"])
        self.assertEqual(jev_veto("long", {"ok": False}, self.V), ["jev_unavailable"])
        self.assertEqual(jev_veto("long", _verdict(dump_0_100=float("nan")), self.V),
                         ["jev_malformed"])
        self.assertEqual(jev_veto("short", _verdict(pump_0_100=None), self.V),
                         ["jev_malformed"])
        self.assertEqual(jev_veto("long", _verdict(dump_0_100=True), self.V),
                         ["jev_malformed"])                 # bool is not a score
        # whipsaw is a long-only bar: a short never needs it present
        self.assertEqual(jev_veto("short", _verdict(whipsaw_prob=None), self.V), [])
        open_ = replace(self.V, fail_closed=False)
        self.assertEqual(jev_veto("long", None, open_), [])
        self.assertEqual(jev_veto("long", _verdict(dump_0_100=None), open_), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
