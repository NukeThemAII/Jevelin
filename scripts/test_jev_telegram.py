#!/usr/bin/env python3
"""Tests for scripts/jev_telegram.py — stdlib unittest, no network.

Run: .venv/bin/python scripts/test_jev_telegram.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_telegram  # noqa: E402


def _mock_response(status: int = 200):
    ctx = mock.MagicMock()
    ctx.__enter__.return_value.status = status
    return ctx


class LoadConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / ".env"

    def test_both_values_present(self) -> None:
        self.env_path.write_text(
            "TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n")
        token, chat_id = jev_telegram.load_telegram_config(self.env_path)
        self.assertEqual(token, "123:abc")
        self.assertEqual(chat_id, "999")

    def test_chat_id_missing_returns_none(self) -> None:
        self.env_path.write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
        token, chat_id = jev_telegram.load_telegram_config(self.env_path)
        self.assertEqual(token, "123:abc")
        self.assertIsNone(chat_id)

    def test_commented_chat_id_is_ignored(self) -> None:
        self.env_path.write_text(
            "TELEGRAM_BOT_TOKEN=123:abc\n# TELEGRAM_CHAT_ID=   # unset\n")
        token, chat_id = jev_telegram.load_telegram_config(self.env_path)
        self.assertEqual(token, "123:abc")
        self.assertIsNone(chat_id)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            jev_telegram.load_telegram_config(self.env_path)  # never written


class TelegramAlerterTests(unittest.TestCase):
    def test_unconfigured_without_chat_id_is_a_noop(self) -> None:
        alerter = jev_telegram.TelegramAlerter("123:abc", None)
        self.assertFalse(alerter.configured)
        with mock.patch("urllib.request.urlopen") as m:
            ok = alerter.send("hello")
        self.assertFalse(ok)
        m.assert_not_called()
        self.assertEqual(alerter.sent, 0)
        self.assertEqual(alerter.errors, 0)

    def test_unconfigured_without_token_is_a_noop(self) -> None:
        alerter = jev_telegram.TelegramAlerter(None, "999")
        self.assertFalse(alerter.configured)
        self.assertFalse(alerter.send("hello"))

    def test_happy_path_sends_and_counts(self) -> None:
        alerter = jev_telegram.TelegramAlerter("123:abc", "999", timeout=1.0)
        with mock.patch("urllib.request.urlopen",
                        return_value=_mock_response(200)) as m:
            ok = alerter.send("heartbeat stale for 10m")
        self.assertTrue(ok)
        self.assertEqual(alerter.sent, 1)
        self.assertEqual(alerter.errors, 0)
        req = m.call_args[0][0]
        self.assertIn("123:abc", req.full_url)
        self.assertNotIn(b"123:abc", req.data)  # token never in the body

    def test_http_error_fails_open(self) -> None:
        alerter = jev_telegram.TelegramAlerter("123:abc", "999", timeout=1.0)
        err = urllib.error.HTTPError("https://api.telegram.org/x", 400, "bad",
                                      None, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            ok = alerter.send("hello")
        self.assertFalse(ok)
        self.assertEqual(alerter.errors, 1)

    def test_network_error_fails_open(self) -> None:
        alerter = jev_telegram.TelegramAlerter("123:abc", "999", timeout=1.0)
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("no route")):
            ok = alerter.send("hello")
        self.assertFalse(ok)
        self.assertEqual(alerter.errors, 1)

    def test_from_env_builds_alerter(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env_path = Path(tmp.name) / ".env"
        env_path.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n")
        alerter = jev_telegram.from_env(env_path)
        self.assertTrue(alerter.configured)


if __name__ == "__main__":
    unittest.main(verbosity=2)
