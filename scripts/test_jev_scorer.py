#!/usr/bin/env python3
"""Tests for jev_state / jev_scorer — no network, all external IO mocked."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jev_config import FanoutConfig
from jev_questions import CRITERIA_SIZES, QUESTIONS
from jev_scorer import ShadowScorer, normalize_score
from jev_state import build_state

NOW = 1_800_000_000_000

FULL_ANSWERS = {
    "pump": {"type": "score", "score": 1.91, "confidence": 0.86,
             "legend": {"0": "none", "1": "building", "2": "strong", "3": "euphoric"},
             "probabilities": {"0": 0.0, "1": 0.11, "2": 0.87, "3": 0.02}},
    "dump": {"type": "score", "score": 0.5, "confidence": 0.7,
             "probabilities": {"0": 0.9, "1": 0.1, "2": 0.0, "3": 0.0}},
    "phase": {"type": "choice", "choice": "breakout", "confidence": 0.9,
              "probabilities": {"accumulation": 0.1, "breakout": 0.7,
                                "distribution": 0.1, "capitulation": 0.0, "ranging": 0.1}},
    "exhaustion": {"type": "noul", "noul": 0.21},
    "whipsaw": {"type": "noul", "noul": 0.4},
}


def _mock_exchange(closes=None, trades=None, raise_exc=None):
    ex = MagicMock()
    if raise_exc is not None:
        ex.fetch_ohlcv.side_effect = raise_exc
        return ex
    closes = closes or [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    rows = [[NOW - (len(closes) - 1 - i) * 60_000, 0, 0, 0, c] for i, c in enumerate(closes)]
    ex.fetch_ohlcv.return_value = rows
    ex.fetch_trades.return_value = trades or []
    return ex


def _mock_client(ok=True, answers=None, error=None):
    c = MagicMock()
    c.ask.return_value = {
        "ok": ok,
        "answers": answers if ok else None,
        "error": error,
        "usage": {"input_tokens": 100, "output_tokens": 5, "cost": 0.00002},
        "raw": {},
        "latency_ms": 123.0,
    }
    return c


class StateBuilderTests(unittest.TestCase):
    def test_deterministic_fixture_exact(self):
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        trades = [
            (NOW - 10_000, "buy", 1000.0),
            (NOW - 20_000, "sell", 400.0),
            (NOW - 90_000, "buy", 200.0),
        ]
        state = build_state("BTC/USDT", closes, trades, NOW)
        f = state["features"]
        self.assertAlmostEqual(f["return_60s_pct"], (105 - 104) / 104 * 100, places=9)
        self.assertAlmostEqual(f["return_300s_pct"], 5.0, places=9)
        self.assertAlmostEqual(f["buy_flow_share"], 1000 / 1400, places=9)
        self.assertAlmostEqual(f["trade_rate_ratio"], 4.0, places=9)  # (2/0.5) / (1/1)
        self.assertEqual(f["oversized_buy_count_60s"], 0)  # threshold 3*400=1200
        self.assertEqual(f["oversized_sell_count_60s"], 0)
        self.assertAlmostEqual(f["vs_5m_high_pct"], 0.0, places=9)
        self.assertAlmostEqual(f["vs_5m_low_pct"], (105 - 101) / 101 * 100, places=9)
        self.assertEqual(state["last_60s_price_path_pct_from_start"],
                         [0.0, round((105 - 104) / 104 * 100, 4)])
        self.assertEqual(state["last_10_trades_side_usd"],
                         ["buy,200", "sell,400", "buy,1000"])
        self.assertEqual(state["symbol"], "BTC/USDT")
        self.assertEqual(state["asof_ms"], NOW)

    def test_same_inputs_same_output(self):
        closes = [100.0, 101.0, 102.0]
        trades = [(NOW - 5_000, "BUY", 10.0)]
        self.assertEqual(build_state("X", closes, trades, NOW),
                         build_state("X", closes, trades, NOW))

    def test_empty_inputs_safe(self):
        state = build_state("X", [], [], NOW)
        self.assertEqual(state["last_60s_price_path_pct_from_start"], [])
        self.assertEqual(state["last_10_trades_side_usd"], [])
        self.assertEqual(state["features"]["buy_flow_share"], 0.0)


class NormalizationTests(unittest.TestCase):
    def test_score_to_100(self):
        self.assertEqual(normalize_score(1.91, 4), 63.7)

    def test_bad_criteria_count(self):
        with self.assertRaises(ValueError):
            normalize_score(1.0, 1)


class VerdictMappingTests(unittest.TestCase):
    def test_full_payload_maps(self):
        scorer = ShadowScorer(_mock_client(answers=FULL_ANSWERS),
                              exchange=_mock_exchange())
        v = scorer.score("BTC/USDT")
        self.assertTrue(v["ok"])
        self.assertEqual(v["pump_0_100"], 63.7)
        self.assertEqual(v["dump_0_100"], 16.7)
        self.assertEqual(v["phase"], "breakout")
        self.assertAlmostEqual(v["exhaustion_prob"], 0.21)
        self.assertAlmostEqual(v["whipsaw_prob"], 0.4)
        self.assertAlmostEqual(v["confidence"], 0.7)  # min(0.86, 0.7, 0.9)

    def test_questions_shape(self):
        self.assertEqual(CRITERIA_SIZES, {"pump": 4, "dump": 4})
        for qid, q in QUESTIONS.items():
            self.assertIn(q["type"], ("choice", "noul", "score"))
            self.assertIn("instructions", q)

    def test_decision_id_flows_into_verdict_and_client(self):
        # M0 / F-P1-3: state record -> verdict record carries the decision id.
        client = _mock_client(answers=FULL_ANSWERS)
        scorer = ShadowScorer(client, exchange=_mock_exchange())
        v = scorer.score("BTC/USDT", decision_id="decid0000042")
        self.assertEqual(v["decision_id"], "decid0000042")
        self.assertEqual(client.ask.call_args.kwargs.get("decision_id"), "decid0000042")
        # fail-open verdicts carry the id too (never fabricated fields, only the id)
        scorer = ShadowScorer(_mock_client(ok=False, error="timeout"),
                              exchange=_mock_exchange())
        v = scorer.score("BTC/USDT", decision_id="decid0000043")
        self.assertEqual(v["decision_id"], "decid0000043")


class FailOpenTests(unittest.TestCase):
    def test_client_error_no_fabrication(self):
        scorer = ShadowScorer(_mock_client(ok=False, error="timeout"),
                              exchange=_mock_exchange())
        v = scorer.score("BTC/USDT")
        self.assertFalse(v["ok"])
        self.assertEqual(v["error"], "timeout")
        self.assertNotIn("pump_0_100", v)

    def test_exchange_exception_no_fabrication(self):
        scorer = ShadowScorer(_mock_client(answers=FULL_ANSWERS),
                              exchange=_mock_exchange(raise_exc=RuntimeError("ccxt down")))
        v = scorer.score("BTC/USDT")
        self.assertFalse(v["ok"])
        self.assertIn("ccxt down", v["error"])
        self.assertNotIn("phase", v)


def _answers_with_whipsaw(w, w2=None):
    """FULL_ANSWERS clone with the whipsaw noul replaced."""
    import copy

    a = copy.deepcopy(FULL_ANSWERS)
    a["whipsaw"]["noul"] = w
    return a


def _mock_client_seq(results):
    """Mock client whose ask() returns each result in order."""
    c = MagicMock()
    c.ask.side_effect = list(results)
    return c


def _ok_result(answers):
    return {"ok": True, "answers": answers, "error": None,
            "usage": {"input_tokens": 100, "output_tokens": 5, "cost": 0.00002},
            "raw": {}, "latency_ms": 123.0}


class FanoutTests(unittest.TestCase):
    """M3 whipsaw self-consistency fan-out: 2nd Jev sample in (0.40, 0.60)."""

    def test_band_triggers_second_sample(self):
        client = _mock_client_seq([_ok_result(_answers_with_whipsaw(0.5)),
                                   _ok_result(_answers_with_whipsaw(0.43))])
        scorer = ShadowScorer(client, exchange=_mock_exchange(),
                              fanout=FanoutConfig())
        v = scorer.score_state("{}", "BTC/USDT")
        self.assertEqual(client.ask.call_count, 2)  # ONE extra sample, same state
        self.assertEqual(client.ask.call_args_list[1].args[0], "{}")
        self.assertEqual(v["fan_out"], 1)
        self.assertAlmostEqual(v["whipsaw_prob"], 0.5)
        self.assertAlmostEqual(v["whipsaw_prob_2"], 0.43)
        self.assertEqual(v["answers_2"]["whipsaw"]["noul"], 0.43)

    def test_outside_band_single_call(self):
        for w in (0.3, 0.4, 0.6, 0.7):  # band is the OPEN interval (0.40, 0.60)
            client = _mock_client_seq([_ok_result(_answers_with_whipsaw(w))])
            scorer = ShadowScorer(client, exchange=_mock_exchange(),
                                  fanout=FanoutConfig())
            v = scorer.score_state("{}", "BTC/USDT")
            self.assertEqual(client.ask.call_count, 1, w)
            self.assertNotIn("whipsaw_prob_2", v)
            self.assertIsNone(v.get("fan_out"))

    def test_band_edges_fan_out(self):
        for w in (0.401, 0.5, 0.599):
            client = _mock_client_seq([_ok_result(_answers_with_whipsaw(w)),
                                       _ok_result(_answers_with_whipsaw(0.2))])
            scorer = ShadowScorer(client, exchange=_mock_exchange(),
                                  fanout=FanoutConfig())
            scorer.score_state("{}", "BTC/USDT")
            self.assertEqual(client.ask.call_count, 2, w)

    def test_second_sample_failure_recorded_not_fabricated(self):
        client = _mock_client_seq([_ok_result(_answers_with_whipsaw(0.5)),
                                   {"ok": False, "answers": None, "error": "timeout"}])
        scorer = ShadowScorer(client, exchange=_mock_exchange(),
                              fanout=FanoutConfig())
        v = scorer.score_state("{}", "BTC/USDT")
        self.assertEqual(v["fan_out"], 1)
        self.assertNotIn("whipsaw_prob_2", v)  # never fabricate the 2nd sample
        self.assertIn("timeout", v["fan_out_error"])

    def test_no_fanout_config_keeps_single_call(self):
        client = _mock_client_seq([_ok_result(_answers_with_whipsaw(0.5))])
        scorer = ShadowScorer(client, exchange=_mock_exchange())
        v = scorer.score_state("{}", "BTC/USDT")
        self.assertEqual(client.ask.call_count, 1)  # legacy path unchanged
        self.assertIsNone(v.get("fan_out"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
