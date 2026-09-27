#!/usr/bin/env python3
"""Paper-trading loop — public market data + local JSON state only.

NEVER calls any exchange private API and NEVER places orders. Each cycle:
score (public data) -> public ticker price -> decide -> apply_action (local) ->
save -> print one status line per book. ONE Jev verdict per cycle drives BOTH
books: the spot book (decide/PaperPortfolio) and, when enabled, the paper perps
book (decide_perps/PerpsPortfolio, public funding rate via ccxt binanceusdm;
funding fetch failure => funding=na, fail-open). Fail-open: any per-cycle error prints
"cycle error: ..." and the loop continues; KeyboardInterrupt exits cleanly.

Reuses ShadowScorer (jev_scorer), decide + RiskConfig (jev_gates), PaperPortfolio
(jev_paper), PerpsConfig + PerpsPortfolio + decide_perps (jev_perps), and
load_api_key (jev_client). No logic is duplicated here.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import (  # noqa: E402
    PERPS_TAKER_FEE_RATE,
    SLIPPAGE_RATE,
    SPOT_FEE_RATE,
    append_jsonl,
    new_decision_id,
)
from jev_client import JevClient, load_api_key  # noqa: E402
from jev_gates import RiskConfig, decide  # noqa: E402
from jev_paper import PaperPortfolio  # noqa: E402
from jev_perps import PerpsConfig, PerpsPortfolio, decide_perps  # noqa: E402
from jev_scorer import ShadowScorer  # noqa: E402


def _ts_iso(ts_ms) -> str:
    try:
        return datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts_ms)


def _veto_status(action: dict) -> str:
    """Readable reason / veto block for a cycle log line (M0 / F-P1-4)."""
    failed = action.get("vetoed_by") or []
    if failed:
        return (f"veto_bitmask={action.get('veto_bitmask', 0)} "
                f"vetoed_by={','.join(failed)} reason={action['reason']}")
    return f"reason={action['reason']}"


def _decision_row(book, decision_id, symbol, ts_ms, price, action, result, verdict, equity):
    """One gate-decision record per book per cycle (M0 / F-P1-3 + F-P1-4)."""
    v = verdict if isinstance(verdict, dict) else {}
    return {
        "ts_ms": int(ts_ms),
        "decision_id": decision_id,
        "book": book,
        "symbol": symbol,
        "price": float(price),
        "action": action.get("action"),
        "executed": result.get("executed"),
        "veto_bitmask": int(action.get("veto_bitmask") or 0),
        "vetoed_by": list(action.get("vetoed_by") or []),
        "reason": action.get("reason"),
        "equity": float(equity),
        "fees": float(result.get("fees") or 0.0),
        "slippage": float(result.get("slippage") or 0.0),
        "realized_pnl": float(result.get("realized_pnl") or 0.0),
        "funding_paid": float(result.get("funding_paid") or 0.0),
        "verdict": {
            "ok": v.get("ok"),
            "pump_0_100": v.get("pump_0_100"),
            "dump_0_100": v.get("dump_0_100"),
            "phase": v.get("phase"),
            "exhaustion_prob": v.get("exhaustion_prob"),
            "whipsaw_prob": v.get("whipsaw_prob"),
            "confidence": v.get("confidence"),
        },
    }


def fetch_funding_rate(funding_exchange, symbol):
    """Public funding rate (float) or None on any failure / missing handle. Never raises."""
    if funding_exchange is None:
        return None
    try:
        rate = float(funding_exchange.fetch_funding_rate(symbol)["fundingRate"])
        return rate if math.isfinite(rate) else None
    except Exception:  # fail-open: no funding data => no funding veto
        return None


def run_perps_cycle(verdict, perps_portfolio, perps_cfg, funding_exchange, symbol, price,
                    now_ms, decision_id=None, decision_log_path=None) -> dict:
    """Drive the paper perps book from an existing verdict. Never raises (KeyboardInterrupt excepted)."""
    try:
        funding_rate = fetch_funding_rate(funding_exchange, symbol)
        pf = perps_portfolio.to_pf_state(price)  # pre-trade state drives the decision
        action = decide_perps(verdict, pf, perps_cfg, now_ms, funding_rate,
                              decision_id=decision_id)
        result = perps_portfolio.apply_action(action, symbol, price, now_ms, funding_rate,
                                              decision_id=decision_id)
        perps_portfolio.save()
        pf_line = perps_portfolio.to_pf_state(price)

        funding_str = "na" if funding_rate is None else f"{funding_rate:.6f}"
        print(
            f"perps: {_ts_iso(now_ms)} decision={decision_id} {symbol} price={price:.4f} "
            f"funding={funding_str} action={action['action']} executed={result['executed']} "
            f"detail={result['detail']} {_veto_status(action)} "
            f"fees={result.get('fees', 0.0):.6f} slippage={result.get('slippage', 0.0):.6f} "
            f"equity={pf_line.equity_usd:.2f} side={pf_line.side} "
            f"daily_pnl_pct={pf_line.daily_pnl_pct:.4f}"
        )
        if decision_log_path is not None:
            append_jsonl(decision_log_path, _decision_row(
                "perps", decision_id, symbol, now_ms, price, action, result, verdict,
                pf_line.equity_usd))
        return {
            "funding_rate": funding_rate,
            "action": action,
            "result": result,
            "equity": pf_line.equity_usd,
            "has_position": pf_line.has_position,
            "side": pf_line.side,
            "daily_pnl_pct": pf_line.daily_pnl_pct,
        }
    except Exception as exc:  # fail-open: perps problems never break the spot book
        print(f"perps: cycle error: {type(exc).__name__}: {exc} decision={decision_id}")
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_cycle(scorer, portfolio, cfg, exchange, symbol, now_ms, perps_portfolio=None,
              perps_cfg=None, funding_exchange=None, decision_id=None,
              decision_log_path=None) -> dict:
    """Run one paper cycle (spot book, plus perps book when ``perps_portfolio`` is given).

    Fail-open: returns a dict, never raises (KeyboardInterrupt excepted). Each
    cycle gets one unique ``decision_id`` flowing through verdict -> gate
    decision -> trade rows -> every log line (M0 / F-P1-3).
    """
    decision_id = decision_id or new_decision_id()
    if decision_log_path is None:
        decision_log_path = str(Path(portfolio.state_path).parent / "paper_decisions.jsonl")
    try:
        verdict = scorer.score(symbol, decision_id=decision_id)
        price = float(exchange.fetch_ticker(symbol)["last"])
        pf = portfolio.to_pf_state(price)  # pre-trade state drives the decision
        action = decide(verdict, pf, cfg, now_ms, decision_id=decision_id)
        result = portfolio.apply_action(action, symbol, price, now_ms,
                                        decision_id=decision_id)
        portfolio.save()
        pf_line = portfolio.to_pf_state(price)  # post-trade status for the line

        line = (
            f"spot: {_ts_iso(now_ms)} decision={decision_id} {symbol} price={price:.4f} "
            f"action={action['action']} executed={result['executed']} "
            f"{_veto_status(action)} "
            f"fees={result.get('fees', 0.0):.6f} slippage={result.get('slippage', 0.0):.6f} "
            f"equity={pf_line.equity_usd:.2f} has_position={pf_line.has_position} "
            f"daily_pnl_pct={pf_line.daily_pnl_pct:.4f}"
        )
        print(line)
        append_jsonl(decision_log_path, _decision_row(
            "spot", decision_id, symbol, now_ms, price, action, result, verdict,
            pf_line.equity_usd))
        out = {
            "ts_ms": now_ms,
            "decision_id": decision_id,
            "symbol": symbol,
            "price": price,
            "verdict": verdict,
            "action": action,
            "result": result,
            "equity": pf_line.equity_usd,
            "has_position": pf_line.has_position,
            "daily_pnl_pct": pf_line.daily_pnl_pct,
        }
        if perps_portfolio is not None:  # same verdict + price drive the perps book
            out["perps"] = run_perps_cycle(
                verdict, perps_portfolio, perps_cfg or PerpsConfig(), funding_exchange,
                symbol, price, now_ms, decision_id=decision_id,
                decision_log_path=decision_log_path)
        return out
    except Exception as exc:  # fail-open: the loop must never crash
        print(f"cycle error: {type(exc).__name__}: {exc} decision={decision_id}")
        return {"ts_ms": now_ms, "decision_id": decision_id, "symbol": symbol,
                "error": f"{type(exc).__name__}: {exc}"}


def _build_exchange():
    """Public-data exchange handle (ccxt). Imported lazily to keep run_cycle lightweight."""
    import ccxt  # noqa: WPS433 (local import is intentional)

    return ccxt.binance({"enableRateLimit": True})


def _build_funding_exchange():
    """Public USD-M futures handle for funding rates only. None if unavailable (fail-open)."""
    try:
        import ccxt  # noqa: WPS433

        return ccxt.binanceusdm({"enableRateLimit": True})
    except Exception:
        return None


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
    p.add_argument(
        "--perps-state", default="runtime/perps_btc.json",
        help="path to paper perps book state JSON (default: runtime/perps_btc.json)",
    )
    p.add_argument(
        "--perps", action=argparse.BooleanOptionalAction, default=True,
        help="also drive the paper perps book (long+short) from the same verdict (default: on)",
    )
    p.add_argument(
        "--fee-rate", type=float, default=None,
        help="per-side fee rate override for BOTH books "
             "(default: spot 0.001, perps taker 0.0005)",
    )
    p.add_argument(
        "--slippage-rate", type=float, default=None,
        help="per-side slippage rate override for BOTH books "
             "(default 0.0005; 0 = perfect limit fills)",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    api_key = load_api_key()  # OPENROUTER_API_KEY from repo-root .env
    client = JevClient(api_key)
    exchange = _build_exchange()
    scorer = ShadowScorer(client, exchange)
    cfg = RiskConfig()
    fee_rate = args.fee_rate if args.fee_rate is not None else SPOT_FEE_RATE
    slippage_rate = args.slippage_rate if args.slippage_rate is not None else SLIPPAGE_RATE
    portfolio = PaperPortfolio(initial_equity_usd=args.initial_equity, state_path=args.state,
                               fee_rate=fee_rate, slippage_rate=slippage_rate)
    portfolio.load()  # resume from prior state when present
    perps_portfolio = perps_cfg = funding_exchange = None
    if args.perps:
        perps_fee = args.fee_rate if args.fee_rate is not None else PERPS_TAKER_FEE_RATE
        perps_cfg = PerpsConfig(taker_fee_rate=perps_fee, slippage_rate=slippage_rate)
        perps_portfolio = PerpsPortfolio(
            initial_equity_usd=args.initial_equity, state_path=args.perps_state, cfg=perps_cfg)
        perps_portfolio.load()
        funding_exchange = _build_funding_exchange()

    try:
        while True:
            now_ms = int(time.time() * 1000)
            run_cycle(scorer, portfolio, cfg, exchange, args.symbol, now_ms,
                      perps_portfolio, perps_cfg, funding_exchange)
            if args.once:
                break
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        print("\ninterrupted - exiting cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())