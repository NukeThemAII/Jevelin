#!/usr/bin/env python3
"""Tests for jev_radar (CoinGecko discovery radar) — network mocked, temp dirs only.

Run: .venv/bin/python scripts/test_jev_radar.py -v
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_radar
import jev_store

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
BASES = {"BTC", "ETH", "SOL", "WIF", "NEWCOIN", "MEME"}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _coin(**over):
    coin = {"id": "somecoin", "symbol": "NEWCOIN", "trending_rank": 1,
            "price_usd": 1.23, "volume_24h": 10_000_000.0, "mcap": 50_000_000.0,
            "ath_date": _iso(NOW - timedelta(days=120))}
    coin.update(over)
    return coin


class FilterTests(unittest.TestCase):
    def _eval(self, coin, bases=None):
        return jev_radar.evaluate_coin(coin, BASES if bases is None else bases, NOW)

    def test_passing_coin(self):
        passed, rejections = self._eval(_coin())
        self.assertTrue(passed)
        self.assertEqual(rejections, [])

    def test_reject_not_binance_spot_usdt(self):
        passed, rejections = self._eval(_coin(symbol="MOONCOIN"))
        self.assertFalse(passed)
        self.assertIn("not_binance_spot_usdt", rejections)

    def test_reject_low_volume_boundary(self):
        self.assertIn("low_volume", self._eval(_coin(volume_24h=4_999_999.99))[1])
        self.assertNotIn("low_volume", self._eval(_coin(volume_24h=5_000_000.0))[1])

    def test_reject_low_mcap_boundary(self):
        self.assertIn("low_mcap", self._eval(_coin(mcap=19_999_999.99))[1])
        self.assertNotIn("low_mcap", self._eval(_coin(mcap=20_000_000.0))[1])

    def test_missing_data_fails_closed(self):
        _, rejections = self._eval(_coin(volume_24h=None, mcap=None))
        self.assertIn("low_volume", rejections)
        self.assertIn("low_mcap", rejections)

    def test_ath_age_heuristic_boundary_29_vs_31_days(self):
        age_29 = _iso(NOW - timedelta(days=29))
        age_31 = _iso(NOW - timedelta(days=31))
        self.assertIn("too_new", self._eval(_coin(ath_date=age_29))[1])
        self.assertNotIn("too_new", self._eval(_coin(ath_date=age_31))[1])

    def test_missing_ath_date_fails_closed(self):
        self.assertIn("too_new", self._eval(_coin(ath_date=None))[1])

    def test_stablecoin_deny_list(self):
        deny = ("USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "BUSD",
                "WBTC", "WETH", "STETH")
        for sym in deny + ("usdt", "UsDe", "wbtc", "steth"):
            bases = BASES | {sym.upper()}
            passed, rejections = self._eval(_coin(symbol=sym), bases=bases)
            self.assertFalse(passed, sym)
            self.assertIn("stablecoin", rejections, sym)

    def test_core_pair_exclusion(self):
        for sym in ("BTC", "ETH", "SOL", "btc"):
            passed, rejections = self._eval(_coin(symbol=sym))
            self.assertFalse(passed, sym)
            self.assertIn("core_pair", rejections, sym)

    def test_all_rejection_reasons_recorded_in_filter_order(self):
        _, rejections = self._eval(_coin(
            symbol="MOONCOIN", volume_24h=1.0, mcap=1.0,
            ath_date=_iso(NOW - timedelta(days=1))))
        self.assertEqual(rejections, ["not_binance_spot_usdt", "low_volume",
                                      "low_mcap", "too_new"])

class RankingTests(unittest.TestCase):
    def test_rank_survivors_by_volume_desc(self):
        def row(vol, rejections=None):
            return {"coin": _coin(volume_24h=vol), "rejections": rejections or []}
        rows = [row(5e6), row(9e7), row(2e7), row(7e6), row(1e7),
                row(6e6), row(8e7), row(3e7, ["too_new"])]  # last one rejected
        ranked = jev_radar.rank_survivors(rows)
        self.assertEqual([r["rank"] for r in ranked], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([r["coin"]["volume_24h"] for r in ranked],
                         [9e7, 8e7, 2e7, 1e7, 7e6, 6e6, 5e6])
        self.assertEqual([r["coin"]["volume_24h"] for r in ranked[:5]],
                         [9e7, 8e7, 2e7, 1e7, 7e6])  # top 5 = candidates

    def test_histogram_counts_each_reason(self):
        rows = [
            {"coin": _coin(), "rejections": []},
            {"coin": _coin(), "rejections": ["too_new"]},
            {"coin": _coin(), "rejections": ["too_new", "low_mcap"]},
        ]
        self.assertEqual(jev_radar.rejection_histogram(rows),
                         {"too_new": 2, "low_mcap": 1})


def _fake_get(url, params=None, timeout=None):
    if url.endswith("/search/trending"):
        return {"coins": [{"item": {"id": "dogwifcoin", "symbol": "wif",
                                    "market_cap_rank": 42}},
                          {"item": {"id": "memecoin", "symbol": "meme",
                                    "market_cap_rank": None}}]}
    if "/coins/markets" in url:
        row = {"id": "dogwifcoin", "symbol": "wif", "name": "dog wif hat",
               "current_price": 1.2, "total_volume": 9.9e7,
               "market_cap": 1.2e9, "ath_date": "2024-03-31T00:00:00.000Z"}
        row2 = {"id": "memecoin", "symbol": "meme", "name": "meme coin",
                "current_price": 0.01, "total_volume": 3.0e6,
                "market_cap": 5.0e7, "ath_date": "2024-01-01T00:00:00.000Z"}
        ids = (params or {}).get("ids")
        if ids:
            wanted = set(ids.split(","))
            return [r for r in (row, row2) if r["id"] in wanted]
        return [row]  # top-100-by-volume page: meme is outside it
    raise AssertionError("unexpected url: " + url)


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)

    def _run(self, **over):
        args = dict(runtime_dir=str(self.runtime), get=_fake_get,
                    loader=lambda: sorted(BASES), now=NOW, run_id="test-run")
        args.update(over)
        return jev_radar.run_once(**args)

    def test_run_once_logs_every_coin_and_upserts(self):
        report = self._run()
        self.assertEqual(report["universe"], 2)
        lines = (self.runtime / "radar_candidates.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        rows = [json.loads(line) for line in lines]
        by_id = {r["coingecko_id"]: r for r in rows}
        self.assertTrue(by_id["dogwifcoin"]["passed"])
        self.assertEqual(by_id["dogwifcoin"]["rejections"], [])
        self.assertFalse(by_id["memecoin"]["passed"])
        self.assertEqual(by_id["memecoin"]["rejections"], ["low_volume"])
        conn = jev_store.connect(str(self.runtime / "jevelin.db"))
        self.addCleanup(conn.close)
        stored = conn.execute("SELECT * FROM radar_candidates").fetchall()
        self.assertEqual(len(stored), 2)

    def test_report_candidates_and_histogram(self):
        report = self._run()
        self.assertEqual([c["symbol"] for c in report["candidates"]], ["WIF"])
        self.assertEqual(report["candidates"][0]["rank"], 1)
        self.assertEqual(report["histogram"], {"low_volume": 1})
        self.assertEqual(report["passed"], 1)

    def test_api_error_fails_open_without_partial_state(self):
        def bad_get(url, params=None, timeout=None):
            raise RuntimeError("connection reset")
        with self.assertRaises(jev_radar.RadarError):
            self._run(get=bad_get)
        self.assertFalse((self.runtime / "radar_candidates.jsonl").exists())
        self.assertFalse((self.runtime / "jevelin.db").exists())
        self.assertFalse((self.runtime / "binance_symbols.json").exists())

    def test_binance_symbol_cache_24h_ttl(self):
        calls = []

        def loader():
            calls.append(1)
            return sorted(BASES)

        self._run(loader=loader)
        self._run(run_id="test-run-2", loader=loader)
        self.assertEqual(len(calls), 1)  # second run served from cache
        self._run(run_id="test-run-3", loader=loader,
                  now=NOW + timedelta(hours=25))
        self.assertEqual(len(calls), 2)  # TTL expired -> refetch
        cache = json.loads((self.runtime / "binance_symbols.json").read_text())
        self.assertIn("WIF", cache["symbols"])


class PrintReportTests(unittest.TestCase):
    def test_prints_candidates_and_rejection_histogram(self):
        report = {
            "run_id": "r", "universe": 2, "passed": 1,
            "candidates": [{"rank": 1, "symbol": "WIF", "volume_24h": 9.9e7,
                            "mcap": 1.2e9, "ath_age_days": 545.0}],
            "histogram": {"low_volume": 1},
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            jev_radar.print_report(report)
        out = buf.getvalue()
        self.assertIn("candidates", out)
        self.assertIn("WIF", out)
        self.assertIn("rejection histogram", out)
        self.assertIn("low_volume", out)


if __name__ == "__main__":
    unittest.main()