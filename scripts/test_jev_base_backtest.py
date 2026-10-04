#!/usr/bin/env python3
"""Tests for jev_base_backtest: history cache, resampling, causal regime
series, the bar-by-bar simulator, metrics and the random-entry control.
No network: kline fetches are injected.

Run: .venv/bin/python scripts/test_jev_base_backtest.py -v
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_base_backtest as bt  # noqa: E402
from jev_base import BaseConfig  # noqa: E402

M15 = 900_000
H = 3_600_000
T0 = 1_780_000_000_000 - 1_780_000_000_000 % (4 * H)   # 4h-aligned


def _bar(i, o, h, lo, c, step=H, t0=T0):
    return [t0 + i * step, float(o), float(h), float(lo), float(c)]


def _flat(n, price=100.0, spread=1.0, start=0, step=H):
    return [_bar(start + i, price, price + spread, price - spread, price, step)
            for i in range(n)]


CFG = BaseConfig(entry_period=3, atr_period=2, stop_atr=1.0, trail_atr=0.0,
                 trend="none", sides=("long",))


def _breakout_then(*tail):
    """5 flat bars, breakout close at index 5 (prior high 101, prior ATR 2),
    then ``tail`` bars from index 6."""
    bars = _flat(5) + [_bar(5, 100, 104, 99.5, 103)]
    return bars + [_bar(6 + k, *ohlc) for k, ohlc in enumerate(tail)]


class Resample(unittest.TestCase):
    def test_aggregates_ohlc_and_drops_open_bucket(self):
        sub = [[T0 + i * M15, 10.0 + i, 20.0 + i, 5.0 - i, 11.0 + i] for i in range(6)]
        out = bt.resample(sub, H)
        self.assertEqual(out, [[T0, 10.0, 23.0, 2.0, 14.0]])   # 2nd hour incomplete

    def test_keeps_bucket_with_internal_gap(self):
        sub = [[T0 + i * M15, 10.0, 11.0, 9.0, 10.0] for i in (0, 1, 3, 4, 5, 6, 7)]
        out = bt.resample(sub, H)
        self.assertEqual([b[0] for b in out], [T0, T0 + H])


class History(unittest.TestCase):
    def test_month_chunks_cached_and_trimmed(self):
        calls = []

        def fetch(url):
            calls.append(url)
            start = int(url.split("startTime=")[1].split("&")[0])
            end = int(url.split("endTime=")[1].split("&")[0])
            rows, t = [], start
            while t <= end and len(rows) < 1000:
                rows.append([t, "1", "2", "0.5", "1.5", "0"])
                t += M15
            return rows

        start = 1_782_864_000_000          # 2026-07-01 00:00 UTC
        end = start + 40 * 86_400_000      # into August
        now = start + 70 * 86_400_000      # July + August both closed
        with tempfile.TemporaryDirectory() as tmp:
            bars = bt.load_history("BTCUSDT", start, end, tmp, fetch=fetch, now_ms=now)
            n_calls = len(calls)
            again = bt.load_history("BTCUSDT", start, end, tmp, fetch=fetch, now_ms=now)
            self.assertEqual(len(calls), n_calls)                 # all cached
            self.assertEqual(bars, again)
            self.assertEqual(bars[0][0], start)
            self.assertLess(bars[-1][0], end)
            self.assertEqual(len({b[0] for b in bars}), len(bars))
            self.assertTrue(all(b[0] % M15 == 0 for b in bars))
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 2)

    def test_open_month_is_not_cached_and_open_bar_dropped(self):
        def fetch(url):
            start = int(url.split("startTime=")[1].split("&")[0])
            end = int(url.split("endTime=")[1].split("&")[0])
            return [[t, "1", "2", "0.5", "1.5", "0"]
                    for t in range(start, min(end, start + 999 * M15) + 1, M15)]

        start = 1_782_864_000_000
        now = start + 5 * 86_400_000 + 7 * 60_000        # mid-bar
        with tempfile.TemporaryDirectory() as tmp:
            bars = bt.load_history("BTCUSDT", start, now, tmp, fetch=fetch, now_ms=now)
            self.assertLessEqual(bars[-1][0] + M15, now)       # closed bars only
            self.assertEqual(list(Path(tmp).glob("*.json")), [])


class RegimeSeries(unittest.TestCase):
    def test_causal_alignment(self):
        n_h = 130
        sub = []
        for i in range(n_h * 4):
            p = 100.0 * (1.0 + 0.002 * (i // 4))
            sub.append([T0 + i * M15, p, p * 1.001, p * 0.999, p])
        hours = bt.resample(sub, H)
        base = bt.regime_series(sub, hours)
        self.assertEqual(len(base), len(hours))
        self.assertEqual(base[-1], "trend_up")
        # A crash AFTER hour k must not change the label at hour k.
        k = 125
        crashed = [b[:] for b in sub]
        for b in crashed[(k + 1) * 4:]:
            b[1:] = [x * 0.5 for x in b[1:]]
        again = bt.regime_series(crashed, bt.resample(crashed, H))
        self.assertEqual(again[:k + 1], base[:k + 1])

    def test_classifier_sees_only_bars_closed_by_the_signal_close(self):
        # classify's label ignores the 15m features (1h slope decides), so
        # assert on the windows themselves: live sees 60 x 15m + 120 x 1h.
        sub = [[T0 + i * M15, 100.0, 101.0, 99.0, 100.0] for i in range(130 * 4)]
        hours = bt.resample(sub, H)
        seen = []
        real = bt.classify
        bt.classify = lambda w15, w1h, cfg: seen.append((w15, w1h)) or "chop"
        try:
            bt.regime_series(sub, hours)
        finally:
            bt.classify = real
        self.assertEqual(len(seen), 130 - 119)               # warm from hour 119
        for k, (w15, w1h) in zip(range(119, 130), seen):
            close_t = hours[k][0] + H
            self.assertEqual((len(w15), len(w1h)), (60, 120))
            self.assertEqual(w15[-1][0] + M15, close_t)
            self.assertEqual(w1h[-1][0] + H, close_t)

    def test_warmup_is_none(self):
        sub = [[T0 + i * M15, 100.0, 101.0, 99.0, 100.0] for i in range(40)]
        self.assertTrue(all(r is None for r in bt.regime_series(sub, bt.resample(sub, H))))


class Simulate(unittest.TestCase):
    FEE, SLIP = 0.001, 0.0

    def _sim(self, bars, cfg=CFG, **kw):
        from jev_base import indicators
        return bt.simulate(indicators(bars, cfg), cfg, self.FEE, self.SLIP, **kw)

    def test_entry_next_open_then_stop_touch(self):
        trades = self._sim(_breakout_then((103, 104, 102.5, 103.5),
                                          (103.5, 103.6, 99.0, 99.5)))
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["side"], t["path"]), ("long", "stop"))
        self.assertEqual((t["entry_ts"], t["exit_ts"]), (T0 + 6 * H, T0 + 7 * H))
        self.assertEqual(t["entry_fill"], 103.0)
        self.assertAlmostEqual(t["exit_fill"], 103.0 - 3.25)   # Wilder ATR(2)[5] = 3.25
        ratio = (103.0 - 3.25) / 103.0
        net = ratio - 1 - self.FEE - self.FEE * ratio
        self.assertAlmostEqual(t["net_pct"], net * 100)
        self.assertAlmostEqual(t["r"], net / (3.25 / 103.0))
        self.assertEqual(t["signal_ts"], T0 + 5 * H)

    def test_fills_at_next_open_not_signal_close(self):
        t = self._sim(_breakout_then((103.4, 104, 103.0, 103.5)))[0]
        self.assertEqual((t["entry_fill"], t["entry_ts"]), (103.4, T0 + 6 * H))

    def test_trail_ratchets_after_the_stop_check(self):
        cfg = BaseConfig(entry_period=3, atr_period=2, stop_atr=1.0, trail_atr=1.0,
                         trend="none", sides=("long",))
        # bar 6: high 110 would lift a same-bar trail to ~106; low 100 is above
        # the initial stop (99.75) -> survives bar 6, stopped on bar 7 instead.
        bars = _breakout_then((103, 110, 100.0, 109), (109, 109.5, 100.0, 101))
        t = self._sim(bars, cfg)[0]
        self.assertEqual((t["path"], t["exit_ts"]), ("stop", T0 + 7 * H))
        self.assertGreater(t["exit_fill"], 103.0)               # the ratcheted stop

    def test_stop_can_hit_on_entry_bar(self):
        trades = self._sim(_breakout_then((103, 103.2, 99.0, 99.5)))
        self.assertEqual(trades[0]["path"], "stop")
        self.assertEqual(trades[0]["exit_ts"], T0 + 6 * H)

    def test_gap_through_stop_fills_at_open(self):
        trades = self._sim(_breakout_then((103, 104, 102.5, 103.5),
                                          (95.0, 96.0, 94.0, 95.5)))
        self.assertEqual(trades[0]["exit_fill"], 95.0)

    def test_slippage_is_adverse_both_sides(self):
        from jev_base import indicators
        bars = _breakout_then((103, 104, 102.5, 103.5), (103.5, 103.6, 99.0, 99.5))
        t = bt.simulate(indicators(bars, CFG), CFG, 0.0, 0.001)[0]
        self.assertAlmostEqual(t["entry_fill"], 103.0 * 1.001)
        self.assertAlmostEqual(t["exit_fill"], (103.0 * 1.001 - 3.25) * 0.999)

    def test_short_side(self):
        cfg = BaseConfig(entry_period=3, atr_period=2, stop_atr=1.0, trail_atr=0.0,
                         trend="none", sides=("short",))
        bars = _flat(5) + [_bar(5, 100, 100.5, 96, 97),
                           _bar(6, 97, 97.5, 96, 96.5), _bar(7, 96.5, 101, 96, 100)]
        t = self._sim(bars, cfg)[0]
        self.assertEqual((t["side"], t["path"]), ("short", "stop"))
        self.assertAlmostEqual(t["exit_fill"], 97.0 + 3.25)
        ratio = (97.0 + 3.25) / 97.0
        self.assertAlmostEqual(t["net_pct"], (1 - ratio - self.FEE - self.FEE * ratio) * 100)
        from jev_base import indicators
        t = bt.simulate(indicators(bars, cfg), cfg, 0.0, 0.001)[0]
        self.assertAlmostEqual(t["entry_fill"], 97.0 * 0.999)          # sell lower
        self.assertAlmostEqual(t["exit_fill"], (97.0 * 0.999 + 3.25) * 1.001)  # buy higher

    def test_channel_exit_acts_at_next_open(self):
        cfg = BaseConfig(entry_period=3, atr_period=2, stop_atr=5.0, trail_atr=0.0,
                         exit_period=2, trend="none", sides=("long",))
        # bar 7 closes below the 2-bar prior low (min(99.5, 102.5)) -> exit at bar 8 open
        bars = _breakout_then((103, 104, 102.5, 103.5), (103.5, 103.6, 99.0, 99.2),
                              (99.3, 99.6, 99.0, 99.4))
        t = self._sim(bars, cfg)[0]
        self.assertEqual((t["path"], t["exit_ts"], t["exit_fill"]),
                         ("channel", T0 + 8 * H, 99.3))

    def test_open_at_end_marked_at_last_close_without_exit_costs(self):
        t = self._sim(_breakout_then((103, 104, 102.5, 103.5)))[0]
        self.assertEqual((t["path"], t["exit_fill"]), ("open", 103.5))
        self.assertAlmostEqual(t["net_pct"], (103.5 / 103.0 - 1 - self.FEE) * 100)

    def test_no_entry_without_a_next_bar(self):
        self.assertEqual(self._sim(_flat(5) + [_bar(5, 100, 104, 99.5, 103)]), [])

    def test_start_ts_is_warmup_only(self):
        bars = _breakout_then((103, 104, 102.5, 103.5), (103.5, 103.6, 99.0, 99.5))
        self.assertEqual(self._sim(bars, start_ts=T0 + 6 * H), [])
        self.assertEqual(len(self._sim(bars, start_ts=T0 + 5 * H)), 1)

    def test_regime_list_feeds_the_filter(self):
        cfg = BaseConfig(entry_period=3, atr_period=2, stop_atr=1.0, trail_atr=0.0,
                         trend="regime", sides=("long",))
        bars = _breakout_then((103, 104, 102.5, 103.5), (103.5, 103.6, 99.0, 99.5))
        self.assertEqual(self._sim(bars, cfg, regimes=["chop"] * len(bars)), [])
        self.assertEqual(len(self._sim(bars, cfg, regimes=["trend_up"] * len(bars))), 1)
        self.assertEqual(self._sim(bars, cfg, regimes=None), [])     # fail closed

    def test_reentry_after_stop_on_new_signal(self):
        bars = _breakout_then((103, 103.2, 99.0, 99.5),            # stop on entry bar
                              (99.5, 100.0, 99.0, 99.8),
                              (99.8, 106.0, 99.7, 105.5),           # new breakout
                              (105.5, 106.5, 105.0, 106.0))
        trades = self._sim(bars)
        self.assertEqual([t["path"] for t in trades], ["stop", "open"])
        self.assertEqual(trades[1]["entry_ts"], T0 + 9 * H)


class Metrics(unittest.TestCase):
    def test_summary(self):
        trades = [{"net_pct": 2.0, "r": 1.0, "side": "long", "path": "stop", "bars": 3},
                  {"net_pct": -1.0, "r": -0.5, "side": "long", "path": "stop", "bars": 1},
                  {"net_pct": -2.0, "r": -1.0, "side": "short", "path": "open", "bars": 2},
                  {"net_pct": 4.0, "r": 2.0, "side": "long", "path": "time", "bars": 4}]
        m = bt.metrics(trades)
        self.assertEqual((m["n"], m["longs"], m["shorts"]), (4, 3, 1))
        self.assertAlmostEqual(m["pf"], 2.0)
        self.assertAlmostEqual(m["win"], 0.5)
        self.assertAlmostEqual(m["sum_pct"], 3.0)
        self.assertAlmostEqual(m["max_dd_pct"], 3.0)       # +2 -> -1 -> -3
        rs = [1.0, -0.5, -1.0, 2.0]
        mean = sum(rs) / 4
        sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / 3)
        self.assertAlmostEqual(m["avg_r"], mean)
        self.assertAlmostEqual(m["sqn"], mean / sd * 2.0)

    def test_drawdown_counts_from_starting_equity(self):
        m = bt.metrics([{"net_pct": -1.0, "r": -0.5, "side": "long"},
                        {"net_pct": 0.5, "r": 0.25, "side": "long"}])
        self.assertAlmostEqual(m["max_dd_pct"], 1.0)

    def test_empty(self):
        m = bt.metrics([])
        self.assertEqual(m["n"], 0)
        self.assertIsNone(m["pf"])
        self.assertIsNone(m["sqn"])


class RandomControl(unittest.TestCase):
    def test_deterministic_and_rate_bounded(self):
        from jev_base import indicators
        cfg = BaseConfig(entry_period=3, atr_period=2, stop_atr=1.0, trail_atr=0.0,
                         trend="none", sides=("long", "short"))
        bars = []
        p = 100.0
        for i in range(400):
            q = p * (1.0 + 0.01 * math.sin(i / 3.0))
            bars.append(_bar(i, p, max(p, q) + 0.5, min(p, q) - 0.5, q))
            p = q
        ind = indicators(bars, cfg)
        a = bt.random_control(ind, cfg, 0.001, 0.0, rate=0.05, long_share=0.5, seed=7)
        b = bt.random_control(ind, cfg, 0.001, 0.0, rate=0.05, long_share=0.5, seed=7)
        self.assertEqual(a, b)
        self.assertGreater(len(a), 0)
        self.assertEqual(bt.random_control(ind, cfg, 0.001, 0.0, rate=0.0,
                                           long_share=0.5, seed=7), [])


class Folds(unittest.TestCase):
    def test_by_entry_signal_time(self):
        trades = [{"signal_ts": 5}, {"signal_ts": 10}, {"signal_ts": 15}]
        self.assertEqual(bt.in_window(trades, 10, 15), [{"signal_ts": 10}])

    def test_cutoff_is_bangkok_midnight_sep17(self):
        # 2026-09-17 00:00 +07 = 2026-09-16 17:00 UTC
        self.assertEqual(bt.FIT_CUTOFF_MS, 1_789_578_000_000)


def _wave_fetch(calls=None):
    """Keyless-API stand-in: deterministic 15m wave (3-day cycle + drift)."""
    def fetch(url):
        if calls is not None:
            calls.append(url)
        start = int(url.split("startTime=")[1].split("&")[0])
        end = int(url.split("endTime=")[1].split("&")[0])
        rows, t = [], start - start % M15
        while t <= end and len(rows) < 1000:
            k = t // M15
            c = 100.0 * math.exp(0.04 * math.sin(2 * math.pi * k / 288) + 1e-5 * (k % 5000))
            o = 100.0 * math.exp(0.04 * math.sin(2 * math.pi * (k - 1) / 288)
                                 + 1e-5 * ((k - 1) % 5000))
            rows.append([t, str(o), str(max(o, c) * 1.001), str(min(o, c) * 0.999),
                         str(c), "0"])
            t += M15
        return rows
    return fetch


class Main(unittest.TestCase):
    def test_refuses_end_past_fit_cutoff(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            rc = bt.main(["--end", "2026-09-18", "--cache", tmp], fetch=_wave_fetch(calls))
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])                       # refused before any fetch

    def test_end_to_end_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.md"
            rc = bt.main(["--symbols", "BTCUSDT", "--start", "2026-06-01",
                          "--end", "2026-08-01", "--cache", tmp, "--seeds", "3",
                          "--out", str(out)],
                         fetch=_wave_fetch(), now_ms=bt.FIT_CUTOFF_MS)
            self.assertEqual(rc, 0)
            text = out.read_text()
        self.assertIn("2026-06-01", text)
        self.assertIn("simulated", text.lower())          # never reads as real P&L
        for name, _ in bt.GRID:
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
