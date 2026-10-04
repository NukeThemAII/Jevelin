#!/usr/bin/env python3
"""Tests for scripts/jev_heartbeat_watchdog.py — stdlib unittest, no network.

Run: .venv/bin/python scripts/test_jev_heartbeat_watchdog.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_heartbeat_watchdog as watchdog  # noqa: E402


class FakeAlerter:
    def __init__(self) -> None:
        self.sent = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


class LastHeartbeatTsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "heartbeat.jsonl"

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(watchdog.last_heartbeat_ts(self.path))

    def test_empty_file_returns_none(self) -> None:
        self.path.write_text("")
        self.assertIsNone(watchdog.last_heartbeat_ts(self.path))

    def test_reads_last_line_ts(self) -> None:
        lines = [json.dumps({"ts": t, "loop": "fast"}) for t in
                  (1_000_000, 1_000_005, 1_000_010)]
        self.path.write_text("\n".join(lines) + "\n")
        self.assertEqual(watchdog.last_heartbeat_ts(self.path), 1000.010)

    def test_trailing_garbage_line_returns_none(self) -> None:
        self.path.write_text('{"ts": 1000}\nnot json\n')
        self.assertIsNone(watchdog.last_heartbeat_ts(self.path))


class CheckTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hb = Path(self.tmp.name) / "heartbeat.jsonl"
        self.state = Path(self.tmp.name) / "watchdog_state.json"
        self.alerter = FakeAlerter()

    def _write_hb(self, ts_ms: float) -> None:
        self.hb.write_text(json.dumps({"ts": ts_ms, "loop": "fast"}) + "\n")

    def test_fresh_heartbeat_no_alert(self) -> None:
        self._write_hb(1_000_000)
        result = watchdog.check(self.hb, self.state, stale_seconds=120,
                                 alerter=self.alerter, now=1000.0 + 5.0)
        self.assertFalse(result["stale"])
        self.assertFalse(result["alerted"])
        self.assertEqual(self.alerter.sent, [])

    def test_missing_file_is_stale_and_alerts_once(self) -> None:
        result = watchdog.check(self.hb, self.state, stale_seconds=120,
                                 alerter=self.alerter, now=1000.0)
        self.assertTrue(result["stale"])
        self.assertTrue(result["alerted"])
        self.assertEqual(len(self.alerter.sent), 1)
        self.assertIn("STALE", self.alerter.sent[0])

    def test_stale_transition_alerts_then_stays_quiet_while_still_stale(self) -> None:
        self._write_hb(1_000_000)  # ts=1000.0s
        watchdog.check(self.hb, self.state, stale_seconds=120,
                       alerter=self.alerter, now=1000.0 + 5.0)  # fresh
        watchdog.check(self.hb, self.state, stale_seconds=120,
                       alerter=self.alerter, now=1000.0 + 200.0)  # now stale
        self.assertEqual(len(self.alerter.sent), 1)
        self.assertIn("STALE", self.alerter.sent[0])
        # second stale check (heartbeat file unchanged) -> no repeat alert
        watchdog.check(self.hb, self.state, stale_seconds=120,
                       alerter=self.alerter, now=1000.0 + 260.0)
        self.assertEqual(len(self.alerter.sent), 1)

    def test_recovery_after_stale_alerts_once(self) -> None:
        self._write_hb(1_000_000)
        watchdog.check(self.hb, self.state, stale_seconds=120,
                       alerter=self.alerter, now=1000.0 + 200.0)  # stale
        self.assertEqual(len(self.alerter.sent), 1)
        self._write_hb(1_000_000 + 210_000)  # fresh tick again
        watchdog.check(self.hb, self.state, stale_seconds=120,
                       alerter=self.alerter, now=1000.0 + 211.0)
        self.assertEqual(len(self.alerter.sent), 2)
        self.assertIn("recovered", self.alerter.sent[1])

    def test_no_alerter_configured_is_a_noop(self) -> None:
        result = watchdog.check(self.hb, self.state, stale_seconds=120,
                                 alerter=None, now=1000.0)
        self.assertTrue(result["stale"])
        self.assertFalse(result["alerted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
