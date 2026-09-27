#!/usr/bin/env python3
"""Tests for jev_risk.PortfolioRisk (M5 portfolio risk) — pure math, no network.

Every expectation is hand-computed from the config/v2.yaml M5 numbers:
  pair_spot_cap 0.30, pair_perps_margin_cap 0.10, basket_long_cap 0.40,
  basket_short_cap 0.20, global_daily_loss -0.05, drawdown_halt 0.10,
  drawdown_recover 0.05, min_position_pct 0.01.

Run: .venv/bin/python scripts/test_jev_risk.py -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import PortfolioConfig  # noqa: E402
from jev_gates import PortfolioState, RiskConfig, decide  # noqa: E402
from jev_perps import PerpsConfig, PerpsPortfolioState, decide_perps  # noqa: E402
from jev_risk import PortfolioRisk  # noqa: E402

CFG = PortfolioConfig()
PAIRS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
DAY1 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc).timestamp()
NOW_MS = int(DAY1 * 1000)


def _flat_books(deployed=None, equities=None):
    """books_state / equity_by_book fixture: 3 pairs x (spot + perps), flat.

    ``deployed``: {(pair, book): {"notional": float, "margin": float,
                                  "side": "long"|"short"|None}}
    ``equities``: {(pair, book): float} — default 10000 per book (total 60000).
    """
    deployed = deployed or {}
    equities = equities or {}
    books_state, equity_by_book = {}, {}
    for pair in PAIRS:
        books_state[pair], equity_by_book[pair] = {}, {}
        for book in ("spot", "perps"):
            d = deployed.get((pair, book), {"notional": 0.0, "margin": 0.0,
                                            "side": None})
            books_state[pair][book] = {
                "notional": float(d["notional"]),
                "margin": float(d["margin"]),
                "side": d["side"],
            }
            equity_by_book[pair][book] = float(equities.get((pair, book), 10000.0))
    return books_state, equity_by_book


def _risk(state_path=None):
    return PortfolioRisk(cfg=CFG, state_path=state_path or "unused.json",
                         pairs=PAIRS)


def _pump_verdict():
    return {"ok": True, "error": None, "pump_0_100": 70.0, "dump_0_100": 10.0,
            "phase": "breakout", "exhaustion_prob": 0.21, "whipsaw_prob": 0.3,
            "confidence": 0.86}


def _flat_pf():
    return PortfolioState(has_position=False, equity_usd=10_000.0,
                          daily_pnl_pct=0.0, last_entry_ts_ms=None,
                          exit_signal_cycles=0, cycles_held=0)


def _long_pf():
    return PortfolioState(has_position=True, equity_usd=10_000.0,
                          daily_pnl_pct=0.0, last_entry_ts_ms=None,
                          exit_signal_cycles=2, cycles_held=3)


def _long_perps_pf():
    return PerpsPortfolioState(has_position=True, equity_usd=10_000.0,
                               daily_pnl_pct=0.0, last_entry_ts_ms=None,
                               exit_signal_cycles=2, cycles_held=3, side="long")


class PairCapTests(unittest.TestCase):
    """Per-pair caps: pair_spot_cap 0.30 x pair book equity, margin cap 0.10."""

    def test_pair_spot_cap_math(self):
        # BTC spot equity 10000 -> cap 3000; 2900 deployed -> 100 remaining.
        deployed = {("BTCUSDT", "spot"): {"notional": 2900.0, "margin": 0.0,
                                          "side": "long"}}
        books, eq = _flat_books(deployed)
        risk = _risk()
        risk.update(books, eq, DAY1)
        self.assertAlmostEqual(
            risk.remaining_capacity("spot", "long", "BTCUSDT", 10000.0), 100.0)
        # ETH has nothing deployed: full 3000 pair capacity (basket is 21100).
        self.assertAlmostEqual(
            risk.remaining_capacity("spot", "long", "ETHUSDT", 10000.0), 3000.0)

    def test_pair_spot_cap_exhausted_flags_pair(self):
        deployed = {("BTCUSDT", "spot"): {"notional": 3000.0, "margin": 0.0,
                                          "side": "long"}}
        books, eq = _flat_books(deployed)
        risk = _risk()
        flags = risk.update(books, eq, DAY1)
        self.assertTrue(flags["pair_cap"]["BTCUSDT"])
        self.assertFalse(flags["pair_cap"]["ETHUSDT"])
        self.assertEqual(
            risk.remaining_capacity("spot", "long", "BTCUSDT", 10000.0), 0.0)

    def test_pair_perps_margin_cap_math(self):
        # ETH perps: margin cap 0.10 x 10000 = 1000; 950 used -> 50 remaining.
        deployed = {("ETHUSDT", "perps"): {"notional": 2850.0, "margin": 950.0,
                                           "side": "long"}}
        books, eq = _flat_books(deployed)
        risk = _risk()
        risk.update(books, eq, DAY1)
        # basket_long = 2850, rem = 24000 - 2850 = 21150 -> /3 = 7050 (not binding)
        self.assertAlmostEqual(
            risk.remaining_capacity("perps", "long", "ETHUSDT", 10000.0,
                                    leverage=3.0), 50.0)
        # cap reached exactly -> pair flag, zero capacity
        deployed[("ETHUSDT", "perps")]["margin"] = 1000.0
        books, eq = _flat_books(deployed)
        flags = risk.update(books, eq, DAY1)
        self.assertTrue(flags["pair_cap"]["ETHUSDT"])
        self.assertEqual(
            risk.remaining_capacity("perps", "long", "ETHUSDT", 10000.0,
                                    leverage=3.0), 0.0)


class BasketCapTests(unittest.TestCase):
    """Basket caps across pairs: long 0.40 / short 0.20 of total equity 60000."""

    def _f1(self):
        # BTC spot long notional 5000; ETH perps long notional 9000 (margin
        # 3000); SOL perps short notional 6000 (margin 2000).
        deployed = {
            ("BTCUSDT", "spot"): {"notional": 5000.0, "margin": 0.0, "side": "long"},
            ("ETHUSDT", "perps"): {"notional": 9000.0, "margin": 3000.0, "side": "long"},
            ("SOLUSDT", "perps"): {"notional": 6000.0, "margin": 2000.0, "side": "short"},
        }
        return _flat_books(deployed)

    def test_basket_totals_across_pairs(self):
        books, eq = self._f1()
        risk = _risk()
        flags = risk.update(books, eq, DAY1)
        self.assertAlmostEqual(risk.basket_long, 5000.0 + 9000.0)   # 14000
        self.assertAlmostEqual(risk.basket_short, 6000.0)
        self.assertFalse(flags["basket_long"])   # 14000 < 24000
        self.assertFalse(flags["basket_short"])  # 6000 < 12000
        # long basket rem = 24000 - 14000 = 10000
        self.assertAlmostEqual(risk.basket_remaining("long"), 10000.0)
        # short basket rem = 12000 - 6000 = 6000
        self.assertAlmostEqual(risk.basket_remaining("short"), 6000.0)

    def test_basket_cap_binding_notional_units(self):
        # Fill the short basket to 11000 -> rem 1000 notional -> perps margin
        # rem at leverage 3 = 1000/3 = 333.333... < pair margin rem 1000.
        deployed = {
            ("ETHUSDT", "perps"): {"notional": 11000.0, "margin": 3666.67,
                                   "side": "short"},
        }
        books, eq = _flat_books(deployed)
        risk = _risk()
        risk.update(books, eq, DAY1)
        self.assertAlmostEqual(
            risk.remaining_capacity("perps", "short", "BTCUSDT", 10000.0,
                                    leverage=3.0), 1000.0 / 3.0, places=9)
        self.assertAlmostEqual(
            risk.remaining_capacity("spot", "long", "BTCUSDT", 10000.0),
            3000.0)  # pair binds for spot (basket_long untouched)

    def test_basket_long_exhausted_flags(self):
        deployed = {
            ("BTCUSDT", "spot"): {"notional": 12000.0, "margin": 0.0, "side": "long"},
            ("ETHUSDT", "spot"): {"notional": 12000.0, "margin": 0.0, "side": "long"},
        }
        books, eq = _flat_books(deployed)
        risk = _risk()
        flags = risk.update(books, eq, DAY1)  # basket_long = 24000 = cap
        self.assertTrue(flags["basket_long"])
        self.assertEqual(risk.basket_remaining("long"), 0.0)
        self.assertEqual(
            risk.remaining_capacity("spot", "long", "SOLUSDT", 10000.0), 0.0)

    def test_basket_short_exhausted_flags(self):
        deployed = {
            ("BTCUSDT", "perps"): {"notional": 12000.0, "margin": 4000.0,
                                   "side": "short"},
        }
        books, eq = _flat_books(deployed)
        risk = _risk()
        flags = risk.update(books, eq, DAY1)  # basket_short = 12000 = cap
        self.assertTrue(flags["basket_short"])
        self.assertEqual(risk.basket_remaining("short"), 0.0)


class GlobalDailyKillTests(unittest.TestCase):
    """Portfolio daily PnL <= -5% blocks ALL entries (boundary-exact)."""

    def _kill_at(self, total_equity):
        books, eq = _flat_books()
        risk = _risk()
        risk.update(books, eq, DAY1)  # base = 60000, day set
        # fault injection: scale every book equity to hit the target total
        scale = total_equity / 60000.0
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] *= scale
        return risk.update(books, eq, DAY1)

    def test_no_kill_at_minus_4_99_pct(self):
        flags = self._kill_at(60000.0 * (1.0 - 0.0499))
        self.assertFalse(flags["global_daily_kill"])

    def test_kill_at_exactly_minus_5_pct(self):
        flags = self._kill_at(57000.0)  # -3000/60000 = -5.00%
        self.assertTrue(flags["global_daily_kill"])

    def test_kill_at_minus_5_01_pct(self):
        flags = self._kill_at(60000.0 * (1.0 - 0.0501))
        self.assertTrue(flags["global_daily_kill"])

    def test_kill_blocks_capacity_but_never_exits(self):
        books, eq = _flat_books()
        risk = _risk()
        risk.update(books, eq, DAY1)
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] *= 0.94  # -6% on the day
        flags = risk.update(books, eq, DAY1)
        self.assertTrue(flags["global_daily_kill"])
        self.assertEqual(
            risk.remaining_capacity("spot", "long", "BTCUSDT", 10000.0), 0.0)
        budget = risk.entry_budget("spot", "BTCUSDT", 10000.0)
        # entries vetoed ...
        d = decide(_pump_verdict(), _flat_pf(), RiskConfig(), NOW_MS,
                   regime="trend_up", risk=budget)
        self.assertEqual(d["action"], "skip")
        self.assertIn("global_daily_kill", d["vetoed_by"])
        # ... while the exit path is untouched (dump 80 >= hard bar)
        exit_verdict = dict(_pump_verdict(), dump_0_100=80.0)
        d = decide(exit_verdict, _long_pf(), RiskConfig(), NOW_MS,
                   regime="trend_up", risk=budget)
        self.assertEqual(d["action"], "exit")
        self.assertEqual(d["vetoed_by"], [])
        pd = decide_perps(exit_verdict, _long_perps_pf(), PerpsConfig(), NOW_MS,
                          funding_rate=None, regime="trend_up", risk=budget)
        self.assertEqual(pd["action"], "exit")
        self.assertEqual(pd["vetoed_by"], [])


class DrawdownHaltTests(unittest.TestCase):
    """Halt at dd >= 10% from the running peak; clear only below 5%."""

    def _dd_at(self, equity_now):
        books, eq = _flat_books()
        risk = _risk()
        risk.update(books, eq, DAY1)  # peak = 60000
        scale = equity_now / 60000.0
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] *= scale
        return risk.update(books, eq, DAY1)

    def test_no_halt_at_9_99_pct_dd(self):
        flags = self._dd_at(60000.0 * (1.0 - 0.0999))
        self.assertFalse(flags["drawdown_halt"])

    def test_halt_at_exactly_10_pct_dd(self):
        flags = self._dd_at(54000.0)  # 6000/60000 = 10.00% dd
        self.assertTrue(flags["drawdown_halt"])

    def test_hysteresis_stays_halted_at_5_pct(self):
        books, eq = _flat_books()
        risk = _risk()
        risk.update(books, eq, DAY1)
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] = 9000.0  # 54000 total: dd = 10% -> halt
        self.assertTrue(risk.update(books, eq, DAY1)["drawdown_halt"])
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] = 9500.0  # 57000 total: dd = 5% exactly
        self.assertTrue(risk.update(books, eq, DAY1)["drawdown_halt"])
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] = 9501.0  # 57006 total: dd = 4.99% -> clear
        flags = risk.update(books, eq, DAY1)
        self.assertFalse(flags["drawdown_halt"])

    def test_halt_blocks_capacity_but_never_exits(self):
        books, eq = _flat_books()
        risk = _risk()
        risk.update(books, eq, DAY1)
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] = 9000.0  # dd = 10%
        risk.update(books, eq, DAY1)
        budget = risk.entry_budget("perps", "ETHUSDT", 9000.0, leverage=3.0)
        d = decide_perps(_pump_verdict(), _flat_pf(), PerpsConfig(), NOW_MS,
                         funding_rate=None, regime="trend_up", risk=budget)
        self.assertEqual(d["action"], "skip")
        self.assertIn("drawdown_halt", d["vetoed_by"])
        exit_verdict = dict(_pump_verdict(), dump_0_100=80.0)
        d = decide(exit_verdict, _long_pf(), RiskConfig(), NOW_MS,
                   regime="trend_up", risk=budget)
        self.assertEqual(d["action"], "exit")


class DailyResetTests(unittest.TestCase):
    """Daily PnL base resets at UTC midnight (fixture clock)."""

    def test_base_resets_at_utc_midnight(self):
        books, eq = _flat_books()
        risk = _risk()
        risk.update(books, eq, DAY1)  # base = 60000
        self.assertAlmostEqual(risk.daily_pnl_base, 60000.0)
        # fault: -2% on the day, same UTC day
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] *= 0.98
        just_before = datetime(2026, 9, 28, 23, 59, tzinfo=timezone.utc).timestamp()
        risk.update(books, eq, just_before)
        self.assertAlmostEqual(risk.daily_pnl_base, 60000.0)  # unchanged
        self.assertAlmostEqual(risk.daily_pnl_frac, -0.02, places=9)
        # next UTC day: base resets to the current total equity (58800)
        just_after = datetime(2026, 9, 29, 0, 1, tzinfo=timezone.utc).timestamp()
        risk.update(books, eq, just_after)
        self.assertAlmostEqual(risk.daily_pnl_base, 58800.0)
        self.assertAlmostEqual(risk.daily_pnl_frac, 0.0)
        self.assertEqual(risk.day, "2026-09-29")

    def test_reset_lifts_daily_kill(self):
        books, eq = _flat_books()
        risk = _risk()
        risk.update(books, eq, DAY1)
        for pair in PAIRS:
            for book in ("spot", "perps"):
                eq[pair][book] *= 0.93  # -7% -> kill
        self.assertTrue(risk.update(books, eq, DAY1)["global_daily_kill"])
        just_after = datetime(2026, 9, 29, 0, 1, tzinfo=timezone.utc).timestamp()
        flags = risk.update(books, eq, just_after)  # new day: base = current
        self.assertFalse(flags["global_daily_kill"])


class MinPositionTests(unittest.TestCase):
    """Remaining capacity below 1% equity is a dust veto (pair_cap/basket_cap)."""

    def test_remaining_capacity_dust_is_vetoed_by_gates(self):
        # BTC spot: cap 3000, 2950 deployed -> 50 remaining = 0.5% of 10000
        # equity < min_position_pct 1% -> the entry must be vetoed.
        deployed = {("BTCUSDT", "spot"): {"notional": 2950.0, "margin": 0.0,
                                          "side": "long"}}
        books, eq = _flat_books(deployed)
        risk = _risk()
        risk.update(books, eq, DAY1)
        self.assertAlmostEqual(
            risk.remaining_capacity("spot", "long", "BTCUSDT", 10000.0), 50.0)
        budget = risk.entry_budget("spot", "BTCUSDT", 10000.0)
        self.assertEqual(budget["min_position_pct"], 0.01)
        d = decide(_pump_verdict(), _flat_pf(), RiskConfig(), NOW_MS,
                   regime="trend_up", risk=budget)
        self.assertEqual(d["action"], "skip")
        self.assertIn("pair_cap", d["vetoed_by"])

    def test_capacity_above_floor_enters_clamped(self):
        # 100 remaining = 1% of equity: exactly at the floor -> allowed, size
        # clamped to the remaining pair capacity (tier wants 0.20 x 1.0 tier).
        deployed = {("BTCUSDT", "spot"): {"notional": 2900.0, "margin": 0.0,
                                          "side": "long"}}
        books, eq = _flat_books(deployed)
        risk = _risk()
        risk.update(books, eq, DAY1)
        budget = risk.entry_budget("spot", "BTCUSDT", 10000.0)
        d = decide(_pump_verdict(), _flat_pf(), RiskConfig(), NOW_MS,
                   regime="trend_up", risk=budget)
        self.assertEqual(d["action"], "enter")
        self.assertEqual(d["size_fraction"], 0.01)  # 100 / 10000


class StatePersistenceTests(unittest.TestCase):
    """risk_state.json round-trip + crash-safety (M0 atomic-write pattern)."""

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            books, eq = _flat_books()
            risk = _risk(str(path))
            risk.update(books, eq, DAY1)
            for pair in PAIRS:
                for book in ("spot", "perps"):
                    eq[pair][book] *= 0.92  # -8%: dd 8% (no halt), kill on
            risk.update(books, eq, DAY1)
            self.assertTrue(risk.save())

            back = _risk(str(path))
            self.assertTrue(back.load())
            self.assertAlmostEqual(back.equity_peak, 60000.0)
            self.assertAlmostEqual(back.daily_pnl_base, 60000.0)
            self.assertEqual(back.day, risk.day)
            self.assertFalse(back.drawdown_halted)
            self.assertEqual(back.flags, risk.flags)

    def test_write_failure_leaves_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            old = {"equity_peak": 11111.0, "day": "2026-09-01",
                   "daily_pnl_base": 11111.0, "drawdown_halted": True}
            path.write_text(json.dumps(old))
            books, eq = _flat_books()
            risk = _risk(str(path))
            risk.update(books, eq, DAY1)  # peak/base move in memory
            with mock.patch("os.replace", side_effect=OSError("injected")):
                self.assertFalse(risk.save())  # fail-open: never raises
            # old file untouched and still parseable; no tmp litter
            self.assertEqual(json.loads(path.read_text()), old)
            self.assertFalse(Path(str(path) + ".tmp").exists())

    def test_load_recovers_persisted_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            books, eq = _flat_books()
            risk = _risk(str(path))
            risk.update(books, eq, DAY1)
            for pair in PAIRS:
                for book in ("spot", "perps"):
                    eq[pair][book] = 9000.0  # dd 10% -> halt
            risk.update(books, eq, DAY1)
            self.assertTrue(risk.drawdown_halted)
            self.assertTrue(risk.save())
            back = _risk(str(path))
            back.load()
            self.assertTrue(back.drawdown_halted)
            # and the halt still blocks capacity after the restart
            self.assertEqual(
                back.remaining_capacity("spot", "long", "BTCUSDT", 9000.0), 0.0)


class EntryBudgetTests(unittest.TestCase):
    """entry_budget() is the exact dict contract jev_gates/jev_perps consume."""

    def test_budget_shape_and_values(self):
        deployed = {("BTCUSDT", "spot"): {"notional": 2900.0, "margin": 0.0,
                                          "side": "long"}}
        books, eq = _flat_books(deployed)
        risk = _risk()
        risk.update(books, eq, DAY1)
        budget = risk.entry_budget("spot", "BTCUSDT", 10000.0)
        self.assertEqual(set(budget), {"global_daily_kill", "drawdown_halt",
                                       "pair_remaining", "basket_remaining_long",
                                       "basket_remaining_short",
                                       "min_position_pct"})
        self.assertFalse(budget["global_daily_kill"])
        self.assertFalse(budget["drawdown_halt"])
        self.assertAlmostEqual(budget["pair_remaining"], 100.0)
        self.assertAlmostEqual(budget["basket_remaining_long"], 21100.0)
        self.assertAlmostEqual(budget["min_position_pct"], 0.01)

    def test_perps_budget_translates_basket_by_leverage(self):
        deployed = {
            ("ETHUSDT", "perps"): {"notional": 11000.0, "margin": 3666.67,
                                   "side": "short"},
        }
        books, eq = _flat_books(deployed)
        risk = _risk()
        risk.update(books, eq, DAY1)
        budget = risk.entry_budget("perps", "BTCUSDT", 10000.0, leverage=3.0)
        self.assertAlmostEqual(budget["pair_remaining"], 1000.0)  # margin units
        self.assertAlmostEqual(budget["basket_remaining_long"], 24000.0 / 3.0)
        self.assertAlmostEqual(budget["basket_remaining_short"], 1000.0 / 3.0,
                               places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)