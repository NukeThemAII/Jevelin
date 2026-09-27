#!/usr/bin/env python3
"""Tests for jev_supervisor (M2 split-cadence supervisor) — stdlib only.

Mock network/client everywhere, simulated time via an injected clock. NO live
API calls. Run: .venv/bin/python scripts/test_jev_supervisor.py -v
"""
import asyncio
import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_store  # noqa: E402
from jev_paper import PaperPortfolio  # noqa: E402
from jev_perps import PerpsConfig, PerpsPortfolio  # noqa: E402
from jev_supervisor import BookPair, Supervisor  # noqa: E402

T0 = 1_800_000_000.0
T0_MS = int(T0 * 1000)
SYMBOL = "BTCUSDT"

# A "strong pump, no dump" answer set: passes every entry gate (confidence 0.8).
ANSWERS = {
    "pump": {"score": 2.5, "confidence": 0.8},
    "dump": {"score": 0.5, "confidence": 0.8},
    "phase": {"choice": "accumulation", "confidence": 0.8},
    "exhaustion": {"noul": 0.3},
    "whipsaw": {"noul": 0.2},
}


class FakeClock:
    def __init__(self, start=T0):
        self.t = float(start)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)


class FakeClient:
    """Counts asks; never touches the network."""

    def __init__(self, answers=None):
        self.calls = 0
        self.cost_usd = 0.0
        self.states = []
        self.answers = answers if answers is not None else ANSWERS

    def ask(self, state, questions, decision_id=None):
        self.calls += 1
        self.cost_usd += 0.00002
        self.states.append(state)
        return {
            "ok": True,
            "answers": self.answers,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.00002},
            "raw": {"answers": self.answers,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "cost": 0.00002}},
            "error": None,
            "latency_ms": 400.0,
            "decision_id": decision_id,
        }


class FakeMarket:
    """Duck-typed stand-in for jev_supervisor.MarketData."""

    def __init__(self, price=100.0, closes=None, trades=None):
        self.exchange = object()  # never used by the supervisor's state flow
        self.price = float(price)
        self._closes = list(closes) if closes is not None else [float(price)] * 10
        self._trades = list(trades or [])
        self.history = []  # (ts_ms, price) as fed by poll_ticker
        self.polls = 0
        self.on_poll = None  # optional hook(ts_ms) for fault injection

    def poll_ticker(self, symbol, now_ms):
        self.polls += 1
        self.history.append((int(now_ms), float(self.price)))
        if self.on_poll is not None:
            self.on_poll(int(now_ms))
        return float(self.price)

    def refresh(self, symbol, now_ms):
        pass

    def closes(self, symbol):
        return list(self._closes)

    def trades(self, symbol):
        return list(self._trades)

    def last_price(self, symbol):
        return float(self.price)

    def price_ago(self, symbol, ts_ms):
        best = None
        for ts, price in self.history:
            if ts <= int(ts_ms):
                best = price
        return best

    def trade_count_since(self, symbol, ts_ms):
        return sum(1 for ts, _side, _usd in self._trades if ts >= int(ts_ms))


def make_books(tmp, initial=10000.0):
    spot = PaperPortfolio(initial_equity_usd=initial,
                          state_path=str(Path(tmp) / "paper_btc.json"))
    perps = PerpsPortfolio(initial_equity_usd=initial,
                           state_path=str(Path(tmp) / "perps_btc.json"),
                           cfg=PerpsConfig())
    return BookPair(spot=spot, perps=perps)


def enter_long(perps, price=100.0, ts_ms=T0_MS):
    return perps.apply_action(
        {"action": "enter_long", "size_fraction": 0.1, "leverage": 3.0,
         "reason": "fixture", "decision_id": "fixture-1"},
        SYMBOL, float(price), int(ts_ms))


def read_jsonl(path):
    rows = []
    p = Path(path)
    if not p.exists():
        return rows
    for line in p.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class FastLoopStopTests(unittest.TestCase):
    """Stop close fires on the fast loop (free, no Jev) within 10 s of breach."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.market = FakeMarket(price=100.0)
        self.client = FakeClient()
        self.conn = jev_store.connect(str(Path(self.tmp.name) / "jevelin.db"))
        self.addCleanup(self.conn.close)
        self.books = make_books(self.tmp.name)
        enter_long(self.books.perps, price=100.0)  # stop ~= 98.0, liq ~= 68.3

    def test_stop_close_within_10s_of_breach(self):
        t0 = self.clock.now()

        def price_fn(_now_ms):
            t = self.clock.now() - t0
            if t < 5.0:
                return 100.0
            if t < 6.0:
                return 97.9  # below the ~98.0 stop, above liq
            return 98.5

        self.market.poll_ticker = lambda symbol, now_ms: (
            self.market.history.append((int(now_ms), price_fn(now_ms))),
            price_fn(now_ms))[1]

        sup = Supervisor([SYMBOL], self.market, self.client,
                         {SYMBOL: self.books}, conn=self.conn, clock=self.clock.now,
                         fast_interval=1.0, slow_interval=3600.0,
                         decision_log_path=str(Path(self.tmp.name) / "paper_decisions.jsonl"))
        self.addCleanup(sup.restore_signal_handlers)

        async def fake_sleep(seconds):
            self.clock.advance(seconds)
            if self.clock.now() >= t0 + 6.0:
                sup.request_shutdown()
            await asyncio.sleep(0)

        sup._sleep = fake_sleep
        asyncio.run(sup.run_fast_loop())

        # closed by the fast loop, zero Jev calls
        self.assertIsNone(self.books.perps.position)
        self.assertEqual(self.client.calls, 0)

        trades = read_jsonl(Path(self.tmp.name) / "perps_btc.json.trades.jsonl")
        closes = [t for t in trades if t.get("action") == "stop_loss"]
        self.assertEqual(len(closes), 1)
        breach_ts_ms = T0_MS + 5000
        self.assertGreaterEqual(closes[0]["ts_ms"], breach_ts_ms)
        self.assertLessEqual(closes[0]["ts_ms"] - breach_ts_ms, 10_000)

        # ... and the fill landed in the store (before shutdown closes it).
        # First tail capture also picks up the fixture entry row — 2 rows total.
        rows = jev_store.list_trades(self.conn, book="perps")
        stop_rows = [r for r in rows if r["reason"] == "stop_loss"]
        self.assertEqual(len(stop_rows), 1)
        self.assertEqual(rows[-1]["reason"], "stop_loss")
        sup.shutdown()


class SlowLoopCacheTests(unittest.TestCase):
    """Quiet tape reuses the verdict; moving tape scores again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.market = FakeMarket(price=100.0)
        self.client = FakeClient()
        self.books = make_books(self.tmp.name)
        self.sup = Supervisor([SYMBOL], self.market, self.client,
                              {SYMBOL: self.books}, conn=None,
                              clock=self.clock.now,
                              decision_log_path=str(Path(self.tmp.name) / "paper_decisions.jsonl"))
        self.addCleanup(self.sup.restore_signal_handlers)

    def test_quiet_tape_cache_hit_no_call(self):
        r1 = self.sup.slow_cycle()[0]
        self.assertEqual(r1["cache_hit"], "miss")
        self.assertEqual(self.client.calls, 1)

        self.clock.advance(300)
        self.market.price = 100.02  # +2 bp < 5 bp threshold
        r2 = self.sup.slow_cycle()[0]
        self.assertEqual(r2["cache_hit"], "hit_stale_price")
        self.assertEqual(self.client.calls, 1)  # unchanged: no Jev call

    def test_moving_tape_miss_and_call(self):
        self.sup.slow_cycle()
        self.clock.advance(300)
        self.market.price = 100.06  # +6 bp >= 5 bp threshold
        r2 = self.sup.slow_cycle()[0]
        self.assertEqual(r2["cache_hit"], "miss")
        self.assertEqual(self.client.calls, 2)

    def test_identical_state_exact_hit(self):
        self.sup.slow_cycle()
        r2 = self.sup.slow_cycle()[0]  # same clock => same state => exact hit
        self.assertEqual(r2["cache_hit"], "hit_exact")
        self.assertEqual(self.client.calls, 1)

    def test_cache_fields_on_cycle_lines(self):
        self.sup.slow_cycle()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.sup.slow_cycle()
        text = out.getvalue()
        self.assertIn("cache=hit_exact", text)
        self.assertIn("burst=0", text)
        self.assertIn("cost=", text)


class BurstTests(unittest.TestCase):
    """Spike => immediate score + --burst-cycles more, all logged burst=1."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.market = FakeMarket(price=100.0)
        self.client = FakeClient()
        self.books = make_books(self.tmp.name)
        self.sup = Supervisor([SYMBOL], self.market, self.client,
                              {SYMBOL: self.books}, conn=None,
                              clock=self.clock.now, burst_cycles=2,
                              burst_threshold=0.003,
                              decision_log_path=str(Path(self.tmp.name) / "paper_decisions.jsonl"))
        self.addCleanup(self.sup.restore_signal_handlers)

    def test_spike_forces_immediate_plus_two_cycles(self):
        self.market.history.append((T0_MS - 60_000, 100.0))
        self.market.price = 100.5  # +0.5% within a minute > 0.3% threshold

        self.sup.fast_tick()
        self.assertTrue(self.sup._burst_trigger.is_set())

        t0 = self.clock.now()

        async def fake_sleep(seconds):
            self.clock.advance(seconds)
            if self.clock.now() >= t0 + 1000.0:
                self.sup.request_shutdown()
            await asyncio.sleep(0)

        self.sup._sleep = fake_sleep

        async def collect():
            await self.sup.run_slow_loop()

        with contextlib.redirect_stdout(io.StringIO()) as out:
            asyncio.run(collect())

        # immediate score at the spike, then 2 burst cycles, then reuse
        self.assertEqual(self.client.calls, 3)
        self.assertEqual(len(self.sup.slow_results), 4)
        bursts = [r["burst"] for r in self.sup.slow_results]
        self.assertEqual(bursts, [1, 1, 1, 0])
        self.assertEqual(self.sup.slow_results[0]["ts_ms"], T0_MS)  # immediate
        self.assertEqual(self.sup.slow_results[3]["cache_hit"], "hit_stale_price")

        spot_lines = [ln for ln in out.getvalue().splitlines()
                      if ln.startswith("spot:") and "burst=1" in ln]
        self.assertEqual(len(spot_lines), 3)


class ShutdownTests(unittest.TestCase):
    """SIGINT => flush, save book state, close DB, print summary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.market = FakeMarket(price=100.0)
        self.client = FakeClient()
        self.books = make_books(self.tmp.name)
        self.db_path = Path(self.tmp.name) / "jevelin.db"

    def test_sigint_saves_state_and_flushes_db(self):
        sup = Supervisor([SYMBOL], self.market, self.client, {SYMBOL: self.books},
                         conn=jev_store.connect(str(self.db_path)),
                         clock=self.clock.now, fast_interval=1.0,
                         slow_interval=300.0,
                         decision_log_path=str(Path(self.tmp.name) / "paper_decisions.jsonl"))
        self.addCleanup(sup.restore_signal_handlers)

        async def fake_sleep(seconds):
            self.clock.advance(seconds)
            await asyncio.sleep(0)

        sup._sleep = fake_sleep

        def on_poll(_now_ms):
            if self.market.polls == 2:
                os.kill(os.getpid(), signal.SIGINT)  # our handler, not KeyboardInterrupt

        self.market.on_poll = on_poll

        with contextlib.redirect_stdout(io.StringIO()) as out:
            asyncio.run(sup.run())

        text = out.getvalue()
        self.assertIn("shutdown summary", text)
        self.assertIn("cache", text)

        # book state saved atomically and parseable
        state = json.loads((Path(self.tmp.name) / "perps_btc.json").read_text())
        self.assertIn("position", state)

        # DB row flushed before close
        conn = jev_store.connect(str(self.db_path))
        try:
            rows = jev_store.list_decisions(conn)
        finally:
            conn.close()
        self.assertGreaterEqual(len(rows), 1)

        summary = sup.summary()
        self.assertGreaterEqual(summary["slow_cycles"], 1)
        self.assertGreaterEqual(summary["fast_ticks"], 1)


class LiveStoreWriteTests(unittest.TestCase):
    """One simulated cycle writes decisions/trades rows and keeps JSONL shapes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.market = FakeMarket(price=100.0)
        self.client = FakeClient()
        self.books = make_books(self.tmp.name)
        self.conn = jev_store.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.sup = Supervisor([SYMBOL], self.market, self.client,
                              {SYMBOL: self.books}, conn=self.conn,
                              clock=self.clock.now,
                              decision_log_path=str(Path(self.tmp.name) / "paper_decisions.jsonl"))
        self.addCleanup(self.sup.restore_signal_handlers)

    def test_cycle_writes_store_rows(self):
        r = self.sup.slow_cycle()[0]
        self.assertEqual(r["cache_hit"], "miss")

        decisions = jev_store.list_decisions(self.conn)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision_id"], r["decision_id"])
        self.assertEqual(decisions[0]["ok"], 1)
        self.assertAlmostEqual(decisions[0]["cost"], 0.00002)
        self.assertIsNotNone(decisions[0]["state_json"])
        self.assertIsNotNone(decisions[0]["verdict_json"])

        trades = jev_store.list_trades(self.conn)
        self.assertEqual(len(trades), 2)  # spot buy + perps enter_long
        books = sorted(t["book"] for t in trades)
        self.assertEqual(books, ["perps", "spot"])
        for t in trades:
            self.assertEqual(t["decision_id"], r["decision_id"])
            self.assertGreater(t["fees"], 0.0)
            self.assertGreater(t["slippage"], 0.0)
            raw = json.loads(t["raw_json"])
            self.assertEqual(raw["decision_id"], r["decision_id"])

    def test_jsonl_rows_keep_m0_shape(self):
        self.sup.slow_cycle()
        spot_trades = read_jsonl(Path(self.tmp.name) / "paper_btc.json.trades.jsonl")
        self.assertEqual(set(spot_trades[0]), {
            "ts_ms", "decision_id", "book", "symbol", "side", "price", "qty", "usd",
            "realized_pnl", "fees", "slippage", "reason"})
        decisions = read_jsonl(Path(self.tmp.name) / "paper_decisions.jsonl")
        self.assertEqual(len(decisions), 2)  # spot + perps gate rows
        self.assertEqual(set(decisions[0]), {
            "ts_ms", "decision_id", "book", "symbol", "price", "action", "executed",
            "veto_bitmask", "vetoed_by", "reason", "equity", "fees", "slippage",
            "realized_pnl", "funding_paid", "verdict"})


class RiskFlagTests(unittest.TestCase):
    def test_daily_loss_kill_flag_on_fast_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            market = FakeMarket(price=100.0)
            books = make_books(tmp)
            books.perps.day = datetime.now(timezone.utc).date().isoformat()
            books.perps.day_start_equity = 10000.0
            books.perps.equity = 9400.0  # -6% on the day
            sup = Supervisor([SYMBOL], market, FakeClient(), {SYMBOL: books},
                             conn=None, clock=clock.now,
                             decision_log_path=str(Path(tmp) / "paper_decisions.jsonl"))
            self.addCleanup(sup.restore_signal_handlers)
            sup.fast_tick()
            flags = sup.risk_flags(SYMBOL)
            self.assertTrue(flags["daily_loss_kill"])
            self.assertTrue(flags["drawdown"])


class Simulated24hTests(unittest.TestCase):
    """M2 acceptance: quiet 75% / normal 25% tape => Jev calls <= 40% of 288."""

    def test_24h_tape_call_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock()
            market = FakeMarket(price=100.0)
            client = FakeClient()
            books = make_books(tmp)
            sup = Supervisor([SYMBOL], market, client, {SYMBOL: books}, conn=None,
                             clock=clock.now,
                             decision_log_path=str(Path(tmp) / "paper_decisions.jsonl"))
            self.addCleanup(sup.restore_signal_handlers)

            # 4-cycle block: quiet +1bp, quiet -1bp, quiet +2bp, normal +8bp.
            # Quiet cumulative drift stays <= 2 bp of the last scored price.
            moves = [1.0001, 0.9999, 1.0002, 1.0008]
            price = 100.0
            calls_expected_max = int(0.40 * 288)
            for i in range(288):
                price *= moves[i % 4]
                market.price = price
                sup.fast_tick()
                sup.slow_cycle()
                clock.advance(300)

            self.assertLessEqual(client.calls, calls_expected_max)
            self.assertGreaterEqual(client.calls, 1)
            stats = sup.summary()
            self.assertEqual(stats["slow_cycles"], 288)
            total = stats["cache_hits"] + stats["cache_misses"]
            self.assertEqual(total, 288)
            print(f"\n24h sim: {client.calls} Jev calls / 288 cycles = "
                  f"{client.calls / 288 * 100:.1f}% (budget 40%)")
            self.assertLessEqual(client.calls / 288, 0.40)


if __name__ == "__main__":
    unittest.main(verbosity=2)




