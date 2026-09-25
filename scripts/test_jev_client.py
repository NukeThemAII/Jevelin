#!/usr/bin/env python3
"""Tests for scripts/jev_client.py — stdlib unittest, no network.

Run: .venv/bin/python scripts/test_jev_client.py -v
"""
from __future__ import annotations

import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_client  # noqa: E402


FAKE_RESPONSE = {
    "answers": {
        "entry_quality": {
            "type": "choice",
            "choice": "high",
            "probabilities": {"skip": 0.05, "normal": 0.25, "high": 0.70},
            "confidence": 0.82,
        },
        "whipsaw": {"type": "noul", "noul": 0.31},
        "regime": {
            "type": "score",
            "score": 1.2,
            "legend": {"0": "ranging", "1": "transition", "2": "trend"},
            "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
            "confidence": 0.77,
        },
    },
    "usage": {"input_tokens": 530, "output_tokens": 42, "cost": 0.000022},
    "id": "dec_abc123",
    "model": "typesafe/jev-1.13",
    "provider": "TypeSafe",
}

QUESTIONS = {
    "entry_quality": {
        "type": "choice",
        "instructions": "rate entry",
        "criteria": {"skip": "no", "normal": "ok", "high": "great"},
    },
    "whipsaw": {"type": "noul", "instructions": "fakeout?"},
    "regime": {
        "type": "score",
        "instructions": "regime",
        "criteria": ["ranging", "transition", "trend"],
    },
}


def _http_error(code: int, body: bytes = b'{"error":"bad"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://openrouter.ai/api/alpha/decisions", code, "err", None, io.BytesIO(body)
    )


def _mock_response(payload: dict):
    raw = json.dumps(payload).encode()
    ctx = mock.MagicMock()
    ctx.__enter__.return_value.read.return_value = raw
    ctx.__enter__.return_value.status = 200
    return ctx


class JevClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "decisions.jsonl"
        self.client = jev_client.JevClient(
            "sk-test-not-a-real-key", timeout=1.0, log_path=self.log_path
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_happy_path_parses_choice_noul_score(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", return_value=_mock_response(FAKE_RESPONSE)
        ) as m:
            result = self.client.ask("state-str", QUESTIONS)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["raw"]["id"], "dec_abc123")

        answers = result["answers"]
        self.assertEqual(answers["entry_quality"]["type"], "choice")
        self.assertEqual(answers["entry_quality"]["choice"], "high")
        self.assertAlmostEqual(answers["entry_quality"]["confidence"], 0.82)
        self.assertEqual(answers["whipsaw"]["type"], "noul")
        self.assertAlmostEqual(answers["whipsaw"]["noul"], 0.31)
        self.assertEqual(answers["regime"]["type"], "score")
        self.assertAlmostEqual(answers["regime"]["score"], 1.2)
        self.assertIn("2", answers["regime"]["legend"])
        self.assertGreaterEqual(result["latency_ms"], 0.0)
        m.assert_called_once()

    def test_http_400_fail_open_no_retry(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)) as m:
            result = self.client.ask("s", QUESTIONS)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["answers"])
        self.assertIn("HTTP 400", result["error"])
        self.assertEqual(result["usage"]["input_tokens"], 0)
        m.assert_called_once()  # 4xx must not retry

    def test_timeout_fail_open(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(socket.timeout("timed out")),
        ) as m:
            result = self.client.ask("s", QUESTIONS)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["answers"])
        self.assertIn("timed out", result["error"])
        self.assertEqual(m.call_count, 2)  # one retry allowed, then give up

    def test_retry_then_success(self) -> None:
        boom = urllib.error.URLError("connection reset")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[boom, _mock_response(FAKE_RESPONSE)],
        ) as m:
            result = self.client.ask("s", QUESTIONS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["answers"]["entry_quality"]["choice"], "high")
        self.assertEqual(m.call_count, 2)

    def test_usage_accumulation(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", return_value=_mock_response(FAKE_RESPONSE)
        ):
            self.client.ask("s", QUESTIONS)
            self.client.ask("s2", QUESTIONS)
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(self.client.input_tokens, 1060)
        self.assertEqual(self.client.output_tokens, 84)
        self.assertAlmostEqual(self.client.cost_usd, 0.000044)

    def test_decision_log_line_written(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", return_value=_mock_response(FAKE_RESPONSE)
        ):
            self.client.ask("log-state", QUESTIONS)
        self.assertTrue(self.log_path.exists())
        lines = self.log_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertIn("ts", entry)
        self.assertEqual(
            entry["state_sha256"],
            __import__("hashlib").sha256(b"log-state").hexdigest(),
        )
        self.assertEqual(entry["questions"], QUESTIONS)
        self.assertEqual(entry["raw"]["id"], "dec_abc123")
        self.assertIsNone(entry["error"])

    def test_decision_log_records_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)):
            self.client.ask("bad-state", QUESTIONS)
        entry = json.loads(self.log_path.read_text().strip().splitlines()[0])
        self.assertIsNone(entry["raw"])
        self.assertIn("HTTP 400", entry["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
