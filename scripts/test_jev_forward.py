#!/usr/bin/env python3
"""Tests for jev_forward (forward-return replay + rule-faithful sim). No network:
every kline fetch goes through an injected fake.

Run: .venv/bin/python scripts/test_jev_forward.py -v
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_forward as fw  # noqa: E402

T0 = 1_790_000_000_000 - (1_790_000_000_000 % 60_000)  # minute-aligned
MIN = 60_000


def _candles(n, start=T0, base=100.0, step=0.0):
    """n 1m candles: open = base + i*step, high = open + 1, low = open - 1."""
    out = []
    for i in range(n):
        o = base + i * step
        out.append([start + i * MIN, str(o), str(o + 1), str(o - 1), str(o), "0"])
    return out


class FakeFetch:
    """Serves /api/v3/klines pages from a fixed candle list; records calls."""

    def __init__(self, candles):
        self.candles = candles
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        q = dict(p.split("=") for p in url.split("?", 1)[1].split("&"))
        start, limit = int(q["startTime"]), int(q["limit"])
        end = int(q.get("endTime", 10 ** 15))
        rows = [c for c in self.candles if start <= c[0] <= end]
        return rows[:limit]


class KlinesTests(unittest.TestCase):
    def setUp(self):
        self.k = fw.Klines([[T0 + i * MIN, 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i]
                            for i in range(10)])

    def test_price_at_is_open_of_first_candle_at_or_after(self):
        self.assertEqual(self.k.price_at(T0), 100.0)
        self.assertEqual(self.k.price_at(T0 + 1), 101.0)        # next candle
        self.assertEqual(self.k.price_at(T0 + 3 * MIN), 103.0)

    def test_price_at_beyond_data_is_none(self):
        self.assertIsNone(self.k.price_at(T0 + 10 * MIN))

    def test_hilo_is_half_open(self):
        # candles 2,3,4 (open_time in [t2, t5)): highs 103..105, lows 101..103
        self.assertEqual(self.k.hilo(T0 + 2 * MIN, T0 + 5 * MIN), (105.0, 101.0))
        self.assertIsNone(self.k.hilo(T0 + 5 * MIN, T0 + 5 * MIN))


class FetchTests(unittest.TestCase):
    def test_paginates_until_end(self):
        fake = FakeFetch(_candles(2500))
        rows = fw.fetch_klines("BTCUSDT", T0, T0 + 2499 * MIN, fetch=fake)
        self.assertEqual(len(rows), 2500)
        self.assertEqual(len(fake.urls), 3)                     # 1000 + 1000 + 500
        self.assertTrue(fake.urls[0].startswith(fw.KLINE_URL))
        self.assertIn("symbol=BTCUSDT", fake.urls[0])
        self.assertEqual(rows[0], [T0, 100.0, 101.0, 99.0, 100.0])  # floats, 5 cols

    def test_stops_on_empty_page(self):
        fake = FakeFetch(_candles(5))
        rows = fw.fetch_klines("BTCUSDT", T0, T0 + 10_000 * MIN, fetch=fake)
        self.assertEqual(len(rows), 5)

    def test_cache_hit_skips_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeFetch(_candles(30))
            k1 = fw.load_klines("BTCUSDT", T0, T0 + 29 * MIN, tmp, fetch=fake)
            calls = len(fake.urls)
            k2 = fw.load_klines("BTCUSDT", T0, T0 + 29 * MIN, tmp, fetch=fake)
            self.assertEqual(len(fake.urls), calls)
            self.assertEqual(k1.price_at(T0 + 5 * MIN), k2.price_at(T0 + 5 * MIN))
            self.assertEqual(len(list(Path(tmp).glob("spot_BTCUSDT_1m_*.json"))), 1)


def _verdict(**kw):
    v = {"ok": True, "error": None, "pump_0_100": 70.0, "dump_0_100": 10.0,
         "phase": "breakout", "exhaustion_prob": 0.2, "whipsaw_prob": 0.3,
         "confidence": 0.9}
    v.update(kw)
    return v


def _row(ts, price, book="spot", symbol="BTCUSDT", regime="trend_up",
         vetoed_by=(), did=None, **vkw):
    return {"decision_id": did or f"d{ts}", "book": book, "symbol": symbol,
            "ts_ms": ts, "price": price, "regime": regime,
            "verdict": _verdict(**vkw), "vetoed_by": list(vetoed_by),
            "action": "skip" if vetoed_by else "enter"}


class LoadTests(unittest.TestCase):
    def test_load_decisions_normalizes_and_skips_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper_decisions.jsonl"
            good = {"decision_id": "a", "book": "perps", "symbol": "ETHUSDT",
                    "ts_ms": T0, "price": 2500.5, "regime": "chop",
                    "verdict": _verdict(), "vetoed_by": ["regime_chop"],
                    "action": "skip", "equity": 10000.0}
            path.write_text(json.dumps(good) + "\n{not json\n"
                            + json.dumps({"book": "spot"}) + "\n")
            rows = fw.load_decisions(path)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual((r["book"], r["symbol"], r["ts_ms"], r["price"]),
                             ("perps", "ETHUSDT", T0, 2500.5))
            self.assertEqual(r["vetoed_by"], ["regime_chop"])
            self.assertEqual(r["regime"], "chop")

    def test_load_v1_calls_rebuilds_verdicts_with_scorer_math(self):
        answers = {
            "pump": {"type": "score", "score": 1.5, "confidence": 0.8},
            "dump": {"type": "score", "score": 0.3, "confidence": 0.6},
            "phase": {"type": "choice", "choice": "breakout", "confidence": 0.7},
            "exhaustion": {"type": "noul", "noul": 0.4},
            "whipsaw": {"type": "noul", "noul": 0.5},
        }
        lines = [
            {"ts": T0 / 1000.0, "raw": {"answers": answers}, "error": None},
            {"ts": T0 / 1000.0 + 60, "decision_id": "m1",           # M0+: skip
             "raw": {"answers": answers}, "error": None},
            {"ts": T0 / 1000.0 + 120, "raw": {"answers": {"pump": answers["pump"]}},
             "error": None},                                          # partial: skip
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jev_decisions.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            rows = fw.load_v1_calls(path, symbol="BTCUSDT")
        self.assertEqual(len(rows), 1)
        v = rows[0]["verdict"]
        self.assertEqual(rows[0]["ts_ms"], T0)
        self.assertEqual((v["pump_0_100"], v["dump_0_100"]), (50.0, 10.0))
        self.assertEqual(v["confidence"], 0.6)                  # min(pump, dump, phase)
        self.assertEqual((v["phase"], v["whipsaw_prob"]), ("breakout", 0.5))
        self.assertIsNone(rows[0]["price"])                     # priced from klines


class ForwardTests(unittest.TestCase):
    def test_forward_returns_vs_logged_price(self):
        k = fw.Klines([[T0 + i * MIN, 100.0 + i, 0, 0, 0] for i in range(70)])
        rows = [_row(T0, 100.0)]
        fw.forward_returns(rows, {"BTCUSDT": k})
        self.assertAlmostEqual(rows[0]["fwd"]["15m"], 15.0)
        self.assertAlmostEqual(rows[0]["fwd"]["1h"], 60.0)
        self.assertIsNone(rows[0]["fwd"]["4h"])                 # past the data

    def test_missing_price_is_filled_from_klines(self):
        k = fw.Klines([[T0 + i * MIN, 200.0 + i, 0, 0, 0] for i in range(20)])
        rows = [dict(_row(T0, None))]
        fw.forward_returns(rows, {"BTCUSDT": k})
        self.assertEqual(rows[0]["price"], 200.0)
        self.assertAlmostEqual(rows[0]["fwd"]["15m"], 7.5)

    def test_unknown_symbol_gets_no_forward(self):
        rows = [_row(T0, 100.0, symbol="XYZUSDT")]
        fw.forward_returns(rows, {})
        self.assertEqual(rows[0]["fwd"], {h: None for h, _ in fw.HORIZONS})


class StatsTests(unittest.TestCase):
    def test_spearman_monotone_and_reversed(self):
        xs = list(range(20))
        self.assertAlmostEqual(fw.spearman(xs, [x * x for x in xs])[0], 1.0)
        self.assertAlmostEqual(fw.spearman(xs, [-x for x in xs])[0], -1.0)
        self.assertEqual(fw.spearman(xs, xs)[1], 20)

    def test_spearman_ties_use_average_ranks(self):
        rho, n = fw.spearman([1, 1, 2], [1, 2, 3], min_n=3)
        self.assertAlmostEqual(rho, 1.5 / 3 ** 0.5)
        self.assertEqual(n, 3)

    def test_spearman_drops_none_and_needs_min_n(self):
        self.assertIsNone(fw.spearman([1, 2, None], [1, None, 3], min_n=3))
        self.assertIsNone(fw.spearman([1, 1, 1, 1], [1, 2, 3, 4], min_n=3))  # no variance

    def test_decimate_keeps_first_per_symbol_bucket(self):
        b = T0 - T0 % (15 * MIN)  # bucket-aligned base
        rows = [_row(b, 1.0), _row(b + 5 * MIN, 1.0), _row(b + 15 * MIN, 1.0),
                _row(b + 5 * MIN, 1.0, symbol="ETHUSDT")]
        kept = fw.decimate(rows, bucket_ms=15 * MIN)
        self.assertEqual([(r["symbol"], r["ts_ms"]) for r in kept],
                         [("BTCUSDT", b), ("BTCUSDT", b + 15 * MIN),
                          ("ETHUSDT", b + 5 * MIN)])


def _with_fwd(row, **fwd):
    row["fwd"] = {h: fwd.get(h) for h, _ in fw.HORIZONS}
    return row


class GateTableTests(unittest.TestCase):
    def test_spot_counts_sole_and_long_direction(self):
        rows = [
            _with_fwd(_row(T0, 1.0, vetoed_by=["low_pump"]), **{"1h": 1.0}),
            _with_fwd(_row(T0 + MIN, 1.0, vetoed_by=["low_pump"]), **{"1h": -3.0}),
            _with_fwd(_row(T0 + 2 * MIN, 1.0, vetoed_by=["regime_chop", "low_pump"]),
                      **{"1h": -5.0}),
            _with_fwd(_row(T0 + 3 * MIN, 1.0), **{"1h": 9.0}),            # entered
            _with_fwd(_row(T0 + 4 * MIN, 1.0, book="perps",
                           vetoed_by=["low_pump"]), **{"1h": 7.0}),       # other book
        ]
        table = {g["gate"]: g for g in fw.gate_table(rows, "spot")}
        lp = table["low_pump"]
        self.assertEqual((lp["count"], lp["sole"]), (3, 2))
        self.assertAlmostEqual(lp["mean"]["1h"], -7.0 / 3)
        self.assertAlmostEqual(lp["sole_mean"]["1h"], -1.0)
        self.assertIsNone(lp["mean"]["15m"])                    # no data, no number
        self.assertEqual((table["regime_chop"]["count"], table["regime_chop"]["sole"]),
                         (1, 0))
        self.assertEqual([g["gate"] for g in fw.gate_table(rows, "spot")][0],
                         "low_pump")                            # by count desc

    def test_perps_direction_follows_stronger_signal(self):
        rows = [_with_fwd(_row(T0, 1.0, book="perps", vetoed_by=["low_confidence"],
                               pump_0_100=10.0, dump_0_100=80.0), **{"1h": -2.0}),
                _with_fwd(_row(T0 + MIN, 1.0, book="perps",
                               vetoed_by=["low_confidence"]), **{"1h": -2.0})]
        g = fw.gate_table(rows, "perps")[0]
        self.assertAlmostEqual(g["mean"]["1h"], 0.0)            # short +2, long -2

    def test_round_trip_cost_from_config(self):
        from jev_config import V2Config
        cfg = V2Config()
        self.assertAlmostEqual(fw.round_trip_cost_pct(cfg, "spot"), 0.30)
        self.assertAlmostEqual(fw.round_trip_cost_pct(cfg, "perps"), 0.20)


def _cfg(**sections):
    from jev_config import V2Config
    return fw.apply_variant(V2Config(), sections)


def _flat_klines(n=600, price=100.0, spikes=None):
    """n flat 1m candles (high/low +-0.1); spikes = {minute: (high, low)}."""
    spikes = spikes or {}
    rows = []
    for i in range(n):
        hi, lo = spikes.get(i, (price + 0.1, price - 0.1))
        rows.append([T0 + i * MIN, price, hi, lo, price])
    return fw.Klines(rows)


class SimulateTests(unittest.TestCase):
    def test_spot_entry_then_hysteresis_exit_with_costs(self):
        rows = [_row(T0, 100.0),                                       # enter
                _row(T0 + MIN, 101.0, dump_0_100=10.0, pump_0_100=10.0),
                _row(T0 + 2 * MIN, 102.0, dump_0_100=70.0, pump_0_100=10.0),
                _row(T0 + 3 * MIN, 110.0, dump_0_100=70.0, pump_0_100=10.0)]
        trades = fw.simulate(rows, _cfg(), "spot", {})
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["side"], t["path"], t["exit_ts"]),
                         ("long", "signal", T0 + 3 * MIN))
        ef, xf, fee = 100.0 * 1.0005, 110.0 * 0.9995, 0.001
        self.assertAlmostEqual(t["net_pct"], (xf / ef - 1 - fee - fee * xf / ef) * 100)

    def test_baseline_perps_never_shorts_a_distribution_dump(self):
        rows = [_row(T0 + i * MIN, 100.0, book="perps", regime="trend_down",
                     pump_0_100=10.0, dump_0_100=80.0, phase="distribution")
                for i in range(5)]
        self.assertEqual(fw.simulate(rows, _cfg(), "perps", {}), [])

    def test_tuned_short_stops_out_intrabar(self):
        cfg = _cfg(perps={"short_entry_phases": ("distribution",),
                          "short_max_whipsaw": 1.0})
        short = dict(book="perps", regime="trend_down", pump_0_100=10.0,
                     dump_0_100=80.0, phase="distribution", whipsaw_prob=0.9)
        rows = [_row(T0, 100.0, **short),
                _row(T0 + 3 * MIN, 100.0, **dict(short, dump_0_100=10.0))]
        k = _flat_klines(spikes={1: (103.0, 99.9)})             # wick through the stop
        trades = fw.simulate(rows, cfg, "perps", {"BTCUSDT": k})
        self.assertEqual(len(trades), 1)
        t = trades[0]
        ef = 100.0 * 0.9995
        stop = ef * 1.02
        xf = stop * 1.0005
        self.assertEqual((t["side"], t["path"]), ("short", "stop"))
        self.assertAlmostEqual(t["exit_fill"], xf)
        self.assertAlmostEqual(t["net_pct"],
                               (-(xf / ef - 1) - 0.0005 - 0.0005 * xf / ef) * 100)

    def test_max_hold_closes_at_cycle_price(self):
        rows = [_row(T0 + i * 10 * MIN, 100.0 + i, book="perps") for i in range(4)]
        k = _flat_klines(n=60, price=100.0)
        trades = fw.simulate(rows, _cfg(), "perps", {"BTCUSDT": k}, max_hold_min=20)
        self.assertEqual((trades[0]["path"], trades[0]["exit_ts"]),
                         ("time", T0 + 20 * MIN))

    def test_open_at_end_is_marked_without_exit_costs(self):
        rows = [_row(T0, 100.0), _row(T0 + MIN, 105.0, pump_0_100=10.0)]
        t = fw.simulate(rows, _cfg(), "spot", {})[0]
        ef = 100.0 * 1.0005
        self.assertEqual(t["path"], "open")
        self.assertAlmostEqual(t["net_pct"], (105.0 / ef - 1 - 0.001) * 100)

    def test_cooldown_comes_from_the_real_gates(self):
        # enter, exit 3 cycles later, then a fresh entry signal inside the 900s
        # cooldown must be refused by decide() itself
        rows = [_row(T0, 100.0),
                _row(T0 + MIN, 100.0, dump_0_100=80.0, pump_0_100=10.0),
                _row(T0 + 2 * MIN, 100.0, dump_0_100=80.0, pump_0_100=10.0),
                _row(T0 + 3 * MIN, 100.0, dump_0_100=80.0, pump_0_100=10.0),
                _row(T0 + 4 * MIN, 100.0),                       # cooldown
                _row(T0 + 16 * MIN, 100.0)]                      # 960s: enters
        trades = fw.simulate(rows, _cfg(), "spot", {})
        self.assertEqual([t["entry_ts"] for t in trades], [T0, T0 + 16 * MIN])


class SummaryTests(unittest.TestCase):
    def test_summarize(self):
        trades = [{"net_pct": 2.0, "side": "long", "path": "signal", "hold_min": 10},
                  {"net_pct": -1.0, "side": "short", "path": "stop", "hold_min": 30},
                  {"net_pct": -1.0, "side": "long", "path": "signal", "hold_min": 20}]
        s = fw.summarize(trades)
        self.assertEqual((s["n"], s["longs"], s["shorts"]), (3, 2, 1))
        self.assertAlmostEqual(s["pf"], 1.0)
        self.assertAlmostEqual(s["win_rate"], 1 / 3)
        self.assertAlmostEqual(s["sum_pct"], 0.0)
        self.assertEqual(s["median_hold_min"], 20)
        self.assertEqual(s["paths"], {"signal": 2, "stop": 1})
        self.assertEqual(fw.summarize([])["n"], 0)

    def test_split_halves_by_time(self):
        rows = [_row(T0 + i * MIN, 1.0) for i in range(6)]
        h1, h2 = fw.split_halves(rows)
        self.assertEqual(([r["ts_ms"] for r in h1], len(h2)),
                         ([T0, T0 + MIN, T0 + 2 * MIN], 3))

    def test_apply_variant_overrides_only_given_fields(self):
        from jev_config import V2Config
        base = V2Config()
        cfg = fw.apply_variant(base, {"perps": {"short_max_whipsaw": 1.0}})
        self.assertEqual(cfg.perps.short_max_whipsaw, 1.0)
        self.assertEqual(cfg.spot, base.spot)
        self.assertEqual(cfg.perps.entry_min_pump, base.perps.entry_min_pump)


class ReportTests(unittest.TestCase):
    def _decisions(self, tmp):
        path = Path(tmp) / "paper_decisions.jsonl"
        lines = []
        for i in range(40):
            ts = T0 + i * 5 * MIN
            for book in ("spot", "perps"):
                vetoed = ["regime_chop"] if i % 2 else []
                lines.append(json.dumps({
                    "decision_id": f"d{i}", "book": book, "symbol": "BTCUSDT",
                    "ts_ms": ts, "price": 100.0 + i * 0.5,
                    "regime": "chop" if i % 2 else "trend_up",
                    "verdict": _verdict(pump_0_100=40.0 + i, dump_0_100=60.0 - i),
                    "vetoed_by": vetoed, "action": "skip" if vetoed else "enter"}))
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_main_writes_report_and_caches_klines(self):
        fake = FakeFetch(_candles(2000, step=0.05))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.md"
            cache = Path(tmp) / "klines"
            argv = ["--decisions", str(self._decisions(tmp)), "--cache", str(cache),
                    "--out", str(out), "--now-ms", str(T0 + 3000 * MIN)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fw.main(argv, fetch=fake), 0)
            text = out.read_text()
            for heading in ("## Window", "## Forward returns by regime",
                            "## Per-gate attribution", "## Information coefficient",
                            "## Rule-faithful sim"):
                self.assertIn(heading, text)
            self.assertIn("| regime_chop |", text)
            self.assertIn("| baseline |", text)
            self.assertIn("round trip 0.30%", text)
            self.assertTrue(list(cache.glob("spot_BTCUSDT_1m_*.json")))
            n_calls = len(fake.urls)
            with contextlib.redirect_stdout(io.StringIO()):
                fw.main(argv, fetch=fake)
            self.assertEqual(len(fake.urls), n_calls)            # cache hit

    def test_kline_range_never_reaches_past_now(self):
        rows = [_row(T0, 100.0), _row(T0 + 10 * MIN, 100.0)]
        start, end = fw.kline_range(rows, now_ms=T0 + 60 * MIN)
        self.assertEqual(start, T0 - MIN)
        self.assertLessEqual(end, T0 + 60 * MIN)
        _, end_full = fw.kline_range(rows, now_ms=T0 + 10 ** 9)
        self.assertEqual(end_full, T0 + 10 * MIN + (1440 + 5) * MIN)


if __name__ == "__main__":
    unittest.main()
