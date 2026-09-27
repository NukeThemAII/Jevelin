#!/usr/bin/env python3
"""jev_radar — read-only CoinGecko discovery radar (radar-lite, M6 pre-pass).

DISCOVERY ONLY: it never calls gates, books, or the paper loop — zero trading
impact. CLI: ``--once`` for a single pass, ``--interval [SEC]`` to loop
(default hourly).

Flow per run:
1. CoinGecko /search/trending (keyless) -> trending coins (id, symbol, rank).
2. CoinGecko /coins/markets?vs_currency=usd&order=volume_desc&per_page=100 ->
   volume/mcap/ath_date/current price, merged onto the trending coins by id.
   Trending ids outside the top-100 volume page are enriched with one extra
   /coins/markets?ids=... call so the volume/mcap floors are judged on real
   numbers — a rejection reason is never invented from missing data.
3. Binance spot USDT bases (ccxt binance, keyless): spot markets quoted in
   USDT only — leveraged up/down tokens and non-USDT quotes excluded. Cached
   in runtime/binance_symbols.json with a 24h TTL (atomic write).
4. Filters — EVERY coin is logged with ALL its failed reasons (filter order):
     not_binance_spot_usdt  not on Binance spot USDT
     low_volume             24h volume < $5M (missing data fails closed)
     low_mcap               mcap < $20M (missing data fails closed)
     too_new                ATH < 30 days old (missing ath_date fails closed)
     stablecoin             pegged/wrapped deny-list
     core_pair              BTC/ETH/SOL
5. Survivors ranked by 24h volume; top 5 = candidates. Every coin (candidate
   and rejections) is appended to runtime/radar_candidates.jsonl and upserted
   into the store table radar_candidates.
6. Exit prints the candidate list (symbol, vol, mcap, ath age) + the rejection
   histogram (reason -> count). Any API error is logged to stderr and exits
   non-zero with NO partial writes (all fetches complete before any write).

Discovery only: CoinGecko tells us where to look, never what to do.
Usage: .venv/bin/python scripts/jev_radar.py [--once | --interval [SEC]]
       [--runtime-dir runtime] [--db runtime/jevelin.db]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

import jev_store  # noqa: E402
from jev_config import atomic_write_json  # noqa: E402

CG_BASE = "https://api.coingecko.com/api/v3"
BANGKOK = ZoneInfo("Asia/Bangkok")

MIN_VOLUME_USD = 5_000_000.0
MIN_MCAP_USD = 20_000_000.0
MIN_ATH_AGE_DAYS = 30.0
CANDIDATE_LIMIT = 5
SYMBOL_CACHE_TTL_S = 24 * 3600

# pegged / wrapped tokens: never trade candidates regardless of volume
STABLE_DENY = frozenset({
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "BUSD",
    "WBTC", "WETH", "STETH",
})
CORE_PAIRS = frozenset({"BTC", "ETH", "SOL"})


class RadarError(Exception):
    """Any API/transport failure — the run fails open (exit non-zero)."""


def http_get_json(url, params=None, timeout=15):
    """GET ``url`` and parse JSON. Raises RadarError on any failure."""
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        raise RadarError(f"{url}: {type(exc).__name__}: {exc}") from exc


def fetch_trending(get=None) -> list:
    """Step 1: trending coins as [{id, symbol, trending_rank, mcap_rank}]."""
    get = get or http_get_json
    data = get(CG_BASE + "/search/trending")
    entries = data.get("coins") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise RadarError("trending: empty or malformed response")
    out = []
    for pos, entry in enumerate(entries, 1):
        item = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(item, dict) or not item.get("id"):
            continue
        out.append({"id": item["id"], "symbol": item.get("symbol"),
                    "trending_rank": pos,
                    "mcap_rank": item.get("market_cap_rank")})
    if not out:
        raise RadarError("trending: no usable coins in response")
    return out


def fetch_market_rows(get=None, ids=None) -> list:
    """Step 2: market rows (volume/mcap/ath_date/price) from /coins/markets."""
    get = get or http_get_json
    if ids:
        params = {"vs_currency": "usd", "ids": ",".join(sorted(ids))}
    else:
        params = {"vs_currency": "usd", "order": "volume_desc", "per_page": 100}
    data = get(CG_BASE + "/coins/markets", params=params)
    if not isinstance(data, list):
        raise RadarError("coins/markets: malformed response")
    return [r for r in data if isinstance(r, dict) and r.get("id")]

def merge_coins(trending: list, market_rows: list) -> list:
    """Join market data onto the trending universe by CoinGecko id."""
    by_id = {r["id"]: r for r in market_rows}
    coins = []
    for t in trending:
        m = by_id.get(t["id"]) or {}
        coins.append({
            "id": t["id"],
            "symbol": (m.get("symbol") or t.get("symbol") or "").upper(),
            "name": m.get("name"),
            "trending_rank": t.get("trending_rank"),
            "mcap_rank": m.get("market_cap_rank", t.get("mcap_rank")),
            "price_usd": m.get("current_price"),
            "volume_24h": m.get("total_volume"),
            "mcap": m.get("market_cap"),
            "ath_date": m.get("ath_date"),
            "raw": {"trending": t, "market": m},
        })
    return coins


def _as_utc(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def ath_age_days(ath_date, now: datetime):
    """Days since the all-time-high date; None when unparseable/absent."""
    if not isinstance(ath_date, str) or not ath_date:
        return None
    try:
        dt = datetime.fromisoformat(ath_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_as_utc(now) - dt).total_seconds() / 86400.0


def evaluate_coin(coin: dict, binance_bases, now: datetime):
    """Pure filter: (passed, [all failed reasons in filter order]).

    Missing data fails closed — an unverifiable floor is a rejection, and the
    reasons recorded are exactly those the coin failed.
    """
    rejections = []
    sym = (coin.get("symbol") or "").upper()
    if sym not in binance_bases:
        rejections.append("not_binance_spot_usdt")
    vol = coin.get("volume_24h")
    if not isinstance(vol, (int, float)) or vol < MIN_VOLUME_USD:
        rejections.append("low_volume")
    mcap = coin.get("mcap")
    if not isinstance(mcap, (int, float)) or mcap < MIN_MCAP_USD:
        rejections.append("low_mcap")
    age = ath_age_days(coin.get("ath_date"), now)
    if age is None or age < MIN_ATH_AGE_DAYS:
        rejections.append("too_new")
    if sym in STABLE_DENY:
        rejections.append("stablecoin")
    if sym in CORE_PAIRS:
        rejections.append("core_pair")
    return (not rejections, rejections)


def rank_survivors(evaluated: list) -> list:
    """Passed rows sorted by 24h volume desc, each with a 1-based ``rank``."""
    survivors = [dict(r) for r in evaluated if not r["rejections"]]
    survivors.sort(key=lambda r: -(r["coin"].get("volume_24h") or 0.0))
    for i, r in enumerate(survivors, 1):
        r["rank"] = i
    return survivors


def rejection_histogram(evaluated: list) -> dict:
    """reason -> count over every rejection of every coin."""
    hist = {}
    for r in evaluated:
        for reason in r["rejections"]:
            hist[reason] = hist.get(reason, 0) + 1
    return hist


def _drop_leveraged(bases: set) -> set:
    """Drop Binance leveraged tokens (BTCDOWN, ETHBULL...) — sibling of a base."""
    out = set()
    for base in bases:
        stem = None
        if base.endswith(("BULL", "BEAR", "DOWN")):
            stem = base[:-4]
        elif base.endswith("UP"):
            stem = base[:-2]
        if stem and stem in bases:
            continue  # leveraged sibling of a real base asset
        out.add(base)
    return out


def fetch_binance_bases() -> list:
    """Step 3: base assets of active Binance spot USDT markets (keyless ccxt)."""
    import ccxt
    exchange = ccxt.binance({"enableRateLimit": True})
    markets = exchange.load_markets()
    bases = set()
    for market in markets.values():
        if not (market.get("spot") and market.get("quote") == "USDT"):
            continue
        base = market.get("base")
        if base:
            bases.add(str(base).upper())
    if not bases:
        raise RadarError("binance: empty symbol set")
    return sorted(_drop_leveraged(bases))


def load_binance_bases(runtime_dir, loader=None, now=None):
    """(bases set, from_cache). Fresh fetches write the 24h-TTL symbol cache."""
    now = _as_utc(now or datetime.now(timezone.utc))
    cache_path = Path(runtime_dir) / "binance_symbols.json"
    try:
        cache = json.loads(cache_path.read_text())
        ts = cache.get("ts")
        if isinstance(ts, (int, float)) \
                and now.timestamp() - ts < SYMBOL_CACHE_TTL_S:
            return set(cache.get("symbols") or []), True
    except (OSError, ValueError):
        pass
    loader = loader or fetch_binance_bases
    bases = sorted({str(s).upper() for s in loader()})
    if not bases:
        raise RadarError("binance: empty symbol set")
    atomic_write_json(cache_path, {"ts": now.timestamp(), "symbols": bases})
    return set(bases), False


def run_once(runtime_dir="runtime", db_path=None, get=None, loader=None,
             now=None, run_id=None) -> dict:
    """One discovery pass. Raises RadarError on any API failure (fail-open:
    nothing is written unless every fetch succeeded)."""
    get = get or http_get_json
    now = _as_utc(now or datetime.now(timezone.utc))
    runtime_dir = Path(runtime_dir)
    db_path = Path(db_path) if db_path else runtime_dir / "jevelin.db"
    try:
        trending = fetch_trending(get)
        market_rows = fetch_market_rows(get)
        have = {r["id"] for r in market_rows}
        missing = [t["id"] for t in trending if t["id"] not in have]
        if missing:
            market_rows += fetch_market_rows(get, ids=missing)
        bases, from_cache = load_binance_bases(runtime_dir, loader, now)
    except RadarError:
        raise  # fail-open: nothing written on any API error
    except Exception as exc:
        raise RadarError(f"{type(exc).__name__}: {exc}") from exc

    coins = merge_coins(trending, market_rows)
    evaluated = []
    for coin in coins:
        passed, rejections = evaluate_coin(coin, bases, now)
        evaluated.append({"coin": coin, "rejections": rejections})
    ranked = rank_survivors(evaluated)
    rank_by_id = {r["coin"]["id"]: r["rank"] for r in ranked}
    hist = rejection_histogram(evaluated)

    run_id = run_id or "{}-{}".format(
        now.astimezone(BANGKOK).strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8])
    # -- write phase: only reached when every fetch succeeded ----------------
    jsonl_path = runtime_dir / "radar_candidates.jsonl"
    conn = jev_store.connect(str(db_path))
    try:
        with jsonl_path.open("a", encoding="utf-8") as fh:
            for r in evaluated:
                coin, rejections = r["coin"], r["rejections"]
                row = {
                    "ts": now.timestamp(), "run_id": run_id,
                    "rank": rank_by_id.get(coin["id"]),
                    "symbol": coin["symbol"], "coingecko_id": coin["id"],
                    "price_usd": coin["price_usd"],
                    "volume_24h": coin["volume_24h"], "mcap": coin["mcap"],
                    "ath_date": coin["ath_date"],
                    "passed": 1 if not rejections else 0,
                    "rejections": rejections,
                    "raw": coin.get("raw"),
                }
                fh.write(json.dumps(row, default=str) + "\n")
                jev_store.upsert_radar_candidate(conn, {
                    "ts": row["ts"], "run_id": run_id, "rank": row["rank"],
                    "symbol": row["symbol"], "coingecko_id": row["coingecko_id"],
                    "price_usd": row["price_usd"],
                    "volume_24h": row["volume_24h"], "mcap": row["mcap"],
                    "ath_date": row["ath_date"], "passed": row["passed"],
                    "rejections_json": json.dumps(rejections),
                    "raw_json": json.dumps(coin.get("raw"), default=str),
                })
    finally:
        conn.close()

    candidates = [{
        "rank": r["rank"], "symbol": r["coin"]["symbol"],
        "coingecko_id": r["coin"]["id"], "price_usd": r["coin"]["price_usd"],
        "volume_24h": r["coin"]["volume_24h"], "mcap": r["coin"]["mcap"],
        "ath_date": r["coin"]["ath_date"],
        "ath_age_days": ath_age_days(r["coin"]["ath_date"], now),
    } for r in ranked[:CANDIDATE_LIMIT]]
    return {
        "run_id": run_id, "ts": now.timestamp(), "universe": len(evaluated),
        "passed": sum(1 for r in evaluated if not r["rejections"]),
        "candidates": candidates, "histogram": hist,
        "binance_cached": from_cache,
        "jsonl_path": str(jsonl_path), "db_path": str(db_path),
    }


def _num(value) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "n/a"


def print_report(report: dict) -> None:
    ts = report.get("ts")
    stamp = datetime.fromtimestamp(ts, BANGKOK).strftime("%Y-%m-%d %H:%M:%S") \
        if isinstance(ts, (int, float)) \
        else datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Jevelin radar — run {report['run_id']} "
          f"(generated {stamp} Asia/Bangkok)")
    src = "cache" if report.get("binance_cached") else "fetched"
    print(f"universe: {report['universe']} trending coins | "
          f"passed: {report['passed']} | binance symbols: {src}")
    print(f"candidates (top {CANDIDATE_LIMIT} by 24h volume):")
    print(f"  {'rank':<5}{'symbol':<10}{'volume_24h':>16}{'mcap':>16}{'ath age':>10}")
    for c in report["candidates"]:
        age = c.get("ath_age_days")
        age_s = f"{age:.0f}d" if isinstance(age, (int, float)) else "n/a"
        print(f"  {c['rank']:<5}{c['symbol']:<10}"
              f"{_num(c['volume_24h']):>16}{_num(c['mcap']):>16}{age_s:>10}")
    print("rejection histogram (reason -> count):")
    for reason, count in sorted(report["histogram"].items(),
                                key=lambda kv: (-kv[1], kv[0])):
        print(f"  {reason:<24}{count:>6}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="CoinGecko discovery radar — read-only, zero trading impact.")
    p.add_argument("--once", action="store_true", help="single pass then exit")
    p.add_argument("--interval", nargs="?", const=3600, type=int, default=None,
                   metavar="SEC",
                   help="loop mode: one pass every SEC seconds (default 3600)")
    p.add_argument("--runtime-dir", default="runtime",
                   help="directory for radar_candidates.jsonl / jevelin.db")
    p.add_argument("--db", default=None,
                   help="store path (default: <runtime-dir>/jevelin.db)")
    args = p.parse_args(argv)

    def _one() -> int:
        report = run_once(runtime_dir=args.runtime_dir, db_path=args.db)
        print_report(report)
        return 0

    if args.interval is None:
        try:
            return _one()
        except RadarError as exc:
            print(f"radar error: {exc}", file=sys.stderr)
            return 1
    while True:  # loop mode: any API error exits non-zero (supervisor restarts)
        try:
            _one()
        except RadarError as exc:
            print(f"radar error: {exc}", file=sys.stderr)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())