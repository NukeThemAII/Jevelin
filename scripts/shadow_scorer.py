#!/usr/bin/env python3
"""Jev shadow scorer CLI — public exchange data only, never orders/private.

Usage:
    .venv/bin/python scripts/shadow_scorer.py --once
    .venv/bin/python scripts/shadow_scorer.py --loop --interval 60 --symbol BTC/USDT
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ccxt  # noqa: E402

from jev_client import JevClient, load_api_key  # noqa: E402
from jev_scorer import ShadowScorer  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Jev shadow scorer (public data only, no orders)"
    )
    p.add_argument("--symbol", default="BTC/USDT", help="market symbol")
    p.add_argument("--exchange", default="binance", help="ccxt exchange id")
    p.add_argument(
        "--once",
        action="store_true",
        help="single score and exit (default if neither --once nor --loop)",
    )
    p.add_argument("--loop", action="store_true", help="score repeatedly")
    p.add_argument(
        "--interval", type=float, default=60.0, help="loop interval seconds"
    )
    return p.parse_args(argv)


def _make_exchange(name: str):
    factory = getattr(ccxt, name, None)
    if factory is None:
        raise SystemExit(f"unknown ccxt exchange: {name}")
    return factory({"enableRateLimit": True})


def _print_verdict(symbol: str, verdict: dict) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not verdict.get("ok"):
        print(
            f"{ts} {symbol} ERROR error={verdict.get('error')}",
            flush=True,
        )
        return
    conf = verdict.get("confidence")
    conf_s = f"{conf:.3f}" if isinstance(conf, (int, float)) else "n/a"
    latency = verdict.get("latency_ms")
    lat_s = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "?"
    print(
        f"{ts} {symbol} "
        f"pump={verdict['pump_0_100']:.1f} "
        f"dump={verdict['dump_0_100']:.1f} "
        f"phase={verdict['phase']} "
        f"exhaustion={verdict['exhaustion_prob']:.3f} "
        f"whipsaw={verdict['whipsaw_prob']:.3f} "
        f"conf={conf_s} "
        f"latency={lat_s}",
        flush=True,
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    loop = bool(args.loop)
    if not loop and not args.once:
        args.once = True  # default: single score

    client = JevClient(load_api_key())
    exchange = _make_exchange(args.exchange)
    scorer = ShadowScorer(client, exchange=exchange)

    try:
        if loop:
            while True:
                verdict = scorer.score(args.symbol)
                _print_verdict(args.symbol, verdict)
                time.sleep(max(1.0, args.interval))
        else:
            verdict = scorer.score(args.symbol)
            _print_verdict(args.symbol, verdict)
    except KeyboardInterrupt:
        pass
    finally:
        print(
            f"totals: calls={client.calls} "
            f"input_tokens={client.input_tokens} "
            f"cost_usd={client.cost_usd}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
