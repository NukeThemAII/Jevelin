#!/usr/bin/env python3
"""Tests for jev_router: causal scale-free features, forward labels, the L2
logistic fit, and the purged quarterly walk-forward with its
exposure-matched threshold.

Run: .venv/bin/python scripts/test_jev_router.py -v
"""
import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_router as jr  # noqa: E402
from jev_calibrate import since_ms  # noqa: E402

H4 = 4 * 3_600_000
T0 = int(since_ms("2023-07-01"))


def _bars(closes, t0=T0, step=H4):
    out, prev = [], closes[0]
    for k, c in enumerate(closes):
        out.append([t0 + k * step, prev, max(prev, c) * 1.002, min(prev, c) * 0.998, c])
        prev = c
    return out


def _walk(n, seed=1, vol=0.01):
    rng = random.Random(seed)
    p, out = 100.0, []
    for _ in range(n):
        p *= math.exp(rng.gauss(0.0, vol))
        out.append(p)
    return out


def _feats(slope=0.001):
    return {"regime": "trend_up", "slope": slope, "vol_ratio": 1.0,
            "width_percentile": 0.5}


class Labels(unittest.TestCase):
    def test_forward_close_up_over_h_bars(self):
        bars = _bars([10, 11, 9, 12, 12, 8])
        self.assertEqual(jr.labels(bars, h=2), [0, 1, 1, 0, None, None])

    def test_default_horizon_is_one_day_of_4h_bars(self):
        self.assertEqual(jr.LABEL_BARS, 6)


class Features(unittest.TestCase):
    def setUp(self):
        self.closes = _walk(120)
        self.bars = _bars(self.closes)
        self.atr = [None] * 13 + [1.0] * (len(self.bars) - 13)
        self.r1 = [_feats(0.002 + k * 1e-6) for k in range(len(self.bars))]
        self.live = [_feats(-0.001) for _ in self.bars]

    def test_order_and_values(self):
        self.assertEqual(jr.FEATURES, ("slope_4h", "slope_1h", "vol_ratio_1h",
                                       "width_pct_1h", "mom6", "mom42", "chan20"))
        x = jr.features(self.bars, self.atr, self.r1, self.live)
        i = 100
        c = self.closes
        a = 1.0 / c[i]
        hh = max(b[2] for b in self.bars[i - 19:i + 1])
        ll = min(b[3] for b in self.bars[i - 19:i + 1])
        want = (self.r1[i]["slope"], -0.001, 1.0, 0.5, math.log(c[i] / c[i - 6]) / a,
                math.log(c[i] / c[i - 42]) / a, (c[i] - ll) / (hh - ll))
        for got, exp in zip(x[i], want):
            self.assertAlmostEqual(got, exp)

    def test_warmup_and_missing_inputs_are_none(self):
        self.r1[60] = None
        self.live[61] = {"regime": "chop", "slope": None, "vol_ratio": None,
                         "width_percentile": None}
        self.atr[62] = None
        x = jr.features(self.bars, self.atr, self.r1, self.live)
        self.assertTrue(all(v is None for v in x[:42]))      # mom42 needs 42 prior bars
        self.assertIsNotNone(x[42])
        self.assertEqual((x[60], x[61], x[62]), (None, None, None))

    def test_causal(self):
        x = jr.features(self.bars, self.atr, self.r1, self.live)
        bent = [b[:] for b in self.bars]
        for b in bent[81:]:
            b[1:] = [v * 3.0 for v in b[1:]]
        y = jr.features(bent, self.atr, self.r1, self.live)
        self.assertEqual(x[:81], y[:81])

    def test_scale_free(self):
        # CH-3: no absolute-USD level reaches the model
        x = jr.features(self.bars, self.atr, self.r1, self.live)
        big = [[b[0]] + [v * 1000.0 for v in b[1:]] for b in self.bars]
        atr = [None if a is None else a * 1000.0 for a in self.atr]
        y = jr.features(big, atr, self.r1, self.live)
        for u, v in zip(x[42:], y[42:]):
            for p, q in zip(u, v):
                self.assertAlmostEqual(p, q, places=9)


class Logistic(unittest.TestCase):
    def test_recovers_a_known_model(self):
        rng = random.Random(3)
        true = (0.3, 1.5, -0.8)
        X, y = [], []
        for _ in range(6000):
            x = (rng.gauss(0, 1), rng.gauss(0, 1))
            p = 1.0 / (1.0 + math.exp(-(true[0] + true[1] * x[0] + true[2] * x[1])))
            X.append(x)
            y.append(1 if rng.random() < p else 0)
        w = jr.fit_logistic(X, y, l2=0.0)
        for got, exp in zip(w, true):
            self.assertAlmostEqual(got, exp, delta=0.12)

    def test_l2_shrinks_slopes_not_the_intercept(self):
        rng = random.Random(4)
        X = [(rng.gauss(0, 1),) for _ in range(400)]
        y = [1 if x[0] + rng.gauss(0, 1) > -0.5 else 0 for x in X]
        w = jr.fit_logistic(X, y, l2=1e9)
        self.assertAlmostEqual(w[1], 0.0, places=5)
        base = sum(y) / len(y)
        self.assertAlmostEqual(w[0], math.log(base / (1 - base)), places=5)

    def test_separable_data_stays_finite_under_l2(self):
        X = [(-2.0,), (-1.0,), (1.0,), (2.0,)]
        w = jr.fit_logistic(X, [0, 0, 1, 1], l2=1.0)
        self.assertTrue(all(math.isfinite(v) for v in w))
        self.assertGreater(w[1], 0)

    def test_standardize(self):
        mu, sd = jr.standardize([(1.0, 5.0), (3.0, 5.0)])
        self.assertEqual(mu, [2.0, 5.0])
        self.assertEqual(sd, [1.0, 1.0])               # constant column -> 1, not 0


class Quarters(unittest.TestCase):
    def test_calendar_quarters_in_bangkok(self):
        got = jr.quarter_starts(since_ms("2024-01-01"), since_ms("2024-09-17"))
        self.assertEqual(got, [int(since_ms(d)) for d in
                               ("2024-01-01", "2024-04-01", "2024-07-01")])

    def test_mid_quarter_start_is_its_own_first_fold(self):
        got = jr.quarter_starts(since_ms("2024-02-15"), since_ms("2024-04-02"))
        self.assertEqual(got, [int(since_ms("2024-02-15")), int(since_ms("2024-04-01"))])


def _wf_data(n=1600, seeds=(1, 2)):
    """Two symbols, 4h bars from T0 with a weak momentum effect to learn."""
    data = {}
    for s in seeds:
        closes = _walk(n, seed=s)
        bars = _bars(closes)
        atr = [None] * 13 + [c * 0.01 for c in closes[13:]]
        rng = random.Random(100 + s)
        r1 = [None] * 30 + [_feats(rng.gauss(0, 0.002)) for _ in range(n - 30)]
        live = [None] * 30 + [_feats(rng.gauss(0, 0.002)) for _ in range(n - 30)]
        regimes = [None if f is None else ("trend_up" if f["slope"] > 0.001 else "chop")
                   for f in r1]
        data[f"S{s}"] = {"bars": bars, "X": jr.features(bars, atr, r1, live),
                         "y": jr.labels(bars), "r1": regimes}
    return data


class WalkForward(unittest.TestCase):
    START = int(since_ms("2024-01-01"))
    END = int(since_ms("2024-04-15"))

    def _run(self, data=None):
        return jr.walk_forward(data or _wf_data(), self.START, self.END,
                               train_start_ms=T0, period_ms=H4)

    def test_routes_only_inside_the_window_and_on_known_labels(self):
        routes, folds = self._run()
        self.assertEqual([f["start"] for f in folds],
                         [self.START, int(since_ms("2024-04-01"))])
        data = _wf_data()
        for sym, labels in routes.items():
            ts = [b[0] for b in data[sym]["bars"]]
            self.assertEqual(len(labels), len(ts))
            for t, lab in zip(ts, labels):
                if t < self.START or t >= self.END:
                    self.assertIsNone(lab)
                else:
                    self.assertIn(lab, (jr.ON, jr.OFF))

    def test_purged_training_never_sees_a_label_past_the_fold_start(self):
        _, folds = self._run()
        for f in folds:
            self.assertGreater(f["n_train"], 1000)
            self.assertLessEqual(f["last_label_end"], f["start"])

    def test_exposure_matched_to_r1_on_training_rows(self):
        _, folds = self._run()
        for f in folds:
            self.assertAlmostEqual(f["train_on_share"], f["r1_on_share"],
                                   delta=1.0 / f["n_train"] + 1e-12)

    def test_future_data_cannot_move_earlier_folds(self):
        data = _wf_data()
        routes, _ = self._run(data)
        cut = int(since_ms("2024-04-01"))
        bent = _wf_data()
        for sym, d in bent.items():
            bars = d["bars"]
            for b in bars:
                if b[0] >= cut:
                    b[1:] = [v * 0.5 for v in b[1:]]
            d["y"] = jr.labels(bars)
            d["X"] = [x if b[0] < cut else (None if x is None else tuple(v * 2 for v in x))
                      for x, b in zip(d["X"], bars)]
        again, _ = self._run(bent)
        for sym in routes:
            ts = [b[0] for b in data[sym]["bars"]]
            keep = [k for k, t in enumerate(ts) if t < cut]
            self.assertEqual([routes[sym][k] for k in keep], [again[sym][k] for k in keep])

    def test_deterministic(self):
        self.assertEqual(self._run(), self._run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
