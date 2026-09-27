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
    """Pull public trades/OHLCV, score via JevClient. Public data only.

    ``fanout`` (FanoutConfig, M3): when the raw whipsaw noul lands in the
    coin-flip band (band_low, band_high), take a SECOND Jev sample of the same
    state/questions and record it on the verdict (``fan_out``/``whipsaw_prob_2``
    / ``answers_2``); the gate layer majority-votes and fails closed on a split.
    """

    def __init__(self, client, exchange=None, fanout=None) -> None:
        self.client = client
        self.exchange = (
            exchange if exchange is not None else ccxt.binance({"enableRateLimit": True})
        )
        self.fanout = fanout

    def score(self, symbol: str, decision_id: Optional[str] = None) -> dict:
        """Verdict dict for one cycle. ``decision_id`` flows into the verdict and
        the client's audit log (M0 / F-P1-3)."""
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
            return self.score_state(state_str, symbol, decision_id=decision_id)
        except Exception as exc:  # fail-open: never fabricate values
            return {"ok": False, "error": str(exc), "symbol": symbol,
                    "decision_id": decision_id}

    def score_state(self, state_str: str, symbol: str,
                    decision_id: Optional[str] = None) -> dict:
        """Verdict from an already-built state string (M2 supervisor). Fail-open.

        The exact Jev call + parse flow of ``score``; the caller owns state
        building and the decision cache."""
        try:
            result = self.client.ask(state_str, QUESTIONS, decision_id=decision_id)
            if not result.get("ok"):
                return {
                    "ok": False,
                    "error": str(result.get("error") or "jev ask failed"),
                    "symbol": symbol,
                    "decision_id": decision_id,
                }

            answers = result.get("answers") or {}
            pump_raw = float(answers["pump"]["score"])
            dump_raw = float(answers["dump"]["score"])
            verdict = {
                "ok": True,
                "error": None,
                "symbol": symbol,
                "decision_id": decision_id,
                "pump_0_100": normalize_score(pump_raw, CRITERIA_SIZES["pump"]),
                "dump_0_100": normalize_score(dump_raw, CRITERIA_SIZES["dump"]),
                "phase": str(answers["phase"]["choice"]),
                "exhaustion_prob": float(answers["exhaustion"]["noul"]),
                "whipsaw_prob": float(answers["whipsaw"]["noul"]),
                "confidence": _confidence_min(answers),
                "answers": answers,
                "latency_ms": result.get("latency_ms"),
            }
            self._maybe_fanout(state_str, verdict, decision_id)
            return verdict
        except Exception as exc:  # fail-open: never fabricate values
            return {"ok": False, "error": str(exc), "symbol": symbol,
                    "decision_id": decision_id}

    def _maybe_fanout(self, state_str: str, verdict: dict,
                      decision_id: Optional[str] = None) -> None:
        """M3 whipsaw self-consistency fan-out: 2nd sample in the coin-flip band.

        Only the raw whipsaw noul triggers it (band_low < noul < band_high).
        On success the second raw answers + noul land on the verdict; on any
        failure nothing is fabricated — ``fan_out=1`` with no ``whipsaw_prob_2``
        tells the gate to fail closed (whipsaw_fanout_tie).
        """
        if self.fanout is None:
            return
        try:
            w = float(verdict.get("whipsaw_prob"))
        except (TypeError, ValueError):
            return
        if not (self.fanout.band_low < w < self.fanout.band_high):
            return  # outside the band: single sample, as today
        verdict["fan_out"] = 1
        try:
            result = self.client.ask(state_str, QUESTIONS, decision_id=decision_id)
        except Exception as exc:  # fail-open: record, never fabricate
            verdict["fan_out_error"] = f"{type(exc).__name__}: {exc}"
            return
        if not result.get("ok"):
            verdict["fan_out_error"] = str(result.get("error") or "jev ask failed")
            return
        try:
            answers2 = result.get("answers") or {}
            verdict["whipsaw_prob_2"] = float(answers2["whipsaw"]["noul"])
            verdict["answers_2"] = answers2
        except Exception as exc:  # malformed 2nd sample: fail-closed upstream
            verdict["fan_out_error"] = f"bad 2nd sample: {type(exc).__name__}: {exc}"
