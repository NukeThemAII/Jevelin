#!/usr/bin/env python3
"""Jevelin supervisor CLI (M2+M3+M6) — split-cadence dual-book paper trading.

Entry point for scripts/jev_supervisor.py. Public Binance data + local JSON
state only: NEVER calls an exchange private API and NEVER places orders.

  --config config/v2.yaml       M3/M5 threshold source (scripts/jev_config.py)
  --pairs BTCUSDT[,ETHUSDT...]  pair universe (default: config pairs =
                                BTCUSDT,ETHUSDT,SOLUSDT); one spot+perps book
                                pair per symbol (BTC keeps paper_btc.json)
  --universe static|scout       M6 pair-universe source (default: static).
                                scout = latest CoinGecko scout pass list,
                                re-read at slow-cycle boundaries; stale
                                (> 2x scout interval), empty or broken scout
                                data falls back to the static universe (logged)
  --fast-interval 5             fast risk loop seconds (marks, stops/liq, flags)
  --slow-interval 300           slow Jev loop seconds (+ burst trigger)
  --burst-threshold 0.003       |1-min return| above this forces a score
  --burst-trades 150            1-min trade count above this forces a score
  --burst-cycles 2              extra forced slow cycles after a spike
  --cache-min-move 0.0005       price move (5 bp) below this reuses the verdict
  --cache-ttl 1800              verdict reuse window (s) before a re-score
  --fee-rate/--slippage-rate    M0 execution-cost overrides (beat the yaml)
  --whipsaw                     entry_max_whipsaw override (beats the yaml)
  --once                        one slow cycle + fast ticks, then exit
  --max-cycles N                bounded live run (N slow cycles)

Flags that are SET override config/v2.yaml at startup; unset flags fall back
to the yaml (which itself falls back to the dataclass defaults). Every applied
config is recorded in the store's config_versions table.

Example: .venv/bin/python scripts/jevelin_supervisor.py --once
         .venv/bin/python scripts/jevelin_supervisor.py --max-cycles 12
         .venv/bin/python scripts/jevelin_supervisor.py --once --universe scout
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_store  # noqa: E402
from jev_cache import DecisionCache  # noqa: E402
from jev_client import JevClient, load_api_key  # noqa: E402
from jev_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    ConfigError,
    ScoutConfig,
    load_config,
)
from jev_paper import PaperPortfolio  # noqa: E402
from jev_perps import PerpsPortfolio  # noqa: E402
from jev_supervisor import BookPair, MarketData, Supervisor  # noqa: E402


def symbol_tag(symbol: str) -> str:
    """BTCUSDT -> 'btc' (matches the v1 state file names, e.g. paper_btc.json)."""
    s = str(symbol).upper()
    return s[:-4].lower() if s.endswith("USDT") else s.lower()


def build_books(args, symbols, cfg) -> dict:
    """Books from the resolved config (M3): fees/slippage/thresholds = cfg values."""
    books = {}
    for symbol in symbols:
        tag = symbol_tag(symbol)
        spot = PaperPortfolio(
            initial_equity_usd=args.initial_equity,
            state_path=str(Path(args.runtime_dir) / f"paper_{tag}.json"),
            fee_rate=cfg.execution.spot_fee_rate,
            slippage_rate=cfg.execution.slippage_rate)
        spot.load()  # resume from prior state when present
        perps = None
        if args.perps:
            perps = PerpsPortfolio(
                initial_equity_usd=args.initial_equity,
                state_path=str(Path(args.runtime_dir) / f"perps_{tag}.json"),
                cfg=cfg.perps)
            perps.load()
        books[symbol] = BookPair(spot=spot, perps=perps)
    return books


def build_funding_exchange():
    """Public USD-M futures handle for funding rates. None on failure (fail-open)."""
    try:
        import ccxt  # noqa: WPS433

        return ccxt.binanceusdm({"enableRateLimit": True})
    except Exception:
        return None


# -- M6 scout universe (opt-in; discovery only, gates apply unchanged) --------

def resolve_scout_universe(conn, scout_cfg, static_pairs, now=None):
    """Latest good scout pass -> pair universe. Never raises (fail-open).

    Returns (symbols, source, detail): source is 'scout' or 'fallback_static'
    (the machine-readable log flag). Stale (> 2 x scout.interval_seconds),
    empty, malformed or missing scout data -> the static universe, logged.
    Failed scout passes (ok=0) never count: the last good list survives.
    """
    static = [str(s).strip().upper() for s in (static_pairs or ())]

    def fallback(detail):
        print(f"scout: universe fallback ({detail}) -> static "
              f"{','.join(static)}")
        return list(static), "fallback_static", detail

    if conn is None:
        return fallback("no store")
    try:
        row = jev_store.latest_scout_run(conn)
    except Exception as exc:
        return fallback(f"store error: {type(exc).__name__}: {exc}")
    if not row:
        return fallback("no scout pass yet")
    try:
        now_s = float(now) if now is not None else time.time()
        age = now_s - float(row["ts"])
        limit = 2.0 * float(scout_cfg.interval_seconds)
        if age > limit:
            return fallback(f"stale {age:.0f}s > {limit:.0f}s")
        ranked = json.loads(row.get("ranked_json") or "[]")
        symbols = []
        for entry in ranked:
            sym = str((entry or {}).get("symbol") or "").strip().upper()
            if sym and sym not in symbols:
                symbols.append(sym)
        if not symbols:
            return fallback("empty scout list")
    except Exception as exc:
        return fallback(f"bad scout pass: {type(exc).__name__}: {exc}")
    print(f"scout: universe from pass {row['run_id']}: {','.join(symbols)}")
    return symbols, "scout", str(row.get("run_id"))

def resolve_initial_universe(args, cfg, conn=None):
    """Startup universe for main(): (symbols, use_scout).

    Static mode (the default; --universe not given) NEVER touches the store
    and keeps the --pairs/config universe exactly as-is.
    """
    static = ([s.strip().upper() for s in args.pairs.split(",") if s.strip()]
              if args.pairs
              else [str(s).strip().upper() for s in cfg.pairs])
    if getattr(args, "universe", "static") != "scout":
        return static, False
    if not cfg.scout.enabled:
        print("scout: disabled in config (scout.enabled=false) "
              "-> static universe")
        return static, False
    symbols, source, detail = resolve_scout_universe(conn, cfg.scout, static)
    print(f"scout: initial universe source={source} ({detail}) "
          f"pairs={','.join(symbols)}")
    return symbols, True


class ScoutSupervisor(Supervisor):
    """M6 opt-in dynamic universe: the scout list is re-read at slow-cycle
    boundaries (the fast risk loop never waits on the scout).

    Safety rule (explicit + logged, never hidden): a pair that leaves the
    scout list while holding an open position stays in the managed set until
    it is flat, so its stops/liquidation/exits keep running. Entries on every
    pair remain subject to the unchanged gate stack — zero gate exceptions.
    """

    def __init__(self, *args, static_pairs=(), scout_cfg=None,
                 books_factory=None, **kwargs):
        self._cache_min_move = kwargs.get("cache_min_move", 0.0005)
        self._cache_ttl = kwargs.get("cache_ttl", 1800.0)
        self._cache_clock = kwargs.get("clock", time.time)
        super().__init__(*args, **kwargs)
        self.static_pairs = [str(s).strip().upper()
                             for s in (static_pairs or self.symbols)]
        self.scout_cfg = scout_cfg if scout_cfg is not None else ScoutConfig()
        self.books_factory = books_factory
        self.universe_source = "static"
        self.universe_detail = "startup"

    def _has_open_position(self, symbol) -> bool:
        pair = self.books.get(symbol)
        if pair is None:
            return False
        return any(book is not None
                   and getattr(book, "position", None) is not None
                   for book in (pair.spot, pair.perps))

    def refresh_universe(self) -> None:
        """Swap the managed symbol set at a slow-cycle boundary. Never raises."""
        try:
            symbols, source, detail = resolve_scout_universe(
                self.conn, self.scout_cfg, self.static_pairs, now=self._time())
            retained = [s for s in self.symbols
                        if s not in symbols and self._has_open_position(s)]
            target = list(symbols) + [s for s in retained if s not in symbols]
            if retained:
                print(f"scout: retained {','.join(retained)} until flat "
                      f"(open position) - stops/exits keep running")
            if target != list(self.symbols):
                new = [s for s in target if s not in self.books]
                if new and self.books_factory is not None:
                    self.books.update(self.books_factory(new))
                for s in target:
                    self.caches.setdefault(s, DecisionCache(
                        cache_min_move=self._cache_min_move,
                        cache_ttl=self._cache_ttl, clock=self._cache_clock))
                self.symbols = target
                if self.risk is not None:
                    self.risk.pairs = tuple(target)
                print(f"scout: universe -> {','.join(target)}")
            self.universe_source = source
            self.universe_detail = detail
        except Exception as exc:  # never crash or block the trading loop
            print(f"scout: universe refresh error: {type(exc).__name__}: {exc} "
                  f"(keeping current universe)")

    def slow_cycle(self) -> list:
        self.refresh_universe()
        return super().slow_cycle()



def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Jevelin split-cadence supervisor (M2): fast risk loop + "
                    "slow Jev loop + decision cache. Paper only, no orders.")
    p.add_argument("--pairs", default=None,
                   help="comma-separated market symbols (default: config pairs = "
                        "BTCUSDT,ETHUSDT,SOLUSDT)")
    p.add_argument("--universe", choices=("static", "scout"), default="static",
                   help="pair-universe source (M6): static = --pairs/config "
                        "pairs; scout = latest CoinGecko scout pass list, "
                        "re-read at slow-cycle boundaries (default: static)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                   help="config/v2.yaml path (M3 thresholds; dataclass defaults "
                        "when keys are omitted)")
    p.add_argument("--slow-interval", type=float, default=300.0,
                   help="seconds between slow Jev cycles (default: 300)")
    p.add_argument("--fast-interval", type=float, default=5.0,
                   help="seconds between fast risk ticks (default: 5)")
    p.add_argument("--burst-threshold", type=float, default=None,
                   help="|1-min return| forcing a score (default: config burst.")
    p.add_argument("--burst-trades", type=int, default=None,
                   help="1-min trade count forcing a score (default: config burst.)")
    p.add_argument("--burst-cycles", type=int, default=None,
                   help="extra forced slow cycles after a spike (default: config burst.)")
    p.add_argument("--cache-min-move", type=float, default=None,
                   help="price move below this reuses the verdict (default: config cache.)")
    p.add_argument("--cache-ttl", type=float, default=None,
                   help="verdict reuse window in seconds (default: config cache.)")
    p.add_argument("--whipsaw", type=float, default=None,
                   help="entry_max_whipsaw override for BOTH books (default: config)")
    p.add_argument("--fee-rate", type=float, default=None,
                   help="per-side fee rate override for BOTH books "
                        "(default: spot 0.001, perps taker 0.0005)")
    p.add_argument("--slippage-rate", type=float, default=None,
                   help="per-side slippage rate override for BOTH books "
                        "(default 0.0005; 0 = perfect limit fills)")
    p.add_argument("--once", action="store_true",
                   help="one slow cycle + fast ticks, then exit")
    p.add_argument("--max-cycles", type=int, default=None,
                   help="stop after N slow cycles (bounded live run)")
    p.add_argument("--initial-equity", type=float, default=10000.0,
                   help="starting paper equity in USD")
    p.add_argument("--runtime-dir", default="runtime",
                   help="state/JSONL/DB directory (default: runtime)")
    p.add_argument("--db", default=None,
                   help="store path (default: <runtime-dir>/jevelin.db)")
    p.add_argument("--perps", action=argparse.BooleanOptionalAction, default=True,
                   help="also drive the paper perps book (default: on)")
    return p.parse_args(argv)


def resolve_config(args):
    """config/v2.yaml + CLI overrides (set flags beat the yaml), never raises."""
    cfg = load_config(args.config)
    # Existing override pattern extended (M3): a SET flag beats the yaml value.
    if args.fee_rate is not None:
        cfg = replace(cfg,
                      execution=replace(cfg.execution,
                                        spot_fee_rate=args.fee_rate,
                                        perps_taker_fee_rate=args.fee_rate),
                      perps=replace(cfg.perps, taker_fee_rate=args.fee_rate))
    if args.slippage_rate is not None:
        cfg = replace(cfg,
                      execution=replace(cfg.execution,
                                        slippage_rate=args.slippage_rate),
                      perps=replace(cfg.perps, slippage_rate=args.slippage_rate))
    if args.whipsaw is not None:
        cfg = replace(cfg,
                      spot=replace(cfg.spot, entry_max_whipsaw=args.whipsaw),
                      perps=replace(cfg.perps, entry_max_whipsaw=args.whipsaw))
    return cfg


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = resolve_config(args)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    static_symbols = ([s.strip().upper() for s in args.pairs.split(",") if s.strip()]
                      if args.pairs
                      else [str(s).strip().upper() for s in cfg.pairs])
    if not static_symbols:
        print("error: --pairs produced no symbols")
        return 2
    api_key = load_api_key()  # OPENROUTER_API_KEY from repo-root .env
    client = JevClient(api_key)
    market = MarketData(ohlcv_ttl_seconds=cfg.market.ohlcv_ttl_seconds)
    db_path = args.db or str(Path(args.runtime_dir) / "jevelin.db")
    conn = jev_store.connect(db_path)
    try:  # M3: record the resolved config this run actually used
        jev_store.insert_config_version(conn, cfg.resolved_yaml_text())
    except Exception as exc:  # logging must never break trading
        print(f"store: config_versions write error: {type(exc).__name__}: {exc}")
    print(f"config: config_version={cfg.config_version} source={args.config}")
    # M6: --universe scout swaps in the latest scout pass list (opt-in only;
    # the static default universe is untouched when --universe is not given).
    symbols, use_scout = resolve_initial_universe(args, cfg, conn)
    books = build_books(args, symbols, cfg)
    funding_exchange = build_funding_exchange() if args.perps else None
    scout_kwargs = {}
    if use_scout:
        scout_kwargs = dict(
            static_pairs=static_symbols, scout_cfg=cfg.scout,
            books_factory=lambda syms: build_books(args, syms, cfg))
    sup_cls = ScoutSupervisor if use_scout else Supervisor
    sup = sup_cls(
        symbols, market, client, books, conn=conn,
        funding_exchange=funding_exchange,
        slow_interval=args.slow_interval, fast_interval=args.fast_interval,
        burst_threshold=args.burst_threshold
        if args.burst_threshold is not None else cfg.burst.burst_threshold,
        burst_trades=args.burst_trades
        if args.burst_trades is not None else cfg.burst.burst_trades,
        burst_cycles=args.burst_cycles
        if args.burst_cycles is not None else cfg.burst.burst_cycles,
        cache_min_move=args.cache_min_move
        if args.cache_min_move is not None else cfg.cache.cache_min_move,
        cache_ttl=args.cache_ttl
        if args.cache_ttl is not None else cfg.cache.cache_ttl,
        decision_log_path=str(Path(args.runtime_dir) / "paper_decisions.jsonl"),
        once=args.once, max_cycles=args.max_cycles,
        risk_cfg=cfg.spot, regime_cfg=cfg.regime, fanout_cfg=cfg.fanout,
        market_cfg=cfg.market,
        portfolio_cfg=cfg.portfolio,  # M5 portfolio risk layer
        risk_state_path=str(Path(args.runtime_dir) / "risk_state.json"),
        **scout_kwargs)
    asyncio.run(sup.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

