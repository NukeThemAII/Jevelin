#!/usr/bin/env python3
"""jev_scout — M6 CoinGecko discovery scout (B.4a). Discovery only, no trading.

CoinGecko tells us WHERE to look, never WHAT to do; every eventual trade still
goes through the full M5 gate stack unchanged.

One scout pass (``jev_scout.py --once``, the default with no args):
  1. GET /search/trending -> up to 7 trending coin ids     (CoinGecko, Pool A)
  2. GET /coins/markets?vs_currency=usd&ids=... -> 24h volume, 24h price
     change %, market cap per Pool A candidate                     (CoinGecko)
  3. GET /coins/markets?vs_currency=usd&order=volume_desc&per_page=250&page=1
     -> Pool B scan width; the client-side top movers by |24h change| form
     the pool (the live free API IGNORES the price-change ``order`` values —
     verified 2026-09-28 — so the M6+ spec fallback is in effect; junk-
     density is LOWER THAN IDEAL: liquid majors, not micro-cap pumps).
     Exactly ONE extra GET per pass; on HTTP failure Pool B is
     dropped with a logged error and the pass stays valid (Pool A only).
     ``gainers_enabled: false`` skips it (Pool A only = M6 behavior).
  4. Candidates = Pool A u Pool B deduped by CoinGecko id: a coin in both
     pools counts ONCE (``seed=trending+gainers``; tags ``trending`` /
     ``gainers`` mark single-pool coins).
  5. Map coins to Binance <BASE>/<quote> spot pairs via ccxt fetch_tickers()
     keys; a coin with no Binance pair is rejected ``not_binance_listed``.
  6. Filters (config/v2.yaml ``scout:``): Binance 24h quote volume >=
     min_24h_vol_usd else ``low_volume``; listing age >= min_listing_age_days
     (first daily candle via fetch_ohlcv(symbol, '1d', since=0, limit=2)) else
     ``young_listing``. Seeds without usable CoinGecko market data (missing
     row / symbol / ranking inputs) -> ``trending_only``.
  7. Deterministic rank of the survivors:
     candidate_score = 0.5 * vol_rank_norm
                     + 0.5 * |price_change_24h|_rank_norm
     (rank-normalized 0..1 across survivors; exact ties broken
     lexicographically by symbol — identical inputs always produce the
     identical ranked list).
  8. HARD GATE (M6+ acceptance #1): every selected pair must clear the
     liquidity + age floors — asserted at selection time. A selected floor
     violation is a pipeline bug and fails the pass LOUDLY
     (ScoutHardGateError -> ok=0, last good list kept).
  9. Cap at max_pairs (qualified-but-capped survivors keep ``over_cap``), then
     persist ONE scout_runs row per pass (timestamp, raw input hashes, full
     candidate table incl. rejects + reasons, final ranked list) and cache
     every raw input under runtime/scout_cache/<run_id>-<endpoint>.json.

Metrics (the --once funnel when Pool B is active) report the pools
SEPARATELY (trending= / gainers= / unique_candidates=) plus:

  junk-rejection rate = junk rejects / unique candidates

where junk rejects are the FILTER rejects (trending_only /
not_binance_listed / low_volume / young_listing). ``over_cap`` is a
qualified-but-capped survivor — NOT junk (see REJECT_OVER_CAP) — and never
counts toward junk rejection (counting it would turn the metric into cap
arithmetic). filter correctness = YES iff every selected pair passes the
vol+age floors (the hard gate). Both numbers are reported honestly — below
90% is reported as-is, never engineered.

Reproducibility: ``jev_scout.py --replay <pass-ts>`` re-runs the pipeline from
the cached raw inputs ONLY (zero network) and reproduces the byte-identical
ranked list.

Fail-safe (this touches the pair universe = the money path): any fetch/parse
error -> the pass is recorded ok=0, the failure is logged, and the last good
list is kept. The scout can never crash or block the trading loop. Network
calls happen ONLY here (run_scout_pass / http_get); every unit test is offline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_store  # noqa: E402
from jev_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    ConfigError,
    ScoutConfig,
    load_config,
)

CG_BASE = "https://api.coingecko.com/api/v3"
USER_AGENT = "Jevelin-scout/1.0 (+https://github.com/NukeThemAII/Jevelin)"
DEFAULT_CACHE_DIR = Path("runtime/scout_cache")
MAX_TRENDING_SEEDS = 7
DAY_MS = 86_400_000.0
# Pool B (M6+) market scan. Live free-API reality (verified in the M6+ smoke,
# 2026-09-28): /coins/markets IGNORES order=price_change_percentage_24h_desc
# and _asc (returns the default market-cap order) while order=volume_desc is
# honored. The M6+ spec fallback scan is therefore in effect: fetch the top
# 250 coins by volume, keep the client-side top 50 wildest movers by |24h
# change|. Junk-density is LOWER THAN IDEAL (liquid majors, not the illiquid
# micro-cap pumps of a true gainers list) — stated in the smoke report.
GAINERS_ORDER = "volume_desc"
GAINERS_FETCH_PER_PAGE = 250    # fallback fetch width (spec)
GAINERS_TOP_BY_CHANGE = 50      # fallback kept pool: top movers by |change|

# Machine-readable reject reasons (one per rejected coin).
REJECT_TRENDING_ONLY = "trending_only"
REJECT_NOT_BINANCE_LISTED = "not_binance_listed"
REJECT_LOW_VOLUME = "low_volume"
REJECT_YOUNG_LISTING = "young_listing"
REJECT_OVER_CAP = "over_cap"    # qualified survivor beyond max_pairs (not junk)
REJECT_REASONS = (REJECT_TRENDING_ONLY, REJECT_NOT_BINANCE_LISTED,
                  REJECT_LOW_VOLUME, REJECT_YOUNG_LISTING, REJECT_OVER_CAP)


class ScoutFetchError(Exception):
    """HTTP layer failure (after retries): 429/5xx/connection error."""


class ScoutDataError(Exception):
    """Malformed or missing input data (bad JSON, missing cache, bad run id)."""


class ScoutHardGateError(ScoutDataError):
    """HARD-GATE violation: a selected pair fails the liquidity/age floors.

    A selected floor violation is a pipeline bug — the pass fails loudly
    (ok=0, last good list kept) instead of quietly trading below a floor.
    """


# -- pass identity + canonical serialization ---------------------------------

def run_id_for(ts) -> str:
    """Pass id = UTC timestamp token, e.g. '20260928T101500Z' (cache key)."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")


def ts_for_run_id(run_id) -> float:
    try:
        return datetime.strptime(str(run_id), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise ScoutDataError(
            f"bad pass timestamp {run_id!r} (expected YYYYMMDDTHHMMSSZ)"
        ) from exc


def candidates_json_text(candidates) -> str:
    """Canonical candidates serialization (byte-identical across runs)."""
    return json.dumps(candidates, sort_keys=True, separators=(",", ":"),
                      default=str)


def ranked_json_text(ranked) -> str:
    """Canonical ranked-list serialization (byte-identical across runs)."""
    return json.dumps(ranked, sort_keys=True, separators=(",", ":"),
                      default=str)


# -- HTTP layer (the ONLY network code) --------------------------------------

def http_get(url, params=None, session=None, sleep=time.sleep, retries=3,
             backoff=1.0, timeout=20.0) -> str:
    """GET with an honest UA and exponential backoff. Raises ScoutFetchError.

    ``retries`` attempts total; 429/5xx and connection errors are retried
    with ``backoff * 2**attempt`` seconds between attempts, other 4xx fail
    fast. ``session`` duck-types requests (tests inject a fake).
    """
    sess = session if session is not None else requests
    last = None
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            resp = sess.get(url, params=params, timeout=timeout,
                            headers={"User-Agent": USER_AGENT,
                                     "Accept": "application/json"})
        except Exception as exc:  # connection-level trouble: retryable
            last = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                return resp.text
            last = f"HTTP {resp.status_code}"
            if resp.status_code != 429 and resp.status_code < 500:
                break             # non-retryable client error
        if attempt < attempts - 1:
            sleep(float(backoff) * (2.0 ** attempt))
    raise ScoutFetchError(
        f"GET {url} failed after {attempts} attempts ({last})")

# -- raw-input cache (replay source of truth) ---------------------------------

def _cache_path(cache_dir, run_id, endpoint) -> Path:
    return Path(cache_dir) / f"{run_id}-{endpoint}.json"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_cache(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_cache(cache_dir, run_id) -> dict:
    """All raw inputs of one pass, exactly as cached (zero network)."""
    cache_dir = Path(cache_dir)

    def read(endpoint):
        path = _cache_path(cache_dir, run_id, endpoint)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScoutDataError(
                f"no cached raw input for {endpoint!r} (pass {run_id}): {exc}"
            ) from exc

    raws = {"search_trending": read("search_trending"),
            "coins_markets": read("coins_markets"),
            "coins_markets_gainers": None,
            "binance_tickers": read("binance_tickers"),
            "binance_ohlcv": {}}
    # Pool B raw is optional: passes before M6+ (or with a failed gainers
    # fetch) have none — those ran Pool A only and must replay as such.
    gainers_path = _cache_path(cache_dir, run_id, "coins_markets_gainers")
    if gainers_path.exists():
        raws["coins_markets_gainers"] = gainers_path.read_text(encoding="utf-8")
    prefix = f"{run_id}-binance_ohlcv_"
    for path in sorted(cache_dir.glob(f"{prefix}*.json")):
        sym = path.name[len(prefix):-len(".json")]
        raws["binance_ohlcv"][sym] = path.read_text(encoding="utf-8")
    return raws


# -- parsing (pure; malformed input -> ScoutDataError) -----------------------

def _parse_json(text, what):
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ScoutDataError(f"malformed JSON in {what}: {exc}") from exc


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_trending(text) -> list:
    """search/trending body -> ordered seed dicts (id/symbol/name), cap 7."""
    data = _parse_json(text, "coingecko search/trending")
    coins = data.get("coins") if isinstance(data, dict) else None
    if not isinstance(coins, list):
        raise ScoutDataError(
            "malformed JSON in coingecko search/trending: missing coins[]")
    seeds = []
    for raw in coins[:MAX_TRENDING_SEEDS]:
        item = raw.get("item") if isinstance(raw, dict) else None
        item = item if isinstance(item, dict) else {}
        seeds.append({"id": item.get("id"), "symbol": item.get("symbol"),
                      "name": item.get("name")})
    return seeds


def parse_gainers(text, limit=None) -> list:
    """Pool B coins/markets body -> ordered seed dicts (id/symbol/name).

    The M6+ market-movers scan (spec fallback: the live free API ignores the
    price-change ``order`` values, so the selection is client-side): rows
    ranked by |price_change_percentage_24h| desc — dumpers count like gainers
    (same junk profile) — exact ties broken by CoinGecko id (deterministic),
    capped at ``limit``. Rows without a 24h change cannot rank and sort
    last (kept, never silently dropped — the pool denominator stays honest).
    """
    data = _parse_json(text, "coingecko coins/markets (gainers)")
    if not isinstance(data, list):
        raise ScoutDataError(
            "malformed JSON in coingecko coins/markets (gainers): "
            "expected a list")
    rows = [row if isinstance(row, dict) else {} for row in data]

    def mover_key(row):
        change = _number(row.get("price_change_percentage_24h"))
        magnitude = abs(change) if change is not None else -1.0
        return (-magnitude, str(row.get("id")))

    ordered = sorted(rows, key=mover_key)
    if limit is not None:
        ordered = ordered[:int(limit)]
    return [{"id": row.get("id"), "symbol": row.get("symbol"),
             "name": row.get("name")} for row in ordered]


def parse_markets(text) -> dict:
    """coins/markets body -> {coin_id: raw row}."""
    data = _parse_json(text, "coingecko coins/markets")
    if not isinstance(data, list):
        raise ScoutDataError(
            "malformed JSON in coingecko coins/markets: expected a list")
    return {row["id"]: row for row in data
            if isinstance(row, dict) and row.get("id") is not None}


def _rank_norms(pairs) -> dict:
    """(symbol, value) -> rank-normalized 0..1, ascending.

    Deterministic by construction: values ascending, exact ties ordered by
    symbol; a lone survivor scores 1.0 (it is the maximum).
    """
    order = sorted(pairs, key=lambda kv: (kv[1], kv[0]))
    n = len(order)
    return {sym: (idx / (n - 1) if n > 1 else 1.0)
            for idx, (sym, _value) in enumerate(order)}


# -- M6+ hard gate: filter correctness (acceptance #1, must be 100%) ----------

def filter_floor_violations(entry, cfg) -> list:
    """Floor violations of one selected candidate ([] = clears both floors).

    Missing/NaN values violate the floor: they cannot prove it is met.
    """
    violations = []
    vol = _number(entry.get("binance_quote_volume_usd"))
    if vol is None or not vol >= float(cfg.min_24h_vol_usd):
        violations.append(f"24h volume {entry.get('binance_quote_volume_usd')!r} "
                          f"< floor {cfg.min_24h_vol_usd}")
    age = _number(entry.get("listing_age_days"))
    if age is None or not age >= float(cfg.min_listing_age_days):
        violations.append(f"listing age {entry.get('listing_age_days')!r}d "
                          f"< floor {cfg.min_listing_age_days}d")
    return violations


def filter_correctness(selected, cfg) -> bool:
    """True iff EVERY selected pair clears the liquidity + age floors."""
    return all(not filter_floor_violations(e, cfg) for e in selected)


def assert_filter_correctness(selected, cfg) -> None:
    """HARD GATE (M6+ acceptance #1): fail loudly on a selected floor violation.

    A selected pair violating a floor is a pipeline bug — never accept it
    quietly. Raises ScoutHardGateError naming the pair and the violation.
    """
    for e in selected:
        violations = filter_floor_violations(e, cfg)
        if violations:
            raise ScoutHardGateError(
                f"filter-correctness gate: selected pair {e.get('symbol')!r} "
                f"violates {'; '.join(violations)} — a selected floor "
                f"violation is a bug; failing the pass loudly")

# -- the deterministic pipeline (pure; identical inputs -> identical output) --

def evaluate_pass(raws, pass_ts, cfg, ohlcv_loader=None) -> dict:
    """Raw inputs -> candidate table + ranked list + funnel. No I/O except the
    optional ``ohlcv_loader(pair) -> text`` (live pass fetches+caches missing
    age inputs through it; replay pre-loads everything from the cache).

    raws: {"search_trending": text, "coins_markets": text,
           "coins_markets_gainers": text or absent (Pool B, M6+),
           "binance_tickers": text, "binance_ohlcv": {BASEQUOTE: text}}
    pass_ts: epoch SECONDS of the pass (age math; recorded per pass, so
    --replay is bit-exact). Raises ScoutDataError on malformed inputs.
    """
    seeds_a = parse_trending(raws["search_trending"])
    markets = parse_markets(raws["coins_markets"])
    gainers_text = raws.get("coins_markets_gainers")
    # Pool B active <=> gainers enabled AND a Pool B raw exists (a failed
    # gainers fetch leaves no raw -> Pool A only -> replays identically).
    pool_b_active = bool(cfg.gainers_enabled) and gainers_text is not None
    if pool_b_active:
        seeds_b = parse_gainers(gainers_text,
                                min(int(cfg.gainers_per_page),
                                    GAINERS_TOP_BY_CHANGE))
        merged = parse_markets(gainers_text)   # Pool B supplies its own rows
        merged.update(markets)                 # Pool A row wins (deterministic)
        markets = merged
    else:
        seeds_b = []
    tickers = _parse_json(raws["binance_tickers"], "binance tickers")
    if not isinstance(tickers, dict):
        raise ScoutDataError(
            "malformed JSON in binance tickers: expected an object")
    ohlcv_raws = dict(raws.get("binance_ohlcv") or {})
    quote = str(cfg.quote).upper()

    # Pool A u Pool B deduped by CoinGecko id (a coin in both pools -> ONE
    # candidate, tagged seed=trending+gainers; order: Pool A first, then the
    # Pool B-only remainder in scan order — deterministic by construction).
    seeds = []
    seen = {}

    def add_seed(seed, tag):
        sid = seed.get("id")
        stored = [seed, tag]              # mutable: the tag can widen later
        if sid is not None:
            if sid in seen:
                prior = seen[sid]
                if prior[1] != tag:
                    prior[1] = "trending+gainers"
                return
            seen[sid] = stored
        seeds.append(stored)

    for seed in seeds_a:
        add_seed(seed, "trending")
    for seed in seeds_b:
        add_seed(seed, "gainers")

    candidates = []
    survivors = []
    for seed, seed_tag in seeds:
        entry = {"coingecko_id": seed["id"], "name": seed["name"],
                 "cg_symbol": seed["symbol"], "symbol": None,
                 "selected": False, "reject_reason": None,
                 "cg_volume_24h_usd": None, "price_change_24h_pct": None,
                 "market_cap_usd": None, "binance_quote_volume_usd": None,
                 "listing_age_days": None, "candidate_score": None,
                 "vol_rank_norm": None, "change_rank_norm": None,
                 "rank": None}
        if pool_b_active:
            entry["seed"] = seed_tag
        candidates.append(entry)

        market = markets.get(seed["id"]) if seed["id"] else None
        volume = _number((market or {}).get("total_volume"))
        change = _number((market or {}).get("price_change_percentage_24h"))
        if market is None or not seed["symbol"] \
                or volume is None or change is None:
            entry["reject_reason"] = REJECT_TRENDING_ONLY
            continue
        entry["cg_volume_24h_usd"] = volume
        entry["price_change_24h_pct"] = change
        entry["market_cap_usd"] = _number(market.get("market_cap"))

        base = str(seed["symbol"]).upper()
        entry["symbol"] = f"{base}{quote}"
        ticker = tickers.get(f"{base}/{quote}")
        if not isinstance(ticker, dict):
            entry["reject_reason"] = REJECT_NOT_BINANCE_LISTED
            continue
        quote_volume = _number(ticker.get("quoteVolume")) or 0.0
        entry["binance_quote_volume_usd"] = quote_volume
        if quote_volume < float(cfg.min_24h_vol_usd):
            entry["reject_reason"] = REJECT_LOW_VOLUME
            continue

        if entry["symbol"] not in ohlcv_raws and ohlcv_loader is not None:
            ohlcv_raws[entry["symbol"]] = ohlcv_loader(f"{base}/{quote}")
        candles = None
        if ohlcv_raws.get(entry["symbol"]):
            candles = _parse_json(ohlcv_raws[entry["symbol"]],
                                  f"binance ohlcv {entry['symbol']}")
        first_ts = None
        if isinstance(candles, list) and candles \
                and isinstance(candles[0], list) and candles[0]:
            first_ts = _number(candles[0][0])
        if first_ts is None:
            entry["reject_reason"] = REJECT_YOUNG_LISTING  # age unverifiable
            continue
        age_days = (float(pass_ts) * 1000.0 - first_ts) / DAY_MS
        entry["listing_age_days"] = age_days
        if age_days < float(cfg.min_listing_age_days):
            entry["reject_reason"] = REJECT_YOUNG_LISTING
            continue
        survivors.append(entry)

    # deterministic candidate score over the survivors
    vol_norms = _rank_norms([(e["symbol"], e["cg_volume_24h_usd"])
                             for e in survivors])
    chg_norms = _rank_norms(
        [(e["symbol"], abs(e["price_change_24h_pct"])) for e in survivors])
    for e in survivors:
        e["vol_rank_norm"] = vol_norms[e["symbol"]]
        e["change_rank_norm"] = chg_norms[e["symbol"]]
        e["candidate_score"] = 0.5 * e["vol_rank_norm"] \
            + 0.5 * e["change_rank_norm"]
    ordered = sorted(survivors,
                     key=lambda e: (-e["candidate_score"], e["symbol"]))
    ranked = ordered[:int(cfg.max_pairs)]
    for i, e in enumerate(ranked):
        e["selected"] = True
        e["rank"] = i + 1
    for e in ordered[int(cfg.max_pairs):]:
        e["reject_reason"] = REJECT_OVER_CAP

    reject_reasons = {}
    for e in candidates:
        if e["reject_reason"]:
            reject_reasons[e["reject_reason"]] = \
                reject_reasons.get(e["reject_reason"], 0) + 1
    rejected = sum(reject_reasons.values())
    mapped = sum(1 for e in candidates if e["symbol"] is not None)
    # M6+ HARD GATE (acceptance #1): a selected pair violating a floor is a
    # pipeline bug — fail the pass loudly (never select quietly below floor).
    assert_filter_correctness(ranked, cfg)
    if pool_b_active:
        # M6+ honest junk-rejection metric: FILTER rejects only. over_cap is a
        # qualified-but-capped survivor (NOT junk) and never counts toward it —
        # counting it would make the metric mechanical cap arithmetic.
        junk_rejected = rejected - reject_reasons.get(REJECT_OVER_CAP, 0)
        funnel = {"trending": len(seeds_a),
                  "gainers": len(seeds_b),
                  "unique_candidates": len(candidates),
                  "mapped": mapped,
                  "filtered": len(survivors),
                  "ranked": len(ranked),
                  "rejected": rejected,
                  "reject_reasons": reject_reasons,
                  "junk_rejected": junk_rejected,
                  "junk_rejection_rate":
                      (junk_rejected / len(candidates)) if candidates else 0.0,
                  "filter_correctness": filter_correctness(ranked, cfg)}
    else:
        funnel = {"trending": len(seeds_a),
                  "mapped": mapped,
                  "filtered": len(survivors),
                  "ranked": len(ranked),
                  "rejected": rejected,
                  "reject_reasons": reject_reasons,
                  "rejected_junk_rate":
                      (rejected / len(seeds_a)) if seeds_a else 0.0}
    return {"candidates": candidates, "ranked": ranked, "funnel": funnel}

# -- one scout pass (network) + offline replay -------------------------------

def _default_exchange():
    import ccxt  # local import: only a real pass needs the exchange handle

    return ccxt.binance({"enableRateLimit": True})


def run_scout_pass(cfg=None, *, fetcher=None, exchange=None, now=None,
                   cache_dir=None, conn=None, quiet=True) -> dict:
    """Run ONE scout pass. Never raises: fetch/parse errors -> ok=False record.

    fetcher(url, params) -> response text (default http_get; tests inject).
    exchange duck-types ccxt (fetch_tickers/fetch_ohlcv; default: Binance).
    Every raw input is cached under cache_dir as <run_id>-<endpoint>.json and
    hashed into the record, so the pass is replayable offline. A failed pass
    (ok=0) is recorded for audit but NEVER replaces the last good list.
    """
    cfg = cfg if cfg is not None else ScoutConfig()
    fetcher = fetcher if fetcher is not None else http_get
    cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    pass_ts = float(now) if now is not None else time.time()
    run_id = run_id_for(pass_ts)
    # Snap to the pass token's second resolution: --replay re-derives pass_ts
    # from run_id, and the pass MUST be byte-identical offline (M6 acceptance).
    pass_ts = ts_for_run_id(run_id)
    raws = {"search_trending": None, "coins_markets": None,
            "coins_markets_gainers": None,
            "binance_tickers": None, "binance_ohlcv": {}}
    raw_hashes = {}
    record = {"run_id": run_id, "ts": pass_ts, "ok": False, "error": None,
              "pool_b_error": None, "raw_hashes": raw_hashes, "funnel": None,
              "candidates": None, "ranked": None,
              "candidates_json": None, "ranked_json": None}

    def take(endpoint, text):
        text = str(text)
        _write_cache(_cache_path(cache_dir, run_id, endpoint), text)
        raw_hashes[endpoint] = _sha256(text)
        return text

    try:
        if not cfg.enabled:
            raise ScoutDataError(
                "scout disabled in config (scout.enabled=false)")
        raws["search_trending"] = take(
            "search_trending", fetcher(f"{CG_BASE}/search/trending", None))
        ids = [s["id"] for s in parse_trending(raws["search_trending"])
               if s.get("id")]
        raws["coins_markets"] = take(
            "coins_markets",
            fetcher(f"{CG_BASE}/coins/markets",
                    {"vs_currency": "usd", "ids": ",".join(ids)}))
        parse_markets(raws["coins_markets"])   # fail early on malformed input
        if cfg.gainers_enabled:
            # Pool B (M6+): exactly ONE extra GET per pass. HTTP failure is
            # fail-safe: Pool A still works, the pass stays valid, the error
            # is logged (and the absent raw makes replay Pool-A-only too).
            try:
                raws["coins_markets_gainers"] = take(
                    "coins_markets_gainers",
                    fetcher(f"{CG_BASE}/coins/markets",
                            {"vs_currency": "usd", "order": GAINERS_ORDER,
                             "per_page": str(GAINERS_FETCH_PER_PAGE),
                             "page": "1"}))
            except ScoutFetchError as exc:
                record["pool_b_error"] = f"{type(exc).__name__}: {exc}"
                if not quiet:
                    print(f"scout: gainers pool fetch failed: "
                          f"{record['pool_b_error']} (Pool A only)")
        if exchange is None:
            exchange = _default_exchange()
        raws["binance_tickers"] = take(
            "binance_tickers",
            json.dumps(exchange.fetch_tickers(), sort_keys=True, default=str))

        def ohlcv_loader(pair):
            bsym = pair.replace("/", "")
            text = take(f"binance_ohlcv_{bsym}",
                        json.dumps(exchange.fetch_ohlcv(pair, "1d", since=0,
                                                        limit=2),
                                   sort_keys=True, default=str))
            raws["binance_ohlcv"][bsym] = text
            return text

        out = evaluate_pass(raws, pass_ts, cfg, ohlcv_loader=ohlcv_loader)
        record.update(
            ok=True,
            funnel=out["funnel"], candidates=out["candidates"],
            ranked=out["ranked"],
            candidates_json=candidates_json_text(out["candidates"]),
            ranked_json=ranked_json_text(out["ranked"]))
    except Exception as exc:  # fail-safe: log + keep the last good list
        record["error"] = f"{type(exc).__name__}: {exc}"
        if not quiet:
            print(f"scout: pass {run_id} failed: {record['error']} "
                  f"(keeping last good list)")

    try:
        if conn is not None:
            jev_store.insert_scout_run(conn, {
                "run_id": record["run_id"], "ts": record["ts"],
                "ok": 1 if record["ok"] else 0,
                "error": record["error"],
                "raw_hashes_json": json.dumps(record["raw_hashes"],
                                              sort_keys=True),
                "candidates_json": record["candidates_json"],
                "ranked_json": record["ranked_json"]})
    except Exception as exc:  # logging must never break the pass
        if not quiet:
            print(f"scout: store write error: {type(exc).__name__}: {exc}")
    return record


def replay_pass(run_id, *, cfg=None, cache_dir=None, conn=None) -> dict:
    """Reproduce one pass from its cached raw inputs ONLY (zero network).

    Returns the pass record plus ``match_stored``: True/False against the
    stored scout_runs row (byte-identical ranked list), None when no stored
    row exists. Raises ScoutDataError when the cache is incomplete.
    """
    cfg = cfg if cfg is not None else ScoutConfig()
    cache_dir = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    pass_ts = ts_for_run_id(run_id)
    out = evaluate_pass(_load_cache(cache_dir, run_id), pass_ts, cfg)
    candidates_json = candidates_json_text(out["candidates"])
    ranked_json = ranked_json_text(out["ranked"])
    match_stored = None
    if conn is not None:
        try:
            row = jev_store.get_scout_run(conn, run_id)
        except Exception:
            row = None
        if row is not None and row.get("ok"):
            match_stored = (row.get("ranked_json") == ranked_json
                            and row.get("candidates_json") == candidates_json)
    return {"run_id": run_id, "ts": pass_ts, "ok": True, "error": None,
            "funnel": out["funnel"], "candidates": out["candidates"],
            "ranked": out["ranked"], "candidates_json": candidates_json,
            "ranked_json": ranked_json, "match_stored": match_stored}

# -- human/machine output + CLI ----------------------------------------------

def format_report(record) -> str:
    """Funnel table (pools -> mapped -> filtered -> ranked) + both metrics.

    Pool B active: pools SEPARATELY + junk-rejection rate (unique candidates
    denominator) + the hard filter-correctness line. Pool A only: the exact
    M6 report (byte-for-byte compatibility).
    """
    lines = []
    stamp = "?"
    if record.get("ts") is not None:
        stamp = datetime.fromtimestamp(float(record["ts"]),
                                       tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"scout pass {record.get('run_id')} ({stamp})")
    if not record.get("ok"):
        lines.append(f"FAILED: {record.get('error')} (last good list kept)")
        return "\n".join(lines)
    lines.append(f"{'coin':<12} {'symbol':<10} {'status':<8} {'rank':>4} "
                 f"{'reject_reason':<20} {'vol24h$':>12} {'chg24h%':>9} "
                 f"{'age_d':>7} {'score':>8}")
    for c in record["candidates"]:
        status = "selected" if c["selected"] else "rejected"
        reason = c["reject_reason"] or "-"
        rank = "-" if c["rank"] is None else str(c["rank"])
        vol = "-" if c["cg_volume_24h_usd"] is None \
            else f"{c['cg_volume_24h_usd']:.0f}"
        chg = "-" if c["price_change_24h_pct"] is None \
            else f"{c['price_change_24h_pct']:.2f}"
        age = "-" if c["listing_age_days"] is None \
            else f"{c['listing_age_days']:.1f}"
        score = "-" if c["candidate_score"] is None \
            else f"{c['candidate_score']:.6f}"
        lines.append(f"{(c['cg_symbol'] or '?'):<12} "
                     f"{(c['symbol'] or '-'):<10} {status:<8} {rank:>4} "
                     f"{reason:<20} {vol:>12} {chg:>9} {age:>7} {score:>8}")
    funnel = record["funnel"]
    if "unique_candidates" in funnel:      # M6+ widened pool (Pool B active)
        lines.append(f"funnel: trending={funnel['trending']} "
                     f"gainers={funnel['gainers']} "
                     f"unique_candidates={funnel['unique_candidates']} "
                     f"mapped={funnel['mapped']} "
                     f"filtered={funnel['filtered']} "
                     f"ranked={funnel['ranked']}")
        if funnel["reject_reasons"]:
            lines.append("reject reasons: " + " ".join(
                f"{k}={v}" for k, v in sorted(funnel["reject_reasons"].items())))
        lines.append(
            f"junk-rejection rate: {funnel['junk_rejected']}/"
            f"{funnel['unique_candidates']} = "
            f"{100.0 * funnel['junk_rejection_rate']:.1f}% "
            f"(rejects / unique candidates)")
        verdict = "YES" if funnel.get("filter_correctness") else "NO"
        lines.append(f"filter correctness: all {funnel['ranked']} selected "
                     f"pass vol+age floors: {verdict}   <- hard gate")
    else:                                  # Pool A only (M6 report, verbatim)
        lines.append(f"funnel: trending={funnel['trending']} "
                     f"mapped={funnel['mapped']} filtered={funnel['filtered']} "
                     f"ranked={funnel['ranked']}")
        if funnel["reject_reasons"]:
            lines.append("reject reasons: " + " ".join(
                f"{k}={v}" for k, v in sorted(funnel["reject_reasons"].items())))
        lines.append(
            f"rejected-junk rate: {funnel['rejected']}/{funnel['trending']} = "
            f"{100.0 * funnel['rejected_junk_rate']:.1f}% "
            f"(rejects / trending seeds)")
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Jevelin M6 CoinGecko discovery scout — discovery only, "
                    "never trading. CoinGecko tells us WHERE to look.")
    p.add_argument("--once", action="store_true",
                   help="run one scout pass (the default)")
    p.add_argument("--replay", metavar="PASS_TS", default=None,
                   help="reproduce a pass from its cached raw inputs "
                        "(offline, byte-identical)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                   help="config/v2.yaml path (scout thresholds)")
    p.add_argument("--cache-dir", default=None,
                   help="raw response cache (default: runtime/scout_cache)")
    p.add_argument("--db", default=None,
                   help="store path (default: runtime/jevelin.db)")
    p.add_argument("--max-pairs", type=int, default=None,
                   help="scout.max_pairs override (beats the yaml)")
    p.add_argument("--min-24h-vol-usd", type=float, default=None,
                   help="scout.min_24h_vol_usd override")
    p.add_argument("--min-listing-age-days", type=float, default=None,
                   help="scout.min_listing_age_days override")
    p.add_argument("--quote", default=None,
                   help="scout.quote override (Binance quote currency)")
    return p.parse_args(argv)

def scout_config_from_args(args, cfg) -> ScoutConfig:
    """SET CLI flags beat config/v2.yaml; unset flags keep the yaml value."""
    from dataclasses import replace

    updates = {}
    for key in ("max_pairs", "min_24h_vol_usd", "min_listing_age_days",
                "quote"):
        value = getattr(args, key, None)
        if value is not None:
            updates[key] = value
    return replace(cfg.scout, **updates) if updates else cfg.scout


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    scout_cfg = scout_config_from_args(args, cfg)
    if not scout_cfg.enabled:
        print("scout: disabled in config (scout.enabled=false)")
        return 2
    cache_dir = Path(args.cache_dir) if args.cache_dir else DEFAULT_CACHE_DIR
    conn = None
    try:
        conn = jev_store.connect(args.db or jev_store.DEFAULT_DB_PATH)
    except Exception as exc:  # a broken store must not break the scout
        print(f"scout: store open error: {type(exc).__name__}: {exc} "
              f"(continuing without store)")
        conn = None

    if args.replay:
        try:
            record = replay_pass(args.replay, cfg=scout_cfg,
                                 cache_dir=cache_dir, conn=conn)
        except (ScoutDataError, ScoutFetchError) as exc:
            print(f"scout: replay failed: {exc}")
            return 2
        if args.json:
            print(json.dumps(record, sort_keys=True, default=str))
            return 0
        print(format_report(record))
        if record["match_stored"] is True:
            print(f"replay: ranked list byte-identical to stored pass "
                  f"{record['run_id']}")
        elif record["match_stored"] is False:
            print(f"replay: MISMATCH vs stored pass {record['run_id']}")
            return 1
        else:
            print(f"replay: reproduced from cached raw inputs "
                  f"(no stored pass {record['run_id']} to compare)")
        return 0

    record = run_scout_pass(scout_cfg, cache_dir=cache_dir, conn=conn,
                            quiet=False)
    if args.json:
        print(json.dumps(record, sort_keys=True, default=str))
    else:
        print(format_report(record))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
