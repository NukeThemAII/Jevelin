#!/usr/bin/env python3
"""Jevelin supervisor CLI (M2) — split-cadence dual-book paper trading.

Entry point for scripts/jev_supervisor.py. Public Binance data + local JSON
state only: NEVER calls an exchange private API and NEVER places orders.

  --pairs BTCUSDT[,ETHUSDT...]   one spot+perps book pair per symbol
  --fast-interval 5              fast risk loop seconds (marks, stops/liq, flags)
  --slow-interval 300            slow Jev loop seconds (+ burst trigger)
  --burst-threshold 0.003        |1-min return| above this forces a score
  --burst-trades 150             1-min trade count above this forces a score
  --burst-cycles 2               extra forced slow cycles after a spike
  --cache-min-move 0.0005        price move (5 bp) below this reuses the verdict
  --cache-ttl 1800               verdict reuse window (s) before a re-score
  --fee-rate/--slippage-rate     M0 execution-cost overrides
  --once                         one slow cycle + fast ticks, then exit
  --max-cycles N                 bounded live run (N slow cycles)

Example: .venv/bin/python scripts/jevelin_supervisor.py --once
         .venv/bin/python scripts/jevelin_supervisor.py --max-cycles 12
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_store  # noqa: E402
from jev_client import JevClient, load_api_key  # noqa: E402
from jev_config import (  # noqa: E402
    PERPS_TAKER_FEE_RATE,
    SLIPPAGE_RATE,
    SPOT_FEE_RATE,
)
from jev_paper import PaperPortfolio  # noqa: E402
from jev_perps import PerpsConfig, PerpsPortfolio  # noqa: E402
from jev_supervisor import BookPair, MarketData, Supervisor  # noqa: E402


def symbol_tag(symbol: str) -> str:
    """BTCUSDT -> 'btc' (matches the v1 state file names, e.g. paper_btc.json)."""
    s = str(symbol).upper()
    return s[:-4].lower() if s.endswith("USDT") else s.lower()


def build_books(args, symbols, fee_rate, slippage_rate) -> dict:
    books = {}
    for symbol in symbols:
        tag = symbol_tag(symbol)
        spot = PaperPortfolio(
            initial_equity_usd=args.initial_equity,
            state_path=str(Path(args.runtime_dir) / f"paper_{tag}.json"),
            fee_rate=fee_rate, slippage_rate=slippage_rate)
        spot.load()  # resume from prior state when present
        perps = None
        if args.perps:
            perps_fee = args.fee_rate if args.fee_rate is not None \
                else PERPS_TAKER_FEE_RATE
            perps = PerpsPortfolio(
                initial_equity_usd=args.initial_equity,
                state_path=str(Path(args.runtime_dir) / f"perps_{tag}.json"),
                cfg=PerpsConfig(taker_fee_rate=perps_fee,
                                slippage_rate=slippage_rate))
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Jevelin split-cadence supervisor (M2): fast risk loop + "
                    "slow Jev loop + decision cache. Paper only, no orders.")
    p.add_argument("--pairs", default="BTCUSDT",
                   help="comma-separated market symbols (default: BTCUSDT)")
    p.add_argument("--slow-interval", type=float, default=300.0,
                   help="seconds between slow Jev cycles (default: 300)")
    p.add_argument("--fast-interval", type=float, default=5.0,
                   help="seconds between fast risk ticks (default: 5)")
    p.add_argument("--burst-threshold", type=float, default=0.003,
                   help="|1-min return| forcing a score (default: 0.003 = 0.3%%)")
    p.add_argument("--burst-trades", type=int, default=150,
                   help="1-min trade count forcing a score (default: 150)")
    p.add_argument("--burst-cycles", type=int, default=2,
                   help="extra forced slow cycles after a spike (default: 2)")
    p.add_argument("--cache-min-move", type=float, default=0.0005,
                   help="price move below this reuses the verdict (default: 0.0005)")
    p.add_argument("--cache-ttl", type=float, default=1800.0,
                   help="verdict reuse window in seconds (default: 1800)")
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


def main(argv=None) -> int:
    args = parse_args(argv)
    symbols = [s.strip().upper() for s in args.pairs.split(",") if s.strip()]
    if not symbols:
        print("error: --pairs produced no symbols")
        return 2
    api_key = load_api_key()  # OPENROUTER_API_KEY from repo-root .env
    client = JevClient(api_key)
    market = MarketData()
    fee_rate = args.fee_rate if args.fee_rate is not None else SPOT_FEE_RATE
    slippage_rate = args.slippage_rate if args.slippage_rate is not None \
        else SLIPPAGE_RATE
    books = build_books(args, symbols, fee_rate, slippage_rate)
    db_path = args.db or str(Path(args.runtime_dir) / "jevelin.db")
    conn = jev_store.connect(db_path)
    funding_exchange = build_funding_exchange() if args.perps else None
    sup = Supervisor(
        symbols, market, client, books, conn=conn,
        funding_exchange=funding_exchange,
        slow_interval=args.slow_interval, fast_interval=args.fast_interval,
        burst_threshold=args.burst_threshold, burst_trades=args.burst_trades,
        burst_cycles=args.burst_cycles, cache_min_move=args.cache_min_move,
        cache_ttl=args.cache_ttl,
        decision_log_path=str(Path(args.runtime_dir) / "paper_decisions.jsonl"),
        once=args.once, max_cycles=args.max_cycles)
    asyncio.run(sup.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

