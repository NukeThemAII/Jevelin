#!/usr/bin/env python3
"""Tests for jev_cache (M2 decision cache) — stdlib only, no network, simulated time.

Run: .venv/bin/python scripts/test_jev_cache.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_cache import DecisionCache

STATE = '{"asof_ms": 1800000000000, "symbol": "BTCUSDT"}'
OTHER_STATE = '{"asof_ms": 1800000060000, "symbol": "BTCUSDT"}'
VERDICT = {
    "ok": True, "pump_0_100": 75.0, "dump_0_100": 25.0, "phase": "accumulation",
    "exhaustion_prob": 0.3, "whipsaw_prob": 0.2, "confidence": 0.8,
}
T0 = 1_800_000_000.0


class LookupTests(unittest.TestCase):
    """Exact state-hash hits inside cache_ttl; misses outside it."""

    def test_exact_hit_within_ttl(self):
        cache = DecisionCache()
        cache.store(STATE, VERDICT, price=100.0, now=T0)
        hit = cache.lookup(STATE, now=T0 + 1799.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["cache_hit"], "exact")
        self.assertEqual(hit["pump_0_100"], 75.0)

    def test_lookup_returns_a_copy(self):
        cache = DecisionCache()
        cache.store(STATE, VERDICT, price=100.0, now=T0)
        hit = cache.lookup(STATE, now=T0 + 1.0)
        hit["pump_0_100"] = -1.0  # caller mutation must not poison the cache
        self.assertEqual(cache.lookup(STATE, now=T0 + 2.0)["pump_0_100"], 75.0)

    def test_lookup_miss_unknown_state(self):
        cache = DecisionCache()
        cache.store(STATE, VERDICT, price=100.0, now=T0)
        self.assertIsNone(cache.lookup(OTHER_STATE, now=T0 + 1.0))

    def test_lookup_miss_empty_cache(self):
        self.assertIsNone(DecisionCache().lookup(STATE, now=T0))

    def test_ttl_expiry_miss(self):
        cache = DecisionCache(cache_ttl=1800.0)
        cache.store(STATE, VERDICT, price=100.0, now=T0)
        self.assertIsNone(cache.lookup(STATE, now=T0 + 1800.0 + 1.0))

    def test_ttl_boundary_still_hit(self):
        cache = DecisionCache(cache_ttl=1800.0)
        cache.store(STATE, VERDICT, price=100.0, now=T0)
        self.assertIsNotNone(cache.lookup(STATE, now=T0 + 1800.0))


class MaterialityTests(unittest.TestCase):
    """is_material(price_now, price_last_scored): 5 bp move OR TTL age-out."""

    def _cached(self, **kw):
        cache = DecisionCache(**kw)
        cache.store(STATE, VERDICT, price=100.0, now=T0)  # scored at T0
        return cache

    def test_defaults(self):
        cache = DecisionCache()
        self.assertEqual(cache.cache_min_move, 0.0005)
        self.assertEqual(cache.cache_ttl, 1800.0)

    def test_4_9bp_not_material(self):
        cache = self._cached(cache_min_move=0.0005)
        self.assertFalse(cache.is_material(100.049, 100.0, now=T0 + 1.0))

    def test_5_1bp_material(self):
        cache = self._cached(cache_min_move=0.0005)
        self.assertTrue(cache.is_material(100.051, 100.0, now=T0 + 1.0))

    def test_5bp_boundary_material(self):
        cache = self._cached(cache_min_move=0.0005)
        self.assertTrue(cache.is_material(100.05, 100.0, now=T0 + 1.0))

    def test_zero_move_not_material(self):
        cache = self._cached()
        self.assertFalse(cache.is_material(100.0, 100.0, now=T0 + 1.0))

    def test_move_is_relative(self):
        cache = self._cached(cache_min_move=0.0005)
        self.assertFalse(cache.is_material(50024.5, 50000.0, now=T0 + 1.0))  # 4.9 bp
        self.assertTrue(cache.is_material(50025.5, 50000.0, now=T0 + 1.0))  # 5.1 bp

    def test_down_move_counts(self):
        cache = self._cached(cache_min_move=0.0005)
        self.assertTrue(cache.is_material(99.94, 100.0, now=T0 + 1.0))

    def test_no_last_score_is_material(self):
        cache = DecisionCache()
        self.assertTrue(cache.is_material(100.0, 100.0, now=T0))
        self.assertTrue(cache.is_material(100.0, None, now=T0))

    def test_ttl_age_out_forces_material(self):
        cache = self._cached(cache_ttl=1800.0)
        self.assertFalse(cache.is_material(100.0, 100.0, now=T0 + 1799.0))
        self.assertTrue(cache.is_material(100.0, 100.0, now=T0 + 1801.0))


class CounterAndStoreTests(unittest.TestCase):
    """hit_count / miss_count counters and the last-score bookkeeping."""

    def test_hit_and_miss_counters(self):
        cache = DecisionCache()
        cache.note_hit("exact")
        cache.note_hit("stale_price")
        cache.note_miss()
        cache.note_miss()
        self.assertEqual(cache.hit_count, 2)
        self.assertEqual(cache.miss_count, 2)
        self.assertEqual(cache.hit_exact_count, 1)
        self.assertEqual(cache.hit_stale_count, 1)

    def test_stats_dict(self):
        cache = DecisionCache()
        cache.note_hit("stale_price")
        cache.note_miss()
        stats = cache.stats()
        self.assertEqual(stats["hit_count"], 1)
        self.assertEqual(stats["miss_count"], 1)
        self.assertEqual(stats["hit_exact_count"], 0)
        self.assertEqual(stats["hit_stale_count"], 1)

    def test_unknown_hit_kind_rejected(self):
        cache = DecisionCache()
        with self.assertRaises(ValueError):
            cache.note_hit("bogus")

    def test_store_tracks_last_score(self):
        cache = DecisionCache()
        cache.store(STATE, VERDICT, price=100.0, now=T0)
        self.assertEqual(cache.last_verdict["pump_0_100"], 75.0)
        self.assertEqual(cache.last_price, 100.0)
        self.assertEqual(cache.last_ts, T0)

    def test_store_is_idempotent_overwrite(self):
        cache = DecisionCache()
        cache.store(STATE, VERDICT, price=100.0, now=T0)
        verdict2 = dict(VERDICT, pump_0_100=42.0)
        cache.store(STATE, verdict2, price=101.0, now=T0 + 10.0)
        self.assertEqual(cache.lookup(STATE, now=T0 + 11.0)["pump_0_100"], 42.0)
        self.assertEqual(cache.last_price, 101.0)

    def test_store_defaults_to_clock(self):
        cache = DecisionCache()
        cache.store(STATE, VERDICT, price=100.0)
        self.assertIsNotNone(cache.last_ts)
        self.assertIsNotNone(cache.lookup(STATE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
