#!/usr/bin/env python3
"""Tests for jev_scout (M6 CoinGecko discovery scout) — fully offline.

All HTTP/ccxt inputs are fixtures (FakeFetcher / Fixture / FakeExchange); the
network is NEVER touched. Covers the filter matrix + boundaries, deterministic
ranking + tie-break, the max_pairs cap, raw-response caching + --replay
byte-identical reproducibility, store persistence, fault injection (429/5xx/
malformed JSON -> keep last good list, never crash the trading loop), the
opt-in supervisor scout universe (fresh/stale/empty/fallback/retention) and the
scout config section.

M6+ additions: Pool B market-gainers seed pool (merge + dedup + seed tags,
exactly one extra GET, HTTP-failure fail-safe), the honest junk-rejection
metric (``over_cap`` excluded - a capped survivor is NOT junk) and the hard
filter-correctness gate (asserted at selection; boundary + NaN fault
fixtures), plus byte-for-byte M6 compatibility when the gainers pool is
off/absent.

Run: .venv/bin/python scripts/test_jev_scout.py -v
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_store  # noqa: E402
import jev_scout  # noqa: E402
from jev_config import ConfigError, ScoutConfig, V2Config, load_config  # noqa: E402
from jev_supervisor import BookPair, Supervisor  # noqa: E402
from jevelin_supervisor import (  # noqa: E402
    ScoutSupervisor,
    parse_args as supervisor_parse_args,
    resolve_initial_universe,
    resolve_scout_universe,
)

SHIPPED_YAML = Path(__file__).resolve().parent.parent / "config" / "v2.yaml"
PASS_TS = 1_800_000_000.0            # fixed pass timestamp (deterministic age math)
DAY_MS = 86_400_000

# M6 (commit cda71a5) reference bytes on poolb_fixture() with the gainers pool
# OFF — produced by running the M6 module over these exact fixtures, never
# hand-edited. The gainers_enabled:false / Pool-B-absent path must reproduce
# them byte-for-byte.
M6_DISABLED_CANDIDATES_JSON = (
    '[{"binance_quote_volume_usd":400000000.0,"candidate_score":0.5,'
    '"cg_symbol":"btc","cg_volume_24h_usd":900000000.0,'
    '"change_rank_norm":0.0,"coingecko_id":"bitcoin",'
    '"listing_age_days":120.0,"market_cap_usd":1000000.0,"name":"Bitcoin",'
    '"price_change_24h_pct":2.0,"rank":1,"reject_reason":null,'
    '"selected":true,"symbol":"BTCUSDT","vol_rank_norm":1.0},'
    '{"binance_quote_volume_usd":300000000.0,"candidate_score":0.5,'
    '"cg_symbol":"eth","cg_volume_24h_usd":600000000.0,'
    '"change_rank_norm":1.0,"coingecko_id":"ethereum",'
    '"listing_age_days":120.0,"market_cap_usd":1000000.0,"name":"Ethereum",'
    '"price_change_24h_pct":-9.0,"rank":2,"reject_reason":null,'
    '"selected":true,"symbol":"ETHUSDT","vol_rank_norm":0.0}]')
M6_DISABLED_RANKED_JSON = M6_DISABLED_CANDIDATES_JSON  # identical bytes here
M6_DISABLED_FUNNEL_JSON = (
    '{"filtered":2,"mapped":2,"ranked":2,"reject_reasons":{},'
    '"rejected":0,"rejected_junk_rate":0.0,"trending":2}')
M6_DISABLED_REPORT = (
    'scout pass 20270115T080000Z (2027-01-15T08:00:00Z)\n'
    'coin         symbol     status   rank reject_reason             vol24h$'
    '   chg24h%   age_d    score\n'
    'btc          BTCUSDT    selected    1 -                       900000000'
    '      2.00   120.0 0.500000\n'
    'eth          ETHUSDT    selected    2 -                       600000000'
    '     -9.00   120.0 0.500000\n'
    'funnel: trending=2 mapped=2 filtered=2 ranked=2\n'
    'rejected-junk rate: 0/2 = 0.0% (rejects / trending seeds)')

# -- fixtures (raw CoinGecko/Binance inputs; never live) ----------------------

class Fixture:
    """Assembles the exact raw-input dict evaluate_pass() consumes."""

    def __init__(self):
        self.coins = []          # (cid, cg_symbol, name) Pool A trending seeds
        self.gainers = []        # Pool B gainers seed cids, in scan order
        self.markets = {}        # cid -> coins/markets row
        self.tickers = {}        # "BASE/USDT" -> {"quoteVolume": ...}
        self.ohlcv = {}          # "BASEUSDT" -> first candle ts (ms)

    def seed(self, cid, sym, name=None):
        self.coins.append((cid, sym, name if name is not None else cid.title()))
        return self

    def market(self, cid, sym, volume, change, mcap=1_000_000.0, name=None):
        self.markets[cid] = {"id": cid, "symbol": sym,
                             "name": name if name is not None else cid.title(),
                             "total_volume": volume,
                             "price_change_percentage_24h": change,
                             "market_cap": mcap}
        return self

    def gainer(self, cid, sym, volume, change, mcap=1_000_000.0, name=None):
        """Pool B (market-gainers scan) row, appended in scan order."""
        self.gainers.append(cid)
        return self.market(cid, sym, volume, change, mcap=mcap, name=name)

    def listed(self, sym, quote_volume=10_000_000.0, age_days=60.0,
               pass_ts=PASS_TS):
        pair = f"{sym.upper()}/USDT"
        self.tickers[pair] = {"quoteVolume": quote_volume}
        if age_days is not None:
            self.ohlcv[f"{sym.upper()}USDT"] = int(pass_ts * 1000
                                                   - age_days * DAY_MS)
        return self

    def raws(self):
        trending = json.dumps({"coins": [
            {"item": {"id": cid, "symbol": sym, "name": name}}
            for cid, sym, name in self.coins]})
        gainers = json.dumps([self.markets[cid] for cid in self.gainers])
        return {
            "search_trending": trending,
            "coins_markets": json.dumps(list(self.markets.values())),
            "coins_markets_gainers": gainers,
            "binance_tickers": json.dumps(self.tickers),
            "binance_ohlcv": {pair: json.dumps(
                [[ts + i * DAY_MS, 1.0, 1.0, 1.0, 1.0, 1.0] for i in range(2)])
                for pair, ts in self.ohlcv.items()},
        }

def standard_fixture():
    """7 trending seeds -> 4 survivors; hand-computed scores.

    junkcoin -> trending_only (no markets row), ghostcoin -> not_binance_listed,
    newcoin -> young_listing (listed, but no daily candles to prove age).
    PEPE quote volume is exactly the 5M floor (boundary: passes).
    """
    f = Fixture()
    for cid, sym in [("bitcoin", "btc"), ("ethereum", "eth"), ("solana", "sol"),
                     ("pepe", "pepe"), ("junkcoin", "junk"),
                     ("ghostcoin", "ghost"), ("newcoin", "new")]:
        f.seed(cid, sym)
    f.market("bitcoin", "btc", 900_000_000.0, 2.0)
    f.market("ethereum", "eth", 600_000_000.0, -9.0)
    f.market("solana", "sol", 300_000_000.0, 5.0)
    f.market("pepe", "pepe", 100_000_000.0, 12.0)
    f.market("ghostcoin", "ghost", 50_000_000.0, 4.0)
    f.market("newcoin", "new", 50_000_000.0, 4.0)
    f.listed("BTC", 400_000_000.0, 120.0)
    f.listed("ETH", 300_000_000.0, 120.0)
    f.listed("SOL", 20_000_000.0, 120.0)
    f.listed("PEPE", 5_000_000.0, 120.0)          # boundary volume passes
    f.listed("NEW", 10_000_000.0, age_days=None)  # no candles -> young_listing
    return f


def poolb_fixture():
    """Pool A (2 trending) + Pool B (5 market-gainers, junk-heavy) -> 6 unique.

    bitcoin rides BOTH pools (dedup -> one candidate, seed=trending+gainers).
    Gainers-only junk: mooncoin -> young_listing, ghostcoin ->
    not_binance_listed, pepecoin -> low_volume. Solana is the one clean
    gainers survivor. Hand-computed funnel + junk metric in the metric tests.
    """
    f = Fixture()
    f.seed("bitcoin", "btc").seed("ethereum", "eth")
    f.market("bitcoin", "btc", 900_000_000.0, 2.0)
    f.market("ethereum", "eth", 600_000_000.0, -9.0)
    f.gainer("bitcoin", "btc", 900_000_000.0, 2.0)      # in BOTH pools
    f.gainer("mooncoin", "moon", 50_000_000.0, 88.0)    # -> young_listing
    f.gainer("ghostcoin", "ghost", 50_000_000.0, 77.0)  # -> not_binance_listed
    f.gainer("pepecoin", "pepe", 50_000_000.0, 66.0)    # -> low_volume
    f.gainer("solana", "sol", 300_000_000.0, 55.0)      # clean survivor
    f.listed("BTC", 400_000_000.0, 120.0)
    f.listed("ETH", 300_000_000.0, 120.0)
    f.listed("SOL", 20_000_000.0, 120.0)
    f.listed("MOON", 10_000_000.0, age_days=10.0)       # young listing
    f.listed("PEPE", 4_000_000.0, 120.0)                # below the 5M floor
    return f


def by_symbol(candidates):
    return {c["symbol"] or c["cg_symbol"]: c for c in candidates}


class FakeFetcher:
    """fetcher(url, params) -> response text; url-substring keyed."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def __call__(self, url, params=None):
        self.calls.append((url, params))
        for key, val in self.responses.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                if callable(val):        # param-aware routing (two /coins/markets)
                    return val(params)
                return val
        raise AssertionError(f"unexpected url {url}")


class FakeExchange:
    """ccxt duck-type: fetch_tickers() + fetch_ohlcv()."""

    def __init__(self, tickers=None, ohlcv=None):
        self.tickers = dict(tickers or {})
        self.ohlcv = dict(ohlcv or {})
        self.calls = []

    def fetch_tickers(self, symbols=None):
        self.calls.append(("tickers",))
        return {k: dict(v) for k, v in self.tickers.items()}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append(("ohlcv", symbol, timeframe))
        return [list(c) for c in self.ohlcv.get(symbol, [])]


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class FakeSession:
    """session.get duck-type for http_get retry tests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeBook:
    def __init__(self, position=None):
        self.position = position

def ccxt_pair(bsym):
    """'BTCUSDT' -> 'BTC/USDT' (the ccxt unified symbol fetch_ohlcv takes)."""
    return f"{bsym[:-4]}/USDT" if bsym.endswith("USDT") else bsym


class MarketRouter:
    """Routes the two /coins/markets GETs: Pool A (ids=) vs Pool B (order=)."""

    def __init__(self, pool_a, pool_b):
        self.pool_a = pool_a
        self.pool_b = pool_b          # response text or Exception to raise

    def __call__(self, params):
        target = self.pool_b if (params and "order" in params) else self.pool_a
        if isinstance(target, Exception):
            raise target
        return target


def fixture_fetcher(f):
    raws = f.raws()
    return FakeFetcher({
        "search/trending": raws["search_trending"],
        "coins/markets": MarketRouter(raws["coins_markets"],
                                      raws["coins_markets_gainers"]),
    }), FakeExchange(
        tickers=json.loads(raws["binance_tickers"]),
        ohlcv={ccxt_pair(pair): json.loads(text)
               for pair, text in raws["binance_ohlcv"].items()})


def run_pass(f, now=PASS_TS, cfg=None, cache_dir=None, conn=None):
    fetcher, exchange = fixture_fetcher(f)
    return jev_scout.run_scout_pass(
        cfg or ScoutConfig(), fetcher=fetcher, exchange=exchange, now=now,
        cache_dir=cache_dir, conn=conn, quiet=True)


def evaluate(f, cfg=None, pass_ts=PASS_TS):
    return jev_scout.evaluate_pass(f.raws(), pass_ts, cfg or ScoutConfig())


# -- filter matrix: every reject reason fires exactly when it should ----------

class FilterMatrixTests(unittest.TestCase):
    def one(self, f):
        return evaluate(f)["candidates"][0]

    def test_trending_only_when_no_market_row(self):
        f = Fixture().seed("ghostcoin", "ghost")
        c = self.one(f)
        self.assertEqual(c["reject_reason"], "trending_only")
        self.assertIsNone(c["symbol"])

    def test_trending_only_when_symbol_missing(self):
        f = Fixture()
        f.coins.append(("mystery", None, "Mystery"))
        self.assertEqual(self.one(f)["reject_reason"], "trending_only")

    def test_trending_only_when_price_change_missing(self):
        f = Fixture().seed("oddcoin", "odd")
        f.markets["oddcoin"] = {"id": "oddcoin", "symbol": "odd",
                                "total_volume": 10_000_000.0,
                                "price_change_percentage_24h": None}
        self.assertEqual(self.one(f)["reject_reason"], "trending_only")

    def test_not_binance_listed(self):
        f = Fixture().seed("ghostcoin", "ghost")
        f.market("ghostcoin", "ghost", 50_000_000.0, 4.0)
        c = self.one(f)
        self.assertEqual(c["reject_reason"], "not_binance_listed")
        self.assertEqual(c["symbol"], "GHOSTUSDT")

    def test_low_volume_rejects_below_floor(self):
        f = Fixture().seed("mooncoin", "moon")
        f.market("mooncoin", "moon", 50_000_000.0, 4.0)
        f.listed("MOON", 4_999_999.0, 120.0)
        self.assertEqual(self.one(f)["reject_reason"], "low_volume")

    def test_volume_boundary_exactly_floor_passes(self):
        f = Fixture().seed("mooncoin", "moon")
        f.market("mooncoin", "moon", 50_000_000.0, 4.0)
        f.listed("MOON", 5_000_000.0, 120.0)
        out = evaluate(f)
        self.assertEqual(out["candidates"][0]["reject_reason"], None)
        self.assertEqual([c["symbol"] for c in out["ranked"]], ["MOONUSDT"])

    def test_young_listing_rejects_below_age(self):
        f = Fixture().seed("newcoin", "new")
        f.market("newcoin", "new", 50_000_000.0, 4.0)
        f.listed("NEW", 10_000_000.0, age_days=29.999)
        self.assertEqual(self.one(f)["reject_reason"], "young_listing")

    def test_age_boundary_exactly_30_days_passes(self):
        f = Fixture().seed("newcoin", "new")
        f.market("newcoin", "new", 50_000_000.0, 4.0)
        f.listed("NEW", 10_000_000.0, age_days=30.0)
        out = evaluate(f)
        self.assertIsNone(out["candidates"][0]["reject_reason"])
        self.assertAlmostEqual(out["candidates"][0]["listing_age_days"],
                               30.0, places=6)

    def test_missing_ohlcv_is_young_listing(self):
        f = Fixture().seed("newcoin", "new")
        f.market("newcoin", "new", 50_000_000.0, 4.0)
        f.listed("NEW", 10_000_000.0, age_days=None)  # no candles at all
        self.assertEqual(self.one(f)["reject_reason"], "young_listing")

    def test_first_failing_filter_wins(self):
        # fails BOTH volume and age -> exactly one machine-readable reason
        f = Fixture().seed("mooncoin", "moon")
        f.market("mooncoin", "moon", 50_000_000.0, 4.0)
        f.listed("MOON", 100.0, age_days=1.0)
        self.assertEqual(self.one(f)["reject_reason"], "low_volume")

    def test_every_reject_reason_is_machine_readable(self):
        out = evaluate(standard_fixture())
        for c in out["candidates"]:
            if not c["selected"]:
                self.assertIn(c["reject_reason"], jev_scout.REJECT_REASONS)

# -- ranking: deterministic, hand-computed, tie-broken by symbol --------------

class RankingTests(unittest.TestCase):
    def test_rank_norms_and_scores_hand_computed(self):
        out = evaluate(standard_fixture())
        cand = by_symbol(out["candidates"])
        # vol rank (asc): pepe 0, sol 1/3, eth 2/3, btc 1
        # |change| rank (asc): btc 0, sol 1/3, eth 2/3, pepe 1
        self.assertAlmostEqual(cand["BTCUSDT"]["vol_rank_norm"], 1.0, places=6)
        self.assertAlmostEqual(cand["BTCUSDT"]["change_rank_norm"], 0.0,
                               places=6)
        self.assertAlmostEqual(cand["BTCUSDT"]["candidate_score"], 0.5, places=6)
        self.assertAlmostEqual(cand["ETHUSDT"]["candidate_score"],
                               0.666667, places=6)
        self.assertAlmostEqual(cand["SOLUSDT"]["candidate_score"],
                               0.333333, places=6)
        self.assertAlmostEqual(cand["PEPEUSDT"]["candidate_score"], 0.5,
                               places=6)

    def test_ranked_order_with_tie_break(self):
        # btc and pepe tie at 0.5 -> lexicographic symbol order decides
        out = evaluate(standard_fixture())
        self.assertEqual([c["symbol"] for c in out["ranked"]],
                         ["ETHUSDT", "BTCUSDT", "PEPEUSDT", "SOLUSDT"])
        self.assertEqual([c["rank"] for c in out["ranked"]], [1, 2, 3, 4])

    def test_exact_tie_broken_lexicographically(self):
        f = Fixture()
        f.seed("aaa", "aaa").seed("bbb", "bbb")
        f.market("aaa", "aaa", 100.0, 5.0)   # vol low, change high
        f.market("bbb", "bbb", 200.0, 1.0)   # vol high, change low
        f.listed("AAA", 10_000_000.0, 60.0).listed("BBB", 10_000_000.0, 60.0)
        out = evaluate(f)                    # both score exactly 0.5
        self.assertEqual([c["symbol"] for c in out["ranked"]],
                         ["AAAUSDT", "BBBUSDT"])

    def test_ranking_deterministic_across_runs(self):
        a = evaluate(standard_fixture())
        b = evaluate(standard_fixture())
        self.assertEqual(jev_scout.ranked_json_text(a["ranked"]),
                         jev_scout.ranked_json_text(b["ranked"]))
        self.assertEqual(jev_scout.candidates_json_text(a["candidates"]),
                         jev_scout.candidates_json_text(b["candidates"]))

    def test_single_survivor_scores_one(self):
        f = Fixture().seed("mooncoin", "moon")
        f.market("mooncoin", "moon", 50_000_000.0, 4.0)
        f.listed("MOON", 10_000_000.0, 60.0)
        out = evaluate(f)
        self.assertEqual(out["ranked"][0]["candidate_score"], 1.0)
        self.assertEqual(out["ranked"][0]["vol_rank_norm"], 1.0)


class CapTests(unittest.TestCase):
    def seven_valid(self):
        f = Fixture()
        vols = [900e6, 700e6, 500e6, 300e6, 100e6, 50e6, 10e6]
        chgs = [2.0, -4.0, 6.0, -8.0, 10.0, -12.0, 14.0]
        for i in range(7):
            sym = f"coin{i}"
            f.seed(f"coin{i}", sym)
            f.market(f"coin{i}", sym, vols[i], chgs[i])
            f.listed(sym.upper(), 10_000_000.0, 60.0)
        return f

    def test_cap_at_max_pairs(self):
        out = evaluate(self.seven_valid())
        self.assertEqual(len(out["ranked"]), 5)          # max_pairs default 5
        self.assertEqual([c["rank"] for c in out["ranked"]],
                         [1, 2, 3, 4, 5])

    def test_capped_survivors_kept_with_reason(self):
        out = evaluate(self.seven_valid())
        capped = [c for c in out["candidates"]
                  if c["reject_reason"] == "over_cap"]
        self.assertEqual(len(capped), 2)
        for c in capped:
            self.assertFalse(c["selected"])
            self.assertIsNotNone(c["candidate_score"])   # qualified, just capped

    def test_rejects_retained_with_reasons(self):
        out = evaluate(standard_fixture())
        reasons = {c["cg_symbol"]: c["reject_reason"] for c in out["candidates"]}
        self.assertEqual(reasons["junk"], "trending_only")
        self.assertEqual(reasons["ghost"], "not_binance_listed")
        self.assertEqual(reasons["new"], "young_listing")


class SeedPoolMergeTests(unittest.TestCase):
    """M6+: Pool A u Pool B, deduped by CoinGecko id, tagged with ``seed``."""

    def test_pool_a_and_pool_b_union_deduped_by_coingecko_id(self):
        out = evaluate(poolb_fixture())
        ids = [c["coingecko_id"] for c in out["candidates"]]
        self.assertEqual(ids, ["bitcoin", "ethereum", "mooncoin",
                               "ghostcoin", "pepecoin", "solana"])
        self.assertEqual(len(ids), len(set(ids)))   # bitcoin counts ONCE

    def test_seed_tags_mark_pool_membership(self):
        tags = {c["coingecko_id"]: c["seed"]
                for c in evaluate(poolb_fixture())["candidates"]}
        self.assertEqual(tags["bitcoin"], "trending+gainers")
        self.assertEqual(tags["ethereum"], "trending")
        self.assertEqual(tags["mooncoin"], "gainers")

    def test_gainers_only_coins_enter_the_funnel(self):
        out = evaluate(poolb_fixture())
        by = by_symbol(out["candidates"])
        self.assertEqual(by["MOONUSDT"]["reject_reason"], "young_listing")
        self.assertEqual(by["GHOSTUSDT"]["reject_reason"], "not_binance_listed")
        self.assertEqual(by["PEPEUSDT"]["reject_reason"], "low_volume")
        # the 3 survivors all score exactly 0.5 (vol rank mirrors |change|
        # rank) -> the lexicographic tie-break decides: BTC, ETH, SOL
        self.assertEqual([c["symbol"] for c in out["ranked"]],
                         ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    def test_pool_b_fetch_is_exactly_one_extra_request(self):
        f = poolb_fixture()
        fetcher, exchange = fixture_fetcher(f)
        with tempfile.TemporaryDirectory() as tmp:
            record = jev_scout.run_scout_pass(
                ScoutConfig(), fetcher=fetcher, exchange=exchange,
                now=PASS_TS, cache_dir=Path(tmp) / "cache", quiet=True)
        self.assertTrue(record["ok"])
        market_calls = [c for c in fetcher.calls if "coins/markets" in c[0]]
        self.assertEqual(len(market_calls), 2)  # 1 pool A + 1 pool B, never more
        gainers = [c for c in market_calls if c[1] and "order" in c[1]]
        self.assertEqual(len(gainers), 1)
        # the live-verified fallback request (CG ignores the price-change
        # order values; volume_desc is honored — see the M6+ smoke report)
        self.assertEqual(gainers[0][1],
                         {"vs_currency": "usd", "order": "volume_desc",
                          "per_page": "250", "page": "1"})

    def test_gainers_disabled_makes_no_pool_b_request(self):
        f = poolb_fixture()
        fetcher, exchange = fixture_fetcher(f)
        with tempfile.TemporaryDirectory() as tmp:
            record = jev_scout.run_scout_pass(
                ScoutConfig(gainers_enabled=False), fetcher=fetcher,
                exchange=exchange, now=PASS_TS,
                cache_dir=Path(tmp) / "cache", quiet=True)
        self.assertTrue(record["ok"])
        self.assertEqual(len(fetcher.calls), 2)  # trending + pool A markets only

    def test_parse_gainers_keeps_top_movers_by_abs_change(self):
        rows = [{"id": "pump", "symbol": "p",
                 "price_change_percentage_24h": 250.0},
                {"id": "dump", "symbol": "d",
                 "price_change_percentage_24h": -180.0},
                {"id": "calm", "symbol": "c",
                 "price_change_percentage_24h": -2.0},
                {"id": "flat", "symbol": "f",
                 "price_change_percentage_24h": 1.0}]
        seeds = jev_scout.parse_gainers(json.dumps(rows), 3)
        self.assertEqual([s["id"] for s in seeds], ["pump", "dump", "calm"])

    def test_parse_gainers_ties_broken_by_id_and_nulls_sort_last(self):
        rows = [{"id": "b", "price_change_percentage_24h": 5.0},
                {"id": "a", "price_change_percentage_24h": -5.0},
                {"id": "z", "price_change_percentage_24h": None}]
        seeds = jev_scout.parse_gainers(json.dumps(rows))
        self.assertEqual([s["id"] for s in seeds], ["a", "b", "z"])

    def test_parse_gainers_rejects_malformed(self):
        with self.assertRaises(jev_scout.ScoutDataError):
            jev_scout.parse_gainers("{\"nope\": 1}", 100)

    def test_gainers_per_page_caps_the_kept_pool(self):
        out = evaluate(poolb_fixture(), ScoutConfig(gainers_per_page=2))
        self.assertEqual(out["funnel"]["gainers"], 2)   # top-2 movers kept
        self.assertEqual([c["coingecko_id"] for c in out["candidates"]],
                         ["bitcoin", "ethereum", "mooncoin", "ghostcoin"])


class JunkMetricTests(unittest.TestCase):
    """M6+: junk-rejection rate = junk rejects / unique candidates (honest).

    ``over_cap`` rejects are qualified-but-capped survivors, NOT junk (see
    REJECT_OVER_CAP), so they never count toward junk rejection — counting
    them would make the metric mechanical cap arithmetic. Anti-gaming: the
    definition is fixed here and reported as-is, never tuned upward.
    """

    def test_junk_rejection_formula_with_known_rejects(self):
        out = evaluate(poolb_fixture(), ScoutConfig(max_pairs=1))
        f = out["funnel"]
        self.assertEqual(f["trending"], 2)
        self.assertEqual(f["gainers"], 5)
        self.assertEqual(f["unique_candidates"], 6)  # bitcoin deduped
        self.assertEqual(f["mapped"], 6)
        self.assertEqual(f["filtered"], 3)
        self.assertEqual(f["ranked"], 1)
        self.assertEqual(f["reject_reasons"],
                         {"low_volume": 1, "not_binance_listed": 1,
                          "young_listing": 1, "over_cap": 2})
        self.assertEqual(f["rejected"], 5)        # incl. over_cap (bookkeeping)
        self.assertEqual(f["junk_rejected"], 3)   # over_cap is NOT junk
        self.assertEqual(f["junk_rejection_rate"], 0.5)  # 3 / 6 unique
        self.assertTrue(f["filter_correctness"])

    def test_format_report_shows_pools_separately_and_both_metrics(self):
        f = poolb_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            record = run_pass(f, cache_dir=Path(tmp) / "cache")
        text = jev_scout.format_report(record)
        for needle in ("funnel: trending=2 gainers=5 unique_candidates=6",
                       "mapped=6 filtered=3 ranked=3",
                       "reject reasons: low_volume=1 not_binance_listed=1 "
                       "young_listing=1",
                       "junk-rejection rate: 3/6 = 50.0% "
                       "(rejects / unique candidates)",
                       "filter correctness: all 3 selected pass vol+age "
                       "floors: YES   <- hard gate"):
            self.assertIn(needle, text)


class HardGateTests(unittest.TestCase):
    """M6+ acceptance #1 (HARD): every selected pair clears vol+age floors."""

    def entry(self, vol=5_000_000.0, age=30.0):
        return {"symbol": "MOONUSDT", "binance_quote_volume_usd": vol,
                "listing_age_days": age}

    def test_boundary_exactly_at_both_floors_passes(self):
        jev_scout.assert_filter_correctness([self.entry()], ScoutConfig())
        self.assertTrue(
            jev_scout.filter_correctness([self.entry()], ScoutConfig()))

    def test_volume_just_below_floor_raises(self):
        with self.assertRaises(jev_scout.ScoutHardGateError) as ctx:
            jev_scout.assert_filter_correctness(
                [self.entry(vol=4_999_999.99)], ScoutConfig())
        self.assertIn("MOONUSDT", str(ctx.exception))

    def test_age_just_below_floor_raises(self):
        with self.assertRaises(jev_scout.ScoutHardGateError):
            jev_scout.assert_filter_correctness(
                [self.entry(age=29.999)], ScoutConfig())

    def test_missing_and_nan_values_violate(self):
        for bad in ({"symbol": "X", "binance_quote_volume_usd": None,
                     "listing_age_days": 30.0},
                    {"symbol": "X", "binance_quote_volume_usd": float("nan"),
                     "listing_age_days": 30.0},
                    {"symbol": "X", "binance_quote_volume_usd": 5_000_000.0,
                     "listing_age_days": float("nan")}):
            self.assertFalse(
                jev_scout.filter_correctness([bad], ScoutConfig()))
            with self.assertRaises(jev_scout.ScoutHardGateError):
                jev_scout.assert_filter_correctness([bad], ScoutConfig())

    def test_fault_fixture_nan_volume_fires_hard_gate(self):
        # NaN slips the `<` volume filter but can never prove the floor.
        f = Fixture().seed("mooncoin", "moon")
        f.market("mooncoin", "moon", 50_000_000.0, 4.0)
        f.listed("MOON", 10_000_000.0, 120.0)
        f.tickers["MOON/USDT"] = {"quoteVolume": float("nan")}
        with self.assertRaises(jev_scout.ScoutHardGateError):
            evaluate(f)

    def test_fault_fixture_nan_age_fires_hard_gate(self):
        f = Fixture().seed("mooncoin", "moon")
        f.market("mooncoin", "moon", 50_000_000.0, 4.0)
        f.listed("MOON", 10_000_000.0, 120.0)
        f.ohlcv["MOONUSDT"] = float("nan")   # unverifiable age -> gate fires
        with self.assertRaises(jev_scout.ScoutHardGateError):
            evaluate(f)

    def test_hard_gate_fails_the_pass_loudly_without_crashing(self):
        f = Fixture().seed("mooncoin", "moon")
        f.market("mooncoin", "moon", 50_000_000.0, 4.0)
        f.listed("MOON", 10_000_000.0, 120.0)
        f.tickers["MOON/USDT"] = {"quoteVolume": float("nan")}
        with tempfile.TemporaryDirectory() as tmp:
            record = run_pass(f, cache_dir=Path(tmp) / "cache")
        self.assertFalse(record["ok"])        # loud failure, never a crash
        self.assertIn("filter-correctness gate", record["error"])

    def test_report_renders_no_when_correctness_false(self):
        record = {"run_id": "x", "ts": PASS_TS, "ok": True,
                  "candidates": [], "ranked": [],
                  "funnel": {"trending": 1, "gainers": 0,
                             "unique_candidates": 1, "mapped": 1,
                             "filtered": 1, "ranked": 1, "rejected": 0,
                             "reject_reasons": {}, "junk_rejected": 0,
                             "junk_rejection_rate": 0.0,
                             "filter_correctness": False}}
        self.assertIn("floors: NO   <- hard gate",
                      jev_scout.format_report(record))


class PoolBFailureTests(unittest.TestCase):
    """M6+: a Pool B fetch failure must never take the scout/loop down."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "scout_cache"
        self.conn = jev_store.connect(":memory:")

    def tearDown(self):
        self._tmp.cleanup()

    def run_with_pool_b(self, pool_b, quiet=True):
        f = poolb_fixture()
        fetcher, exchange = fixture_fetcher(f)
        fetcher.responses["coins/markets"].pool_b = pool_b
        return jev_scout.run_scout_pass(
            ScoutConfig(), fetcher=fetcher, exchange=exchange, now=PASS_TS,
            cache_dir=self.cache, conn=self.conn, quiet=quiet)

    def test_pool_b_http_failure_fails_safe(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            record = self.run_with_pool_b(
                jev_scout.ScoutFetchError("429 rate limited"), quiet=False)
        self.assertTrue(record["ok"])                  # pass stays valid
        self.assertIn("429", record["pool_b_error"])   # error logged
        self.assertIn("Pool A only", out.getvalue())
        self.assertEqual(record["funnel"]["trending"], 2)
        self.assertNotIn("unique_candidates", record["funnel"])
        self.assertEqual([c["coingecko_id"] for c in record["ranked"]],
                         ["bitcoin", "ethereum"])
        self.assertEqual(jev_store.latest_scout_run(self.conn)["ok"], 1)

    def test_pool_b_malformed_json_fails_the_pass_loudly(self):
        record = self.run_with_pool_b("{not json!!")
        self.assertFalse(record["ok"])
        self.assertIn("malformed", record["error"])


class GainersDisabledByteForByteTests(unittest.TestCase):
    """gainers_enabled:false (or Pool B raw absent) == M6 (cda71a5) exactly."""

    def disabled_out(self):
        return evaluate(poolb_fixture(), ScoutConfig(gainers_enabled=False))

    def absent_out(self):
        f = poolb_fixture()
        raws = f.raws()
        raws.pop("coins_markets_gainers")  # pre-M6+ cached pass: no Pool B raw
        return jev_scout.evaluate_pass(raws, PASS_TS, ScoutConfig())

    def test_candidates_json_byte_identical_to_m6(self):
        for out in (self.disabled_out(), self.absent_out()):
            self.assertEqual(
                jev_scout.candidates_json_text(out["candidates"]),
                M6_DISABLED_CANDIDATES_JSON)

    def test_ranked_json_byte_identical_to_m6(self):
        for out in (self.disabled_out(), self.absent_out()):
            self.assertEqual(jev_scout.ranked_json_text(out["ranked"]),
                             M6_DISABLED_RANKED_JSON)

    def test_funnel_byte_identical_to_m6(self):
        for out in (self.disabled_out(), self.absent_out()):
            self.assertEqual(json.dumps(out["funnel"], sort_keys=True,
                                        separators=(",", ":")),
                             M6_DISABLED_FUNNEL_JSON)

    def test_report_byte_identical_to_m6(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_pass(poolb_fixture(),
                              cfg=ScoutConfig(gainers_enabled=False),
                              cache_dir=Path(tmp) / "cache")
        self.assertEqual(jev_scout.format_report(record), M6_DISABLED_REPORT)


class FunnelTests(unittest.TestCase):
    def test_funnel_counts(self):
        out = evaluate(standard_fixture())
        self.assertEqual(out["funnel"],
                         {"trending": 7, "gainers": 0, "unique_candidates": 7,
                          "mapped": 6, "filtered": 4, "ranked": 4,
                          "rejected": 3,
                          "reject_reasons": {"trending_only": 1,
                                             "not_binance_listed": 1,
                                             "young_listing": 1},
                          "junk_rejected": 3,
                          "junk_rejection_rate": 3 / 7,
                          "filter_correctness": True})

    def test_format_report_shows_funnel_and_junk_rate(self):
        f = standard_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            record = run_pass(f, cache_dir=Path(tmp) / "cache")
        text = jev_scout.format_report(record)
        for needle in ("funnel: trending=7 gainers=0 unique_candidates=7",
                       "mapped=6", "filtered=4", "ranked=4",
                       "not_binance_listed",
                       "junk-rejection rate: 3/7 = 42.9% "
                       "(rejects / unique candidates)",
                       "filter correctness: all 4 selected pass vol+age "
                       "floors: YES   <- hard gate"):
            self.assertIn(needle, text)

# -- raw cache + persistence + --replay reproducibility -----------------------

class CacheReplayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "scout_cache"
        # file-backed store so the --replay CLI (a separate connection) sees
        # the same stored pass rows as the tests.
        self.conn = jev_store.connect(str(self.tmp / "s.db"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_pass_caches_every_raw_input(self):
        run_pass(standard_fixture(), cache_dir=self.cache, conn=self.conn)
        files = sorted(p.name for p in self.cache.glob("*.json"))
        self.assertTrue(any("-search_trending.json" in n for n in files))
        self.assertTrue(any("-coins_markets.json" in n for n in files))
        self.assertTrue(any("-binance_tickers.json" in n for n in files))
        self.assertTrue(any("-binance_ohlcv_" in n for n in files))
        self.assertTrue(any("-coins_markets_gainers.json" in n for n in files))

    def test_run_pass_persists_scout_run(self):
        record = run_pass(standard_fixture(), cache_dir=self.cache,
                          conn=self.conn)
        row = jev_store.get_scout_run(self.conn, record["run_id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["ok"], 1)
        self.assertEqual(row["ts"], PASS_TS)
        self.assertEqual(json.loads(row["ranked_json"]),
                         record["ranked"])
        self.assertEqual(json.loads(row["candidates_json"]),
                         record["candidates"])
        hashes = json.loads(row["raw_hashes_json"])
        self.assertIn("search_trending", hashes)
        self.assertIn("binance_tickers", hashes)

    def test_replay_reproduces_ranked_list_byte_identical(self):
        record = run_pass(standard_fixture(), cache_dir=self.cache,
                          conn=self.conn)
        out = jev_scout.replay_pass(record["run_id"], cache_dir=self.cache,
                                    conn=self.conn)
        self.assertTrue(out["ok"])
        row = jev_store.get_scout_run(self.conn, record["run_id"])
        self.assertEqual(out["ranked_json"], row["ranked_json"])
        self.assertEqual(out["candidates_json"], row["candidates_json"])
        self.assertIs(out["match_stored"], True)

    def test_replay_same_pass_twice_is_identical(self):
        record = run_pass(standard_fixture(), cache_dir=self.cache,
                          conn=self.conn)
        one = jev_scout.replay_pass(record["run_id"], cache_dir=self.cache)
        two = jev_scout.replay_pass(record["run_id"], cache_dir=self.cache)
        self.assertEqual(one["ranked_json"], two["ranked_json"])

    def test_replay_byte_identical_with_pool_tags(self):
        record = run_pass(poolb_fixture(), cache_dir=self.cache,
                          conn=self.conn)
        out = jev_scout.replay_pass(record["run_id"], cache_dir=self.cache,
                                    conn=self.conn)
        self.assertIs(out["match_stored"], True)
        self.assertIn('"seed":"trending+gainers"', record["candidates_json"])
        again = jev_scout.replay_pass(record["run_id"], cache_dir=self.cache)
        self.assertEqual(out["ranked_json"], again["ranked_json"])
        self.assertEqual(out["candidates_json"], again["candidates_json"])

    def test_replay_cli_reports_reproducible(self):
        record = run_pass(standard_fixture(), cache_dir=self.cache,
                          conn=self.conn)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = jev_scout.main(["--replay", record["run_id"],
                                   "--cache-dir", str(self.cache),
                                   "--db", str(self.tmp / "s.db")])
        self.assertEqual(code, 0)
        self.assertIn("byte-identical", out.getvalue())

    def test_replay_cli_json_is_machine_readable(self):
        record = run_pass(standard_fixture(), cache_dir=self.cache,
                          conn=self.conn)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = jev_scout.main(["--replay", record["run_id"], "--json",
                                   "--cache-dir", str(self.cache),
                                   "--db", str(self.tmp / "s.db")])
        self.assertEqual(code, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["run_id"], record["run_id"])
        self.assertEqual([c["symbol"] for c in parsed["ranked"]],
                         [c["symbol"] for c in record["ranked"]])

    def test_replay_cli_unknown_pass_errors(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = jev_scout.main(["--replay", "19990101T000000Z",
                                   "--cache-dir", str(self.cache),
                                   "--db", str(self.tmp / "s.db")])
        self.assertEqual(code, 2)

    def test_same_inputs_same_run_id_identical(self):
        a = run_pass(standard_fixture(), cache_dir=self.cache, conn=self.conn)
        b = run_pass(standard_fixture(), cache_dir=self.cache, conn=self.conn)
        self.assertEqual(a["ranked_json"] if "ranked_json" in a
                         else jev_scout.ranked_json_text(a["ranked"]),
                         jev_scout.ranked_json_text(b["ranked"]))

    def test_replay_matches_live_pass_with_subsecond_clock(self):
        # regression: a live time.time() with sub-second precision must still
        # replay byte-identically (pass_ts snaps to the run-id second).
        record = run_pass(standard_fixture(), now=PASS_TS + 0.75,
                          cache_dir=self.cache, conn=self.conn)
        out = jev_scout.replay_pass(record["run_id"], cache_dir=self.cache,
                                    conn=self.conn)
        self.assertIs(out["match_stored"], True)

# -- fault injection: scout failure keeps the last good list, never crashes ---

class FaultInjectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "scout_cache"
        self.conn = jev_store.connect(":memory:")

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_pass_never_replaces_last_good(self):
        run_pass(standard_fixture(), cache_dir=self.cache, conn=self.conn)
        fetcher, exchange = fixture_fetcher(standard_fixture())
        fetcher.responses["search/trending"] = jev_scout.ScoutFetchError(
            "429 too many requests")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            record = jev_scout.run_scout_pass(
                ScoutConfig(), fetcher=fetcher, exchange=exchange,
                now=PASS_TS + 3600.0, cache_dir=self.cache, conn=self.conn,
                quiet=False)
        self.assertFalse(record["ok"])
        self.assertIn("429", record["error"])
        self.assertIn("keeping last good list", out.getvalue())  # logged
        latest = jev_store.latest_scout_run(self.conn)
        self.assertEqual(latest["ts"], PASS_TS)      # last good list kept
        self.assertEqual(latest["ok"], 1)

    def test_malformed_json_keeps_last_good_list(self):
        run_pass(standard_fixture(), cache_dir=self.cache, conn=self.conn)
        fetcher, exchange = fixture_fetcher(standard_fixture())
        fetcher.responses["coins/markets"] = "{not json!!"
        record = jev_scout.run_scout_pass(
            ScoutConfig(), fetcher=fetcher, exchange=exchange,
            now=PASS_TS + 3600.0, cache_dir=self.cache, conn=self.conn,
            quiet=True)
        self.assertFalse(record["ok"])
        self.assertIn("malformed", record["error"])
        self.assertEqual(jev_store.latest_scout_run(self.conn)["ts"], PASS_TS)

    def test_exchange_error_never_raises(self):
        class BoomExchange:
            def fetch_tickers(self, symbols=None):
                raise OSError("binance down")

        fetcher, _ = fixture_fetcher(standard_fixture())
        record = jev_scout.run_scout_pass(
            ScoutConfig(), fetcher=fetcher, exchange=BoomExchange(),
            now=PASS_TS, cache_dir=self.cache, conn=self.conn, quiet=True)
        self.assertFalse(record["ok"])
        self.assertIn("binance down", record["error"])

    def test_http_get_retries_with_exponential_backoff(self):
        sleeps = []
        session = FakeSession([FakeResp(429, "slow down"),
                               FakeResp(500, "oops"),
                               FakeResp(200, "{\"ok\": true}")])
        text = jev_scout.http_get("https://api.coingecko.com/api/v3/x",
                                  session=session, sleep=sleeps.append)
        self.assertEqual(text, "{\"ok\": true}")
        self.assertEqual(session.calls, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_http_get_raises_after_retries(self):
        session = FakeSession([FakeResp(429, "no")] * 3)
        with self.assertRaises(jev_scout.ScoutFetchError):
            jev_scout.http_get("https://api.coingecko.com/api/v3/x",
                               session=session, sleep=lambda s: None)

    def test_http_get_retries_connection_errors(self):
        sleeps = []
        session = FakeSession([OSError("reset"),
                               FakeResp(200, "data")])
        text = jev_scout.http_get("https://api.coingecko.com/api/v3/x",
                                  session=session, sleep=sleeps.append)
        self.assertEqual(text, "data")
        self.assertEqual(len(sleeps), 1)

    def test_scout_disabled_in_config_refuses_run(self):
        cfg_path = Path(self._tmp.name) / "off.yaml"
        cfg_path.write_text("config_version: 4\nscout:\n  enabled: false\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = jev_scout.main(["--once", "--config", str(cfg_path),
                                   "--cache-dir", str(self.cache),
                                   "--db", str(self._tmp.name) + "/s.db"])
        self.assertEqual(code, 2)
        self.assertIn("disabled", out.getvalue())


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = jev_store.connect(":memory:")

    def row(self, run_id, ts, ok=1):
        return {"run_id": run_id, "ts": ts, "ok": ok, "error": None,
                "raw_hashes_json": "{}", "candidates_json": "[]",
                "ranked_json": '[{"symbol": "BTCUSDT"}]'}

    def test_insert_scout_run_upserts_by_run_id(self):
        self.assertTrue(jev_store.insert_scout_run(
            self.conn, self.row("20260928T000000Z", 1.0)))
        self.assertFalse(jev_store.insert_scout_run(
            self.conn, self.row("20260928T000000Z", 1.0)))
        self.assertEqual(len(jev_store.list_scout_runs(self.conn)), 1)

    def test_latest_scout_run_prefers_newest_ok(self):
        jev_store.insert_scout_run(self.conn, self.row("a", 1.0))
        jev_store.insert_scout_run(self.conn, self.row("b", 2.0))
        jev_store.insert_scout_run(self.conn, self.row("c", 3.0, ok=0))
        self.assertEqual(jev_store.latest_scout_run(self.conn)["run_id"], "b")
        self.assertEqual(
            jev_store.latest_scout_run(self.conn, ok_only=False)["run_id"], "c")

    def test_latest_scout_run_empty(self):
        self.assertIsNone(jev_store.latest_scout_run(self.conn))

# -- supervisor scout universe (opt-in): fresh -> scout, stale/empty -> static --

class FakeMarketStub:
    exchange = object()


def insert_pass(conn, run_id, ts, symbols, ok=1):
    jev_store.insert_scout_run(conn, {
        "run_id": run_id, "ts": ts, "ok": ok, "error": None,
        "raw_hashes_json": "{}", "candidates_json": "[]",
        "ranked_json": json.dumps([{"symbol": s} for s in symbols],
                                  sort_keys=True)})


class SupervisorScoutModeTests(unittest.TestCase):
    STATIC = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = jev_store.connect(":memory:")

    def tearDown(self):
        self._tmp.cleanup()

    def make_sup(self, symbols=("BTCUSDT", "ETHUSDT"), books=None,
                 portfolio_cfg=None):
        books = books or {s: BookPair(spot=FakeBook(), perps=None)
                          for s in symbols}
        return ScoutSupervisor(
            list(symbols), market=FakeMarketStub(), client=object(),
            books=books, conn=self.conn, clock=lambda: PASS_TS,
            static_pairs=list(self.STATIC), scout_cfg=ScoutConfig(),
            books_factory=lambda syms: {s: BookPair(spot=FakeBook(),
                                                    perps=None)
                                        for s in syms},
            portfolio_cfg=portfolio_cfg,
            risk_state_path=str(self.tmp / "risk.json"))

    def test_resolve_reads_latest_pass(self):
        insert_pass(self.conn, "20260928T100000Z", PASS_TS,
                    ["DOGEUSDT", "WIFUSDT"])
        symbols, source, detail = resolve_scout_universe(
            self.conn, ScoutConfig(), self.STATIC, now=PASS_TS + 60)
        self.assertEqual(symbols, ["DOGEUSDT", "WIFUSDT"])
        self.assertEqual(source, "scout")

    def test_resolve_stale_falls_back_and_logs(self):
        insert_pass(self.conn, "old", PASS_TS, ["DOGEUSDT"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            symbols, source, detail = resolve_scout_universe(
                self.conn, ScoutConfig(), self.STATIC,
                now=PASS_TS + 2 * 3600 + 1)      # > 2x interval_seconds
        self.assertEqual(symbols, self.STATIC)
        self.assertEqual(source, "fallback_static")
        self.assertIn("stale", out.getvalue())

    def test_resolve_empty_falls_back_and_logs(self):
        insert_pass(self.conn, "x", PASS_TS, [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            symbols, source, detail = resolve_scout_universe(
                self.conn, ScoutConfig(), self.STATIC, now=PASS_TS + 60)
        self.assertEqual(symbols, self.STATIC)
        self.assertEqual(source, "fallback_static")
        self.assertIn("empty", out.getvalue())

    def test_resolve_malformed_ranked_falls_back(self):
        jev_store.insert_scout_run(self.conn, {
            "run_id": "x", "ts": PASS_TS, "ok": 1, "error": None,
            "raw_hashes_json": "{}", "candidates_json": "[]",
            "ranked_json": "{not json"})
        symbols, source, detail = resolve_scout_universe(
            self.conn, ScoutConfig(), self.STATIC, now=PASS_TS + 60)
        self.assertEqual(symbols, self.STATIC)
        self.assertEqual(source, "fallback_static")

    def test_resolve_no_pass_falls_back(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            symbols, source, detail = resolve_scout_universe(
                self.conn, ScoutConfig(), self.STATIC, now=PASS_TS)
        self.assertEqual(symbols, self.STATIC)
        self.assertEqual(source, "fallback_static")
        self.assertIn("no scout pass", out.getvalue())

    def test_resolve_store_error_falls_back_never_raises(self):
        class Broken:
            def execute(self, *a, **k):
                raise OSError("db locked")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            symbols, source, detail = resolve_scout_universe(
                Broken(), ScoutConfig(), self.STATIC, now=PASS_TS)
        self.assertEqual(symbols, self.STATIC)
        self.assertEqual(source, "fallback_static")
        self.assertIn("store error", out.getvalue())

    def test_resolve_ignores_failed_passes(self):
        insert_pass(self.conn, "good", PASS_TS, ["DOGEUSDT"])
        insert_pass(self.conn, "bad", PASS_TS + 10, ["JUNKUSDT"], ok=0)
        symbols, source, detail = resolve_scout_universe(
            self.conn, ScoutConfig(), self.STATIC, now=PASS_TS + 60)
        self.assertEqual(symbols, ["DOGEUSDT"])   # last good list kept

    def test_static_default_untouched_without_universe_flag(self):
        cfg = load_config(str(SHIPPED_YAML))
        args = supervisor_parse_args([])          # no --universe, no --pairs
        symbols, use_scout = resolve_initial_universe(args, cfg, conn=None)
        self.assertFalse(use_scout)
        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    def test_pairs_flag_overrides_static_universe(self):
        cfg = load_config(str(SHIPPED_YAML))
        args = supervisor_parse_args(["--pairs", "BTCUSDT,XRPUSDT"])
        symbols, use_scout = resolve_initial_universe(args, cfg, conn=None)
        self.assertFalse(use_scout)
        self.assertEqual(symbols, ["BTCUSDT", "XRPUSDT"])

    def test_initial_universe_scout_uses_store(self):
        cfg = load_config(str(SHIPPED_YAML))
        insert_pass(self.conn, "p", PASS_TS, ["DOGEUSDT", "WIFUSDT"])
        args = supervisor_parse_args(["--universe", "scout"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            symbols, use_scout = resolve_initial_universe(args, cfg, self.conn)
        self.assertTrue(use_scout)
        self.assertEqual(symbols, ["DOGEUSDT", "WIFUSDT"])
        self.assertIn("scout", out.getvalue())

    def test_initial_universe_scout_disabled_falls_back(self):
        from dataclasses import replace

        cfg = replace(load_config(str(SHIPPED_YAML)),
                      scout=ScoutConfig(enabled=False))
        insert_pass(self.conn, "p", PASS_TS, ["DOGEUSDT"])
        args = supervisor_parse_args(["--universe", "scout"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            symbols, use_scout = resolve_initial_universe(args, cfg, self.conn)
        self.assertFalse(use_scout)
        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertIn("disabled", out.getvalue())

    def test_refresh_updates_universe(self):
        sup = self.make_sup()
        insert_pass(self.conn, "p", PASS_TS, ["DOGEUSDT", "WIFUSDT"])
        sup.refresh_universe()
        self.assertEqual(sup.symbols, ["DOGEUSDT", "WIFUSDT"])
        self.assertIn("DOGEUSDT", sup.caches)
        self.assertIn("DOGEUSDT", sup.books)
        self.assertEqual(sup.universe_source, "scout")

    def test_refresh_retains_open_position_until_flat(self):
        books = {"BTCUSDT": BookPair(spot=FakeBook(position={"qty": 1.0}),
                                     perps=None),
                 "ETHUSDT": BookPair(spot=FakeBook(), perps=None)}
        sup = self.make_sup(symbols=("BTCUSDT", "ETHUSDT"), books=books)
        insert_pass(self.conn, "p", PASS_TS, ["DOGEUSDT"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sup.refresh_universe()
        # BTCUSDT holds a position -> stays managed (stops/exits keep running)
        self.assertEqual(sup.symbols, ["DOGEUSDT", "BTCUSDT"])
        self.assertIn("retained", out.getvalue())
        books["BTCUSDT"].spot.position = None       # closed -> dropped next pass
        sup.refresh_universe()
        self.assertEqual(sup.symbols, ["DOGEUSDT"])

    def test_refresh_failure_keeps_universe(self):
        sup = self.make_sup()
        with mock.patch("jevelin_supervisor.resolve_scout_universe",
                        side_effect=RuntimeError("boom")):
            sup.refresh_universe()                  # must never raise
        self.assertEqual(sup.symbols, ["BTCUSDT", "ETHUSDT"])

    def test_slow_cycle_still_runs_when_refresh_fails(self):
        sup = self.make_sup()
        with mock.patch("jevelin_supervisor.resolve_scout_universe",
                        side_effect=RuntimeError("boom")), \
                mock.patch.object(Supervisor, "slow_cycle",
                                  return_value=[]) as base:
            sup.slow_cycle()
        base.assert_called_once()

    def test_risk_pairs_follow_universe(self):
        from jev_config import PortfolioConfig

        sup = self.make_sup(portfolio_cfg=PortfolioConfig())
        insert_pass(self.conn, "p", PASS_TS, ["DOGEUSDT"])
        sup.refresh_universe()
        self.assertEqual(tuple(sup.risk.pairs), ("DOGEUSDT",))

# -- scout config section (config/v2.yaml v4 + CLI override) ------------------

def _write_cfg(text):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(text)
    tmp.close()
    return tmp.name


class ScoutConfigTests(unittest.TestCase):
    def test_shipped_yaml_loads_v4_with_scout_defaults(self):
        cfg = load_config(str(SHIPPED_YAML))
        self.assertEqual(cfg.config_version, 4)
        self.assertEqual(cfg.scout, ScoutConfig())

    def test_scout_defaults_match_spec(self):
        s = ScoutConfig()
        self.assertTrue(s.enabled)
        self.assertEqual(s.quote, "USDT")
        self.assertEqual(s.min_24h_vol_usd, 5_000_000.0)
        self.assertEqual(s.min_listing_age_days, 30.0)
        self.assertEqual(s.max_pairs, 5)
        self.assertEqual(s.interval_seconds, 3600.0)
        self.assertTrue(s.gainers_enabled)
        self.assertEqual(s.gainers_per_page, 100)

    def test_scout_yaml_overrides(self):
        cfg = load_config(_write_cfg(
            "config_version: 4\nscout:\n  min_24h_vol_usd: 1000\n"
            "  max_pairs: 3\n  quote: USDC\n"))
        self.assertEqual(cfg.scout.min_24h_vol_usd, 1000.0)
        self.assertEqual(cfg.scout.max_pairs, 3)
        self.assertEqual(cfg.scout.quote, "USDC")
        self.assertEqual(cfg.scout.min_listing_age_days,
                         ScoutConfig().min_listing_age_days)

    def test_gainers_yaml_overrides(self):
        cfg = load_config(_write_cfg(
            "config_version: 4\nscout:\n  gainers_enabled: false\n"
            "  gainers_per_page: 50\n"))
        self.assertFalse(cfg.scout.gainers_enabled)
        self.assertEqual(cfg.scout.gainers_per_page, 50)

    def test_gainers_enabled_must_be_bool(self):
        self.assertConfigError("scout:\n  gainers_enabled: maybe\n",
                               "gainers_enabled")

    def test_gainers_per_page_must_be_positive(self):
        self.assertConfigError("scout:\n  gainers_per_page: 0\n",
                               "gainers_per_page")

    def assertConfigError(self, text, fragment):
        with self.assertRaises(ConfigError) as ctx:
            load_config(_write_cfg(text))
        self.assertIn(fragment, str(ctx.exception))

    def test_enabled_must_be_bool(self):
        self.assertConfigError("scout:\n  enabled: maybe\n", "enabled")

    def test_max_pairs_must_be_positive(self):
        self.assertConfigError("scout:\n  max_pairs: 0\n", "max_pairs")

    def test_volume_floor_must_be_non_negative(self):
        self.assertConfigError("scout:\n  min_24h_vol_usd: -1\n",
                               "min_24h_vol_usd")

    def test_quote_must_be_non_empty(self):
        self.assertConfigError("scout:\n  quote: ''\n", "quote")

    def test_cli_overrides_beat_yaml(self):
        cfg = load_config(_write_cfg("config_version: 4\nscout:\n"
                                     "  max_pairs: 3\n"))
        args = jev_scout.parse_args(["--max-pairs", "2",
                                     "--min-24h-vol-usd", "42"])
        s = jev_scout.scout_config_from_args(args, cfg)
        self.assertEqual(s.max_pairs, 2)
        self.assertEqual(s.min_24h_vol_usd, 42.0)

    def test_cli_unset_flags_keep_yaml(self):
        cfg = load_config(_write_cfg("config_version: 4\nscout:\n"
                                     "  max_pairs: 3\n"))
        args = jev_scout.parse_args([])
        s = jev_scout.scout_config_from_args(args, cfg)
        self.assertEqual(s.max_pairs, 3)
        self.assertEqual(s.min_listing_age_days, 30.0)

    def test_config_version_3_still_loads(self):
        # M6 bumps to v4 but v3 files (pre-scout) keep loading with defaults.
        cfg = load_config(_write_cfg("config_version: 3\n"))
        self.assertEqual(cfg.config_version, 3)
        self.assertEqual(cfg.scout, ScoutConfig())

    def test_unsupported_version_rejected(self):
        self.assertConfigError("config_version: 2\n", "config_version")


if __name__ == "__main__":
    unittest.main(verbosity=2)
