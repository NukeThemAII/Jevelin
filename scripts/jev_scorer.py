#!/usr/bin/env python3
"""ShadowScorer — public Binance data -> state -> Jev answers -> verdict dict.

Fail-open: any exchange/client failure yields {ok:False, error, symbol} with
no fabricated verdict fields. Never calls authenticated/private endpoints.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import ccxt

from jev_questions import CRITERIA_SIZES, QUESTIONS
from jev_state import build_state


def normalize_score(score: float, n_criteria: int) -> float:
    """Map Jev score (0..n_crit-1) to 0..100. round 1."""
    if n_criteria < 2:
        raise ValueError("n_criteria must be >= 2")
    return round(float(score) / (n_criteria - 1) * 100.0, 1)


def _confidence_min(answers: dict) -> Optional[float]:
    confs = []
    for key in ("pump", "dump", "phase"):  # score + choice
        ans = answers.get(key) or {}
        conf = ans.get("confidence")
        if conf is not None:
            confs.append(float(conf))
    return min(confs) if confs else None


class ShadowScorer:
    """Pull public trades/OHLCV, score via JevClient. Public data only."""

    def __init__(self, client, exchange=None) -> None:
        self.client = client
        self.exchange = (
            exchange if exchange is not None else ccxt.binance({"enableRateLimit": True})
        )

    def score(self, symbol: str) -> dict:
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1m", limit=10)
            raw_trades = self.exchange.fetch_trades(symbol, limit=200)
            closes = [float(c[4]) for c in ohlcv]
            trades = [
                (int(t["timestamp"]), str(t["side"]), float(t["price"]) * float(t["amount"]))
                for t in raw_trades
            ]
            now_ms = int(time.time() * 1000)
            state = build_state(symbol, closes, trades, now_ms)
            state_str = json.dumps(state, separators=(",", ":"), sort_keys=True)

            result = self.client.ask(state_str, QUESTIONS)
            if not result.get("ok"):
                return {
                    "ok": False,
                    "error": str(result.get("error") or "jev ask failed"),
                    "symbol": symbol,
                }

            answers = result.get("answers") or {}
            pump_raw = float(answers["pump"]["score"])
            dump_raw = float(answers["dump"]["score"])
            verdict = {
                "ok": True,
                "error": None,
                "symbol": symbol,
                "pump_0_100": normalize_score(pump_raw, CRITERIA_SIZES["pump"]),
                "dump_0_100": normalize_score(dump_raw, CRITERIA_SIZES["dump"]),
                "phase": str(answers["phase"]["choice"]),
                "exhaustion_prob": float(answers["exhaustion"]["noul"]),
                "whipsaw_prob": float(answers["whipsaw"]["noul"]),
                "confidence": _confidence_min(answers),
                "answers": answers,
                "latency_ms": result.get("latency_ms"),
            }
            return verdict
        except Exception as exc:  # fail-open: never fabricate values
            return {"ok": False, "error": str(exc), "symbol": symbol}
