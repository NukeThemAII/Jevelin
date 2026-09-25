#!/usr/bin/env python3
"""Paper-trading loop — public market data + local JSON state only.

NEVER calls any exchange private API and NEVER places orders. Each cycle:
score (public data) -> public ticker price -> decide -> apply_action (local) ->
save -> print one status line. Fail-open: any per-cycle error prints
"cycle error: ..." and the loop continues; KeyboardInterrupt exits cleanly.

Reuses ShadowScorer (jev_scorer), decide + RiskConfig (jev_gates), PaperPortfolio
(jev_paper), and load_api_key (jev_client). No logic is duplicated here.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_client import JevClient, load_api_key  # noqa: E402
from jev_gates import RiskConfig, decide  # noqa: E402
from jev_paper import PaperPortfolio  # noqa: E402
from jev_scorer import ShadowScorer  # noqa: E402


def _ts_iso(ts_ms) -> str:
    try:
        return datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts_ms)


def run_cycle(scorer, portfolio, cfg, exchange, symbol, now_ms) -> dict:
    """Run one paper cycle. Fail-open: returns a dict, never raises (KeyboardInterrupt excepted)."""
    try:
        verdict = scorer.score(symbol)
        price = float(exchange.fetch_ticker(symbol)["last"])
        pf = portfolio.to_pf_state(price)  # pre-trade state drives the decision
        action = decide(verdict, pf, cfg, now_ms)
        result = portfolio.apply_action(action, symbol, price, now_ms)
        portfolio.save()
        pf_line = portfolio.to_pf_state(price)  # post-trade status for the line

        if action.get("vetoed_by"):
            status = f"vetoed_by={action['vetoed_by']} reason={action['reason']}"
        else:
            status = f"reason={action['reason']}"
        line = (
            f"{_ts_iso(now_ms)} {symbol} price={price:.4f} "
            f"action={action['action']} executed={result['executed']} {status} "
            f"equity={pf_line.equity_usd:.2f} has_position={pf_line.has_position} "
            f"daily_pnl_pct={pf_line.daily_pnl_pct:.4f}"
        )
        print(line)
        return {
            "ts_ms": now_ms,
            "symbol": symbol,
            "price": price,
            "verdict": verdict,
            "action": action,
            "result": result,
            "equity": pf_line.equity_usd,
            "has_position": pf_line.has_position,
            "daily_pnl_pct": pf_line.daily_pnl_pct,
        }
    except Exception as exc:  # fail-open: the loop must never crash
        print(f"cycle error: {type(exc).__name__}: {exc}")
        return {"ts_ms": now_ms, "symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}


def _build_exchange():
    """Public-data exchange handle (ccxt). Imported lazily to keep run_cycle lightweight."""
    import ccxt  # noqa: WPS433 (local import is intentional)

    return ccxt.binance({"enableRateLimit": True})


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Jevelin paper-trading loop (public market data + local JSON state only)."
    )
    p.add_argument("--symbol", default="BTCUSDT", help="market symbol (default: BTCUSDT)")
    p.add_argument("--interval", type=int, default=60, help="seconds between cycles (default: 60)")
    p.add_argument("--once", action="store_true", help="run exactly one cycle and exit")
    p.add_argument("--state", default="runtime/paper_btc.json", help="path to portfolio state JSON")
    p.add_argument(
        "--initial-equity", type=float, default=10000.0, help="starting paper equity in USD"
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    api_key = load_api_key()  # OPENROUTER_API_KEY from repo-root .env
    client = JevClient(api_key)
    exchange = _build_exchange()
    scorer = ShadowScorer(client, exchange)
    cfg = RiskConfig()
    portfolio = PaperPortfolio(initial_equity_usd=args.initial_equity, state_path=args.state)
    portfolio.load()  # resume from prior state when present

    try:
        while True:
            now_ms = int(time.time() * 1000)
            run_cycle(scorer, portfolio, cfg, exchange, args.symbol, now_ms)
            if args.once:
                break
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        print("\ninterrupted - exiting cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())