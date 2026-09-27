#!/usr/bin/env python3
"""Tests for jev_regime (M3): deterministic OHLCV regime classifier. No network.

Run: .venv/bin/python scripts/test_jev_regime.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import RegimeConfig  # noqa: E402
from jev_regime import (  # noqa: E402
    REGIMES,
    classify,
    classify_features,
    donchian_width_percentile,
    ema_slope,
    realized_vol_ratio,
)

CFG = RegimeConfig()


def _rows(closes, spread=0.0, t0=1_800_000_000_000, step_ms=900_000):
    """OHLCV rows [ts, o, h, l, c, v] from closes; high/low = close*(1+-spread)."""
    return [[t0 + i * step_ms, c, c * (1.0 + spread), c * (1.0 - spread), c, 1.0]
            for i, c in enumerate(closes)]


# Synthetic fixtures: a flat 0.6%-wide range (the F-P0-2 chop), steady trends.
FLAT_15M = _rows([100.0, 100.06, 100.03, 99.98] * 30, spread=0.001)
FLAT_1H = _rows([100.0] * 40)
UP_15M = _rows([100.0 * 1.005 ** t for t in range(80)])
UP_1H = _rows([100.0 * 1.005 ** t for t in range(40)], step_ms=3_600_000)
DOWN_1H = _rows([100.0 * 0.995 ** t for t in range(40)], step_ms=3_600_000)


class ClassifyTests(unittest.TestCase):
    def test_flat_range_is_chop(self):
        self.assertEqual(classify(FLAT_15M, FLAT_1H), "chop")

    def test_steady_uptrend_is_trend_up(self):
        self.assertEqual(classify(UP_15M, UP_1H), "trend_up")

    def test_steady_downtrend_is_trend_down(self):
        self.assertEqual(classify(UP_15M, DOWN_1H), "trend_down")

    def test_deterministic_same_input_same_output(self):
        for a, b in ((FLAT_15M, FLAT_1H), (UP_15M, UP_1H), (UP_15M, DOWN_1H)):
            self.assertEqual(classify(a, b), classify(a, b))
            self.assertEqual(classify_features(a, b), classify_features(a, b))

    def test_output_domain(self):
        self.assertEqual(set(REGIMES), {"chop", "trend_up", "trend_down"})
        self.assertIn(classify(FLAT_15M, FLAT_1H), REGIMES)

    def test_insufficient_data_is_chop(self):
        self.assertEqual(classify([], []), "chop")
        self.assertEqual(classify(FLAT_15M, []), "chop")
        self.assertEqual(classify([], FLAT_1H), "chop")
        self.assertEqual(classify(FLAT_15M[:5], FLAT_1H[:3]), "chop")

    def test_bad_input_never_raises_is_chop(self):
        for a, b in ((None, "x"), ("x", None), ([[1, 2]], [[1]]),
                     ([[0, 0, 0, 0, 0, 0]] * 30, FLAT_1H)):
            self.assertEqual(classify(a, b), "chop")


class SlopeTests(unittest.TestCase):
    def test_ema_slope_sign_and_flat(self):
        self.assertGreater(ema_slope(UP_1H), 0.0)
        self.assertLess(ema_slope(DOWN_1H), 0.0)
        self.assertEqual(ema_slope(FLAT_1H), 0.0)

    def test_ema_slope_normalized_per_bar(self):
        # steady +0.5%/bar -> slope per bar ~ +0.5% (EMA lag reduces it a little)
        s = ema_slope(UP_1H)
        self.assertGreater(s, 0.003)
        self.assertLess(s, 0.006)

    def test_flat_slope_threshold_edges(self):
        s = ema_slope(UP_1H)
        self.assertEqual(classify(UP_15M, UP_1H, RegimeConfig(flat_slope=s * 1.1)), "chop")
        self.assertEqual(classify(UP_15M, UP_1H, RegimeConfig(flat_slope=s * 0.9)),
                         "trend_up")
        # strictly-greater rule: slope exactly at the eps is NOT a trend
        self.assertEqual(classify(UP_15M, UP_1H, RegimeConfig(flat_slope=s)), "chop")

    def test_flat_slope_knob_moves_the_verdict(self):
        # 0.5%/bar drift reads "chop" once flat_slope is widened past it
        self.assertEqual(classify(UP_15M, UP_1H, RegimeConfig(flat_slope=0.01)), "chop")
        self.assertEqual(classify(UP_15M, UP_1H, RegimeConfig(flat_slope=0.0001)),
                         "trend_up")


class VolRatioTests(unittest.TestCase):
    def test_flat_closes_have_zero_ratio(self):
        rows = _rows([100.0] * 40)
        self.assertEqual(realized_vol_ratio(rows), 0.0)

    def test_compressed_wicks_tight_closes(self):
        # closes barely move inside 1%-wide bars -> realized << ATR expectation
        closes = [100.0, 100.02] * 30
        rows = _rows(closes, spread=0.005)
        self.assertLess(realized_vol_ratio(rows), 0.8)

    def test_uncompressed_close_moves(self):
        closes = [100.0, 101.0] * 30
        rows = _rows(closes, spread=0.0005)
        self.assertGreaterEqual(realized_vol_ratio(rows), 0.8)

    def test_vol_window_parameter_edge(self):
        # noisy past + quiet present: window 16 sees only the quiet tape
        closes = [100.0, 101.0] * 20 + [100.0] * 20
        rows = _rows(closes, spread=0.0005)
        quiet = realized_vol_ratio(rows, RegimeConfig(vol_window=16))
        noisy = realized_vol_ratio(rows, RegimeConfig(vol_window=40))
        self.assertLess(quiet, 0.8)
        self.assertGreater(noisy, 1.0)
        self.assertLess(quiet, noisy)

    def test_atr_period_parameter_edge(self):
        closes = [100.0, 100.02] * 30
        rows = _rows(closes, spread=0.005)
        rows[-1][2] = rows[-1][4] * 1.05  # one late high-wick outlier
        short = realized_vol_ratio(rows, RegimeConfig(atr_period=2))
        long_ = realized_vol_ratio(rows, RegimeConfig(atr_period=30))
        self.assertNotEqual(short, long_)  # the knob actually feeds the math

    def test_insufficient_bars_raise(self):
        with self.assertRaises(ValueError):
            realized_vol_ratio(_rows([100.0, 100.1]))


class DonchianTests(unittest.TestCase):
    def test_narrow_after_wide_history(self):
        rows = _rows([100.0] * 50, spread=0.01) + _rows([100.0] * 30, spread=0.0001)
        pct = donchian_width_percentile(rows)
        self.assertLess(pct, 0.4)

    def test_wide_after_narrow_history(self):
        rows = _rows([100.0] * 50, spread=0.0001) + _rows([100.0] * 30, spread=0.01)
        pct = donchian_width_percentile(rows)
        self.assertGreaterEqual(pct, 0.4)

    def test_constant_width_is_neutral_not_narrow(self):
        rows = _rows([100.0] * 80, spread=0.01)
        self.assertEqual(donchian_width_percentile(rows), 0.5)  # tie-safe mid-rank

    def test_narrow_percentile_knob(self):
        rows = _rows([100.0] * 50, spread=0.0001) + _rows([100.0] * 30, spread=0.01)
        self.assertTrue(classify_features(rows, FLAT_1H,
                                          RegimeConfig(narrow_percentile=1.0))["narrow"])
        self.assertFalse(classify_features(rows, FLAT_1H,
                                           RegimeConfig(narrow_percentile=0.0))["narrow"])

    def test_donchian_period_parameter_edge(self):
        rows = _rows([100.0] * 50, spread=0.0001) + _rows([100.0] * 30, spread=0.01)
        self.assertNotEqual(donchian_width_percentile(rows, RegimeConfig(donchian_period=5)),
                            donchian_width_percentile(rows, RegimeConfig(donchian_period=40)))

    def test_insufficient_bars_raise(self):
        with self.assertRaises(ValueError):
            donchian_width_percentile(_rows([100.0] * 5))


class FeaturesTests(unittest.TestCase):
    def test_features_shape(self):
        f = classify_features(FLAT_15M, FLAT_1H)
        self.assertEqual(f["regime"], "chop")
        for key in ("vol_ratio", "width_percentile", "slope", "compressed", "narrow", "flat"):
            self.assertIn(key, f)
        self.assertTrue(f["flat"])
        self.assertIsInstance(f["compressed"], bool)
        self.assertIsInstance(f["narrow"], bool)

    def test_trend_features(self):
        up = classify_features(UP_15M, UP_1H)
        self.assertEqual(up["regime"], "trend_up")
        self.assertGreater(up["slope"], 0.0)
        self.assertFalse(up["flat"])
        down = classify_features(UP_15M, DOWN_1H)
        self.assertEqual(down["regime"], "trend_down")
        self.assertLess(down["slope"], 0.0)

    def test_flat_feature_flags(self):
        f = classify_features(FLAT_15M, FLAT_1H)
        self.assertTrue(f["flat"])
        self.assertTrue(f["compressed"])  # tiny realized vol vs the bar range
        self.assertLess(f["vol_ratio"], 0.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)


