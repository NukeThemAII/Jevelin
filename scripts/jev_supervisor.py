#!/usr/bin/env python3
"""Split-cadence supervisor (M2) — stdlib asyncio + ccxt public market data.

Replaces the single 5-min paper_loop cadence with two loops over the SAME
unchanged gates/books (paper_loop.py stays untouched as the deprecated
fallback):

  fast loop (--fast-interval, default 5 s): ticker poll -> mark books ->
    stop/liquidation closes via the EXISTING PerpsPortfolio auto-exits ->
    daily-kill/drawdown flags -> burst detection. Deterministic, free, ZERO
    Jev calls.
  slow loop (--slow-interval, default 300 s + burst trigger): refresh ->
    jev_state.build_state -> DecisionCache -> (reuse | Jev via jev_scorer) ->
    jev_gates.decide / jev_perps.decide_perps -> books -> JSONL + SQLite.

Decision cache (scripts/jev_cache.py): exact state-hash hits and immaterial
price moves reuse the last verdict (cache=hit_exact|hit_stale_price); only
material moves or TTL age-outs pay for a Jev call (cache=miss). Burst
(--burst-threshold 1-min return, --burst-trades 1-min trade count) forces an
immediate score and the next --burst-cycles slow cycles (burst=1).

Live store writes (M1 schema via jev_store/jev_import): every slow cycle
records its decision row and every fill lands in the trades table, in addition
to the unchanged JSONL logs — so jev_import/jev_replay keep working against
both sources.

Fail-open everywhere: per-cycle errors are logged, never raised; no fabricated
verdicts; deterministic money math lives in the existing gate/book modules.
Gate/book logic semantics are NOT modified here — this module only rewires
HOW and WHEN they are invoked (M2's explicit mandate).
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Make sibling modules importable when this file is run/imported directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import jev_import  # noqa: E402  (store row mapping, identical to the importer)
import jev_store  # noqa: E402
from jev_cache import DecisionCache  # noqa: E402
from jev_config import (  # noqa: E402
    FanoutConfig,
    MarketConfig,
    RegimeConfig,
    append_jsonl,
    new_decision_id,
)
from jev_gates import RiskConfig, decide  # noqa: E402
from jev_perps import decide_perps  # noqa: E402
from jev_questions import QUESTIONS  # noqa: E402
from jev_regime import classify  # noqa: E402
from jev_risk import PortfolioRisk  # noqa: E402
from jev_scorer import ShadowScorer  # noqa: E402
from jev_state import build_state  # noqa: E402
# Reused v1 cycle plumbing (paper_loop is imported, never modified).
from paper_loop import _decision_row, _ts_iso, _veto_status, fetch_funding_rate  # noqa: E402

BANGKOK = ZoneInfo("Asia/Bangkok")


class MarketData:
    """Binance public market data via ccxt + ring buffers (deque, maxlen ~600).

    ``poll_ticker`` runs per fast tick; ``refresh`` (OHLCV + trades) runs per
    slow cycle for the state builder. Pair-agnostic: every method takes the
    symbol. All reads fail open (None / stale / empty, never raise).
    """

    def __init__(self, exchange=None, maxlen: int = 600,
                 ohlcv_ttl_seconds: float = 60.0) -> None:
        self.exchange = exchange if exchange is not None else self._build_exchange()
        self._prices = {}
        self._trades = {}
        self._closes = {}
        self._ohlcv = {}  # (symbol, timeframe) -> (fetched_ms, rows) — M3 TTL cache
        self.ohlcv_ttl_ms = int(float(ohlcv_ttl_seconds) * 1000)
        self._maxlen = int(maxlen)

    @staticmethod
    def _build_exchange():
        import ccxt  # noqa: WPS433 (local import is intentional)

        return ccxt.binance({"enableRateLimit": True})

    def _price_buf(self, symbol):
        return self._prices.setdefault(symbol, deque(maxlen=self._maxlen))

    def _trade_buf(self, symbol):
        return self._trades.setdefault(symbol, deque(maxlen=self._maxlen))

    def poll_ticker(self, symbol, now_ms):
        """Latest public price; feeds the price ring. Falls back on error."""
        try:
            price = float(self.exchange.fetch_ticker(symbol)["last"])
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f"bad ticker price {price!r}")
        except Exception as exc:  # fail-open: reuse the last known price
            print(f"market: ticker error {symbol}: {type(exc).__name__}: {exc}")
            return self.last_price(symbol)
        self._price_buf(symbol).append((int(now_ms), price))
        return price

    def poll_tickers(self, symbols, now_ms):
        """One fetch_tickers call marking ALL pairs (M5: no per-pair hammering).

        Returns {symbol: price}; per-symbol problems fall back to the last
        known price. Returns None on a batch failure so the caller can fall
        back to per-symbol poll_ticker. Never raises.
        """
        try:
            tickers = self.exchange.fetch_tickers([str(s) for s in symbols])
        except Exception as exc:  # caller falls back to per-symbol polling
            print(f"market: tickers error: {type(exc).__name__}: {exc}")
            return None
        # ccxt keys tickers by UNIFIED symbol ("BTC/USDT") while the app-level
        # symbol is the raw market id ("BTCUSDT") — index both forms.
        by_norm = {str(k).replace("/", "").upper(): v for k, v in tickers.items()}
        prices = {}
        for symbol in symbols:
            try:
                ticker = by_norm.get(str(symbol).replace("/", "").upper())
                price = float(ticker["last"])
                if not math.isfinite(price) or price <= 0:
                    raise ValueError(f"bad ticker price {price!r}")
            except Exception as exc:  # fail-open: reuse the last known price
                print(f"market: ticker error {symbol}: {type(exc).__name__}: {exc}")
                price = self.last_price(symbol)
            if price is not None:
                self._price_buf(symbol).append((int(now_ms), price))
                prices[symbol] = price
        return prices

    def refresh(self, symbol, now_ms):
        """1m closes + recent trades for the state builder (slow cycle)."""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, "1m", limit=10)
            self._closes[symbol] = [float(c[4]) for c in ohlcv]
            raw = self.exchange.fetch_trades(symbol, limit=200)
            trades = [
                (int(t["timestamp"]), str(t["side"]).lower(),
                 float(t["price"]) * float(t["amount"]))
                for t in raw
            ]
            buf = self._trade_buf(symbol)
            buf.clear()  # the fetch window IS the state window (no double counts)
            buf.extend(trades)
        except Exception as exc:  # fail-open: keep the previous window
            print(f"market: refresh error {symbol}: {type(exc).__name__}: {exc}")

    def ohlcv(self, symbol, timeframe, limit, now_ms):
        """TTL-cached OHLCV rows (M3 regime inputs). Fail-open: stale/empty.

        One fetch per (symbol, timeframe) per ``ohlcv_ttl_ms`` — burst cycles
        reuse the same window instead of hammering the REST API.
        """
        key = (symbol, str(timeframe))
        entry = self._ohlcv.get(key)
        now = int(now_ms)
        if entry is not None and now - entry[0] < self.ohlcv_ttl_ms and entry[1]:
            return entry[1]
        try:
            rows = self.exchange.fetch_ohlcv(symbol, timeframe, limit=int(limit))
        except Exception as exc:  # fail-open: keep the previous window
            print(f"market: ohlcv error {symbol} {timeframe}: "
                  f"{type(exc).__name__}: {exc}")
            return entry[1] if entry is not None else []
        self._ohlcv[key] = (now, rows)
        return rows

    def closes(self, symbol):
        return list(self._closes.get(symbol) or [])

    def trades(self, symbol):
        return list(self._trade_buf(symbol))

    def last_price(self, symbol):
        buf = self._price_buf(symbol)
        return buf[-1][1] if buf else None

    def price_ago(self, symbol, ts_ms):
        """Latest ring price at or before ``ts_ms`` (None if no history)."""
        best = None
        for ts, price in self._price_buf(symbol):
            if ts <= int(ts_ms):
                best = price
        return best

    def trade_count_since(self, symbol, ts_ms):
        return sum(1 for ts, _side, _usd in self._trade_buf(symbol)
                   if ts >= int(ts_ms))


@dataclass
class BookPair:
    """One spot + one perps book per symbol (per-pair caps arrive in M5)."""

    spot: object
    perps: Optional[object] = None


class LogTailer:
    """Byte-offset tail reader for the JSONL audit logs (live store writes).

    Returns the (obj, raw_line) pairs appended since the previous capture so
    store rows are byte-identical to what jev_import would produce."""

    def __init__(self) -> None:
        self._offsets = {}

    def capture(self, path) -> list:
        out = []
        if path is None:
            return out
        p = Path(path)
        try:
            size = p.stat().st_size
        except OSError:
            return out
        key = str(p)
        start = self._offsets.get(key, 0)
        if size < start:
            start = 0  # truncated/rotated: reread from the top
        if size == start:
            return out
        try:
            with p.open("rb") as fh:
                fh.seek(start)
                data = fh.read()
            self._offsets[key] = size
        except OSError:
            return out
        for raw in data.decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append((obj, raw))
        return out


def _m3_decision_row(book, decision_id, symbol, now_ms, price, action, result, verdict,
                     equity, regime, risk_flags=None):
    """paper_loop._decision_row + M3 fields: regime + fan-out audit trail.

    M5 adds ``risk_flags``: active portfolio-risk flag names for the pair
    (global_daily_kill / drawdown_halt / pair_cap / basket_long / basket_short).
    """
    row = _decision_row(book, decision_id, symbol, now_ms, price, action, result,
                        verdict, equity)
    row["regime"] = regime
    row["risk_flags"] = list(risk_flags or [])
    v = verdict if isinstance(verdict, dict) else {}
    row["fan_out"] = int(v.get("fan_out") or 0)
    if "whipsaw_prob_2" in v:
        row["verdict"]["whipsaw_prob_2"] = v.get("whipsaw_prob_2")
    if "answers_2" in v:
        row["answers_2"] = v.get("answers_2")  # the second raw answer (M3)
    if v.get("fan_out_error"):
        row["fan_out_error"] = v.get("fan_out_error")
    return row


class Supervisor:
    """Split-cadence driver over the existing gates/books. Fail-open everywhere.

    ``market`` duck-types MarketData (poll_ticker/refresh/closes/trades/
    last_price/price_ago/trade_count_since); ``client`` duck-types JevClient
    (ask/cost_usd[/log_path]); ``books`` maps symbol -> BookPair. ``clock``
    returns epoch seconds and is injectable for simulated-time tests.
    """

    def __init__(self, symbols, market, client, books, conn=None,
                 funding_exchange=None, slow_interval: float = 300.0,
                 fast_interval: float = 5.0, burst_threshold: float = 0.003,
                 burst_trades: int = 150, burst_cycles: int = 2,
                 cache_min_move: float = 0.0005, cache_ttl: float = 1800.0,
                 clock=time.time, decision_log_path: str = "runtime/paper_decisions.jsonl",
                 once: bool = False, max_cycles: Optional[int] = None,
                 risk_cfg=None, regime_cfg=None, fanout_cfg=None,
                 market_cfg=None, portfolio_cfg=None,
                 risk_state_path: str = "runtime/risk_state.json") -> None:
        self.symbols = list(symbols)
        self.market = market
        self.client = client
        self.books = books
        self.conn = conn
        self.funding_exchange = funding_exchange
        self.slow_interval = float(slow_interval)
        self.fast_interval = float(fast_interval)
        self.burst_threshold = float(burst_threshold)
        self.burst_trades = int(burst_trades)
        self.burst_cycles = int(burst_cycles)
        self._clock = clock
        self.decision_log_path = str(decision_log_path)
        self.once = bool(once)
        self.max_cycles = int(max_cycles) if max_cycles is not None else None
        self.caches = {
            symbol: DecisionCache(cache_min_move=cache_min_move,
                                  cache_ttl=cache_ttl, clock=clock)
            for symbol in self.symbols
        }
        # M3 config sections (frozen dataclasses from jev_config / config/v2.yaml)
        self.risk_cfg = risk_cfg if risk_cfg is not None else RiskConfig()
        self.regime_cfg = regime_cfg if regime_cfg is not None else RegimeConfig()
        self.fanout_cfg = fanout_cfg if fanout_cfg is not None else FanoutConfig()
        self.market_cfg = market_cfg if market_cfg is not None else MarketConfig()
        # M5 portfolio risk (jev_risk): None = v1 behavior (no portfolio layer)
        self.risk = (PortfolioRisk(cfg=portfolio_cfg, state_path=risk_state_path,
                                   pairs=self.symbols)
                     if portfolio_cfg is not None else None)
        self._risk_error = None      # set on risk computation failure (fail-closed)
        self._risk_saved = None      # last persisted risk state (change-detector)
        self.scorer = ShadowScorer(client, exchange=getattr(market, "exchange", None),
                                   fanout=self.fanout_cfg)
        self.slow_results = []  # per-symbol result dicts, appended per cycle
        self._counts = {"slow_cycles": 0, "fast_ticks": 0, "jev_calls": 0}
        self._burst_remaining = {}
        self._burst_trigger = threading.Event()
        self._shutdown = threading.Event()
        self._flags = {}
        self._tailer = LogTailer()
        self._old_sig = None
        self._closed = False

    # -- clock / signals / lifecycle -------------------------------------

    def _time(self) -> float:
        return float(self._clock())

    def now_ms(self) -> int:
        return int(self._time() * 1000)

    def request_shutdown(self) -> None:
        self._shutdown.set()
        self._burst_trigger.set()  # wake any idle wait

    def install_signal_handlers(self) -> None:
        """SIGINT/SIGTERM -> graceful shutdown (no KeyboardInterrupt)."""
        if self._old_sig is not None:
            return

        def handler(signum, _frame):
            print(f"signal: {signum} received - shutting down gracefully")
            self.request_shutdown()

        self._old_sig = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_sig[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def restore_signal_handlers(self) -> None:
        for sig, old in (self._old_sig or {}).items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass
        self._old_sig = None

    def summary(self) -> dict:
        hits = sum(c.hit_count for c in self.caches.values())
        misses = sum(c.miss_count for c in self.caches.values())
        exact = sum(c.hit_exact_count for c in self.caches.values())
        stale = sum(c.hit_stale_count for c in self.caches.values())
        return {
            "slow_cycles": self._counts["slow_cycles"],
            "fast_ticks": self._counts["fast_ticks"],
            "jev_calls": self._counts["jev_calls"],
            "cache_hits": hits,
            "cache_misses": misses,
            "hit_exact": exact,
            "hit_stale_price": stale,
            "cost_usd": float(getattr(self.client, "cost_usd", 0.0) or 0.0),
            "risk_events": int(self.risk.risk_events) if self.risk is not None else 0,
        }

    def shutdown(self) -> dict:
        """Graceful stop: save books + risk state, flush rows, close DB, summary."""
        if self._closed:
            return self.summary()
        self._closed = True
        for symbol, pair in self.books.items():
            for book in (pair.spot, pair.perps):
                if book is None:
                    continue
                try:
                    book.save()
                except Exception as exc:
                    print(f"shutdown: save error {symbol}: {type(exc).__name__}: {exc}")
        if self.risk is not None:
            self._save_risk_state()
        self._capture_trade_rows()
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        summary = self.summary()
        stamp = datetime.now(BANGKOK).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"shutdown summary ({stamp} Asia/Bangkok): "
            f"slow_cycles={summary['slow_cycles']} fast_ticks={summary['fast_ticks']} "
            f"jev_calls={summary['jev_calls']} cost_usd={summary['cost_usd']:.6f} "
            f"cache_hits={summary['cache_hits']} (exact={summary['hit_exact']} "
            f"stale_price={summary['hit_stale_price']}) "
            f"cache_misses={summary['cache_misses']} "
            f"risk_events={summary['risk_events']} "
            f"pairs={','.join(self.symbols)}"
        )
        return summary

    # -- fast loop (free risk loop: no Jev, no state building) -------------

    def risk_flags(self, symbol, price=None) -> dict:
        """Daily-kill / drawdown flags from the marked books.

        Enforcement stays where v1 put it: the UNCHANGED gates already block
        entries under daily_loss_kill (exits/stops always allowed). The fast
        loop keeps the flags fresh at 5 s cadence and logs transitions so the
        operator (and M5's portfolio risk) can see them immediately.
        """
        pair = self.books[symbol]
        if price is None:
            price = self.market.last_price(symbol)
        flags = {"daily_loss_kill": False, "drawdown": False}
        books = [pair.spot] + ([pair.perps] if pair.perps is not None else [])
        for book in books:
            limit = getattr(getattr(book, "cfg", None), "daily_loss_limit_pct",
                            RiskConfig().daily_loss_limit_pct)
            pf = book.to_pf_state(price)
            if pf.daily_pnl_pct <= -limit:
                flags["daily_loss_kill"] = True
            if pf.equity_usd <= book.initial_equity_usd * (1.0 - limit / 100.0):
                flags["drawdown"] = True
        if self._flags.get(symbol) != flags:
            self._flags[symbol] = flags
            if any(flags.values()):
                on = ",".join(name for name, on_ in flags.items() if on_)
                print(f"risk: {_ts_iso(self.now_ms())} {symbol} flagged={on}")
        return flags

    # -- M5 portfolio risk (jev_risk) --------------------------------------

    def _books_snapshot(self):
        """({pair: {book: {notional, margin, side}}}, {pair: {book: equity}}).

        Notionals at current marks: spot qty x mark (always long), perps
        qty x mark + posted margin + side. Equities are the marked book
        equities (realized + unrealized).
        """
        books_state, equity_by_book = {}, {}
        for symbol, pair in self.books.items():
            price = self.market.last_price(symbol)
            bs, eq = {}, {}
            for name, book in (("spot", pair.spot), ("perps", pair.perps)):
                if book is None:
                    continue
                position = book.position or {}
                qty = float(position.get("qty") or 0.0)
                mark = (float(price) if price is not None
                        else float(position.get("entry_price") or 0.0))
                if name == "spot":
                    side = "long" if book.position else None
                    margin = 0.0
                else:
                    side = position.get("side")
                    margin = float(position.get("margin_usd") or 0.0)
                bs[name] = {"notional": qty * mark, "margin": margin, "side": side}
                eq[name] = book.mark_to_market(price)
            books_state[symbol] = bs
            equity_by_book[symbol] = eq
        return books_state, equity_by_book

    def portfolio_risk_update(self):
        """Refresh the M5 portfolio risk state from current marks (fail-open).

        On a computation error the failure is remembered and every entry
        budget fails CLOSED until the next successful update; exits are never
        affected.
        """
        if self.risk is None:
            return None
        try:
            books_state, equity_by_book = self._books_snapshot()
            flags = self.risk.update(books_state, equity_by_book, self._time())
            self._risk_error = None
            return flags
        except Exception as exc:
            self._risk_error = f"{type(exc).__name__}: {exc}"
            print(f"risk: update error: {self._risk_error}")
            return None

    def _save_risk_state(self) -> None:
        """Persist risk state when it changed (M0 atomic write, never raises)."""
        if self.risk is None:
            return
        state = self.risk._state_dict()
        if state == self._risk_saved:
            return
        if self.risk.save():
            self._risk_saved = state

    def _entry_budget(self, symbol, book, price, leverage=1.0):
        """M5 risk dict for the gate layer. Fail-CLOSED on error (entries only).

        None when the portfolio layer is off (v1 behavior). On any risk
        failure the returned dict has no capacities -> the gates veto
        pair_cap/basket_cap (entries blocked, exits unaffected).
        """
        if self.risk is None:
            return None
        if self._risk_error is not None:
            return {"error": self._risk_error, "global_daily_kill": False,
                    "drawdown_halt": False, "pair_remaining": None,
                    "basket_remaining_long": None,
                    "basket_remaining_short": None, "min_position_pct": None}
        try:
            pair = self.books[symbol]
            portfolio = pair.spot if book == "spot" else pair.perps
            equity = portfolio.mark_to_market(price)
            return self.risk.entry_budget(book, symbol, equity, leverage=leverage)
        except Exception as exc:
            print(f"risk: budget error {symbol} {book}: {type(exc).__name__}: {exc}")
            return {"error": f"{type(exc).__name__}: {exc}",
                    "global_daily_kill": False, "drawdown_halt": False,
                    "pair_remaining": None, "basket_remaining_long": None,
                    "basket_remaining_short": None, "min_position_pct": None}

    def _risk_flags_for(self, symbol) -> list:
        """Active portfolio-risk flag names for the pair (cycle lines + rows)."""
        if self.risk is None:
            return []
        flags = self.risk.flags
        names = [name for name in ("global_daily_kill", "drawdown_halt",
                                   "basket_long", "basket_short")
                 if flags.get(name)]
        if flags.get("pair_cap", {}).get(symbol):
            names.append("pair_cap")
        return names

    def check_burst(self, symbol, now_ms) -> bool:
        """|1-min return| > burst_threshold OR 1-min trades > burst_trades."""
        try:
            p_now = self.market.last_price(symbol)
            p_ago = self.market.price_ago(symbol, int(now_ms) - 60_000)
            if p_now and p_ago and float(p_ago) > 0:
                ret = abs(float(p_now) - float(p_ago)) / float(p_ago)
                if ret > self.burst_threshold:
                    return True
        except Exception:
            pass
        try:
            return self.market.trade_count_since(symbol, int(now_ms) - 60_000) \
                > self.burst_trades
        except Exception:
            return False

    def _start_burst(self, symbol) -> None:
        """Force-score NOW plus the next burst_cycles slow cycles (burst=1)."""
        self._burst_remaining[symbol] = self.burst_cycles + 1
        self._burst_trigger.set()

    def _fast_risk(self, symbol, price, now_ms) -> list:
        """Mark books, fire stop/liq closes via the existing auto-exit API."""
        closed = []
        pair = self.books[symbol]
        self.risk_flags(symbol, price=price)
        perps = pair.perps
        if perps is not None and perps.position is not None:
            decision_id = new_decision_id()
            action = {"action": "skip", "reason": "fast risk tick",
                      "decision_id": decision_id}
            result = perps.apply_action(action, symbol, price, now_ms,
                                        decision_id=decision_id)
            if result.get("executed"):
                perps.save()
                print(
                    f"fast: {_ts_iso(now_ms)} decision={decision_id} perps {symbol} "
                    f"price={price:.4f} closed={result.get('detail')} "
                    f"realized_pnl={result.get('realized_pnl', 0.0):.6f} "
                    f"fees={result.get('fees', 0.0):.6f}"
                )
                closed.append({"symbol": symbol, "detail": result.get("detail"),
                               "decision_id": decision_id})
        return closed

    def _poll_prices(self, now_ms) -> dict:
        """Marks for ALL pairs: one fetch_tickers call when available (M5).

        Falls back to per-symbol poll_ticker when the market duck-type has no
        batch method (tests) or the batch call fails. Returns {symbol: price}.
        """
        batch = getattr(self.market, "poll_tickers", None)
        if callable(batch):
            try:
                result = batch(self.symbols, now_ms)
                if result is not None:
                    return dict(result)
            except Exception as exc:  # fall back to per-symbol polling
                print(f"market: tickers error: {type(exc).__name__}: {exc}")
        prices = {}
        for symbol in self.symbols:
            try:
                price = self.market.poll_ticker(symbol, now_ms)
            except Exception as exc:  # fail-open: the fast loop must never die
                print(f"fast: tick error {symbol}: {type(exc).__name__}: {exc}")
                continue
            if price is not None:
                prices[symbol] = price
        return prices

    def fast_tick(self) -> dict:
        """One fast-loop iteration: mark, stops/liq, flags, burst detect."""
        now_ms = self.now_ms()
        self._counts["fast_ticks"] += 1
        out = {"ts_ms": now_ms, "prices": {}, "closed": [], "flags": {}}
        prices = self._poll_prices(now_ms)
        for symbol in self.symbols:
            try:
                price = prices.get(symbol)
                if price is None:
                    continue
                out["prices"][symbol] = price
                out["closed"].extend(self._fast_risk(symbol, price, now_ms))
                out["flags"][symbol] = self.risk_flags(symbol, price=price)
                if self.check_burst(symbol, now_ms) \
                        and self._burst_remaining.get(symbol, 0) <= 0:
                    self._start_burst(symbol)
                    print(f"burst: {_ts_iso(now_ms)} {symbol} price={price:.4f} "
                          f"spike detected -> force-score")
            except Exception as exc:  # fail-open: the fast loop must never die
                print(f"fast: tick error {symbol}: {type(exc).__name__}: {exc}")
        if self.risk is not None:  # M5: refresh + persist portfolio risk flags
            out["risk"] = self.portfolio_risk_update()
            self._save_risk_state()
        if out["closed"]:
            self._capture_trade_rows()
        return out

    # -- live store writes (M1 schema; identical mapping to jev_import) ----

    def _write_decision_rows(self, symbol, state_str, verdict, decision_id,
                             cache_hit, cost) -> None:
        """One decisions row per scored cycle (miss AND cache-hit cycles).

        Miss cycles on a real client take the row from the client's own audit
        line (byte-identical to a jev_import run); cache hits / mocked clients
        get a synthetic row carrying the same fields plus the cache marker.
        """
        if self.conn is None:
            return
        try:
            lines = self._tailer.capture(getattr(self.client, "log_path", None))
            if lines:
                for obj, raw in lines:
                    jev_store.upsert_decision(
                        self.conn, jev_import.decision_row(obj, raw, 0))
                return
            answers = verdict.get("answers")
            obj = {
                "ts": self._time(),
                "decision_id": decision_id,
                "symbol": symbol,
                "state_sha256": DecisionCache.state_hash(state_str),
                "questions": QUESTIONS,
                "raw": {
                    "answers": answers if isinstance(answers, dict) else None,
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cost": float(cost or 0.0)},
                    "cache_hit": cache_hit,
                },
                "error": verdict.get("error"),
                "latency_ms": verdict.get("latency_ms"),
            }
            raw_line = json.dumps(obj, default=str)
            jev_store.upsert_decision(
                self.conn, jev_import.decision_row(obj, raw_line, 0))
        except Exception as exc:  # logging must never break trading
            print(f"store: decision write error: {type(exc).__name__}: {exc}")

    def _capture_trade_rows(self) -> None:
        """Every fill appended to the books' trade logs lands in the store."""
        if self.conn is None:
            return
        try:
            for pair in self.books.values():
                for book in (pair.spot, pair.perps):
                    if book is None:
                        continue
                    path = Path(str(book.state_path) + ".trades.jsonl")
                    for obj, raw in self._tailer.capture(path):
                        jev_store.upsert_trade(
                            self.conn, jev_import.trade_row(obj, raw, 0, path))
        except Exception as exc:  # logging must never break trading
            print(f"store: trade write error: {type(exc).__name__}: {exc}")

    # -- slow loop (5-min verdicts + burst trigger) -----------------------

    def slow_cycle(self) -> list:
        """One slow-loop iteration for every pair (one verdict -> both books)."""
        results = [self._slow_one(symbol) for symbol in self.symbols]
        self._counts["slow_cycles"] += 1
        self.slow_results.extend(results)
        return results

    def _slow_one(self, symbol) -> dict:
        now_ms = self.now_ms()
        decision_id = new_decision_id()
        cache = self.caches[symbol]
        pair = self.books[symbol]
        self._burst_trigger.clear()  # consumed by this cycle
        try:
            self.portfolio_risk_update()  # M5: fresh flags/capacities per pair
            self.market.refresh(symbol, now_ms)
            state = build_state(symbol, self.market.closes(symbol),
                                self.market.trades(symbol), now_ms)
            state_str = json.dumps(state, separators=(",", ":"), sort_keys=True)
            regime = self._compute_regime(symbol, now_ms)
            price = self.market.last_price(symbol)
            if price is None:
                print(f"slow: {_ts_iso(now_ms)} decision={decision_id} {symbol} "
                      f"no price - skip cycle")
                return {"symbol": symbol, "ts_ms": now_ms,
                        "decision_id": decision_id, "cache_hit": "miss",
                        "burst": 0, "cost": 0.0, "error": "no price"}

            # Burst: a spike (re-detected now) or an active burst window forces
            # a fresh score regardless of cache materiality (burst=1).
            spike = self.check_burst(symbol, now_ms)
            active = self._burst_remaining.get(symbol, 0) > 0
            if spike and not active:
                self._start_burst(symbol)
                active = True
            burst = 1 if (spike or active) else 0

            cost_before = float(getattr(self.client, "cost_usd", 0.0) or 0.0)
            if burst:
                self._burst_remaining[symbol] = \
                    max(0, self._burst_remaining.get(symbol, 0) - 1)
                cache_hit, verdict = self._score(cache, symbol, state_str, price,
                                                 decision_id)
            else:
                verdict = cache.lookup(state_str, now=self._time())
                if verdict is not None:
                    cache.note_hit("exact")
                    cache_hit = "hit_exact"
                elif cache.last_verdict is not None and not cache.is_material(
                        price, cache.last_price, now=self._time()):
                    verdict = copy.deepcopy(cache.last_verdict)
                    cache.note_hit("stale_price")
                    cache_hit = "hit_stale_price"
                else:
                    cache_hit, verdict = self._score(cache, symbol, state_str,
                                                     price, decision_id)
            cost = float(getattr(self.client, "cost_usd", 0.0) or 0.0) - cost_before
            verdict["decision_id"] = decision_id
            verdict["cache_hit"] = cache_hit

            self._write_decision_rows(symbol, state_str, verdict, decision_id,
                                      cache_hit, cost)
            spot_out = self._run_spot(verdict, pair, symbol, price, now_ms,
                                      decision_id, cache_hit, burst, cost, regime)
            perps_out = self._run_perps(verdict, pair, symbol, price, now_ms,
                                        decision_id, cache_hit, burst, cost, regime)
            self._capture_trade_rows()
            return {"symbol": symbol, "ts_ms": now_ms,
                    "decision_id": decision_id, "cache_hit": cache_hit,
                    "burst": burst, "cost": cost, "verdict": verdict,
                    "regime": regime, "spot": spot_out, "perps": perps_out}
        except Exception as exc:  # fail-open: one pair's error never breaks the loop
            print(f"slow: cycle error {symbol}: {type(exc).__name__}: {exc} "
                  f"decision={decision_id}")
            return {"symbol": symbol, "ts_ms": now_ms,
                    "decision_id": decision_id, "cache_hit": "miss",
                    "burst": 0, "cost": 0.0,
                    "error": f"{type(exc).__name__}: {exc}"}

    def _compute_regime(self, symbol, now_ms) -> str:
        """Deterministic regime (M3, free). Fail-open -> "chop" (no entries)."""
        try:
            rows_15m = self.market.ohlcv(symbol, "15m",
                                         self.market_cfg.ohlcv_15m_limit, now_ms)
            rows_1h = self.market.ohlcv(symbol, "1h",
                                        self.market_cfg.ohlcv_1h_limit, now_ms)
            return classify(rows_15m, rows_1h, self.regime_cfg)
        except Exception as exc:  # fail-open: unknown regime forbids entries
            print(f"regime: classify error {symbol}: {type(exc).__name__}: {exc}")
            return "chop"

    def _score(self, cache, symbol, state_str, price, decision_id):
        """Fresh Jev score (the only path that spends money). Returns
        ("miss", verdict)."""
        cache.note_miss()
        self._counts["jev_calls"] += 1
        verdict = self.scorer.score_state(state_str, symbol,
                                          decision_id=decision_id)
        # M3 fan-out: a 2nd sample in the whipsaw band is a 2nd Jev call
        self._counts["jev_calls"] += int(verdict.get("fan_out") or 0)
        if verdict.get("ok"):
            cache.store(state_str, verdict, price=price, now=self._time())
        return "miss", verdict

    # -- gates + books (paper_loop semantics; line shape + cache/burst/cost) --

    def _run_spot(self, verdict, pair, symbol, price, now_ms, decision_id,
                  cache_hit, burst, cost, regime=None) -> dict:
        """Spot book: decide + apply + save + log (same as paper_loop.run_cycle)."""
        try:
            portfolio = pair.spot
            cfg = self.risk_cfg
            pf = portfolio.to_pf_state(price)  # pre-trade state drives the decision
            budget = self._entry_budget(symbol, "spot", price)  # M5 (None = v1)
            action = decide(verdict, pf, cfg, now_ms, decision_id=decision_id,
                            regime=regime, risk=budget)
            result = portfolio.apply_action(action, symbol, price, now_ms,
                                            decision_id=decision_id)
            portfolio.save()
            pf_line = portfolio.to_pf_state(price)  # post-trade status for the line
            risk_names = self._risk_flags_for(symbol)
            print(
                f"spot: {_ts_iso(now_ms)} decision={decision_id} {symbol} "
                f"price={price:.4f} regime={regime} "
                f"risk={','.join(risk_names) if risk_names else 'ok'} "
                f"fan_out={int((verdict or {}).get('fan_out') or 0)} "
                f"action={action['action']} "
                f"executed={result['executed']} {_veto_status(action)} "
                f"fees={result.get('fees', 0.0):.6f} "
                f"slippage={result.get('slippage', 0.0):.6f} "
                f"equity={pf_line.equity_usd:.2f} has_position={pf_line.has_position} "
                f"daily_pnl_pct={pf_line.daily_pnl_pct:.4f} "
                f"cache={cache_hit} burst={burst} cost={cost:.6f}"
            )
            append_jsonl(self.decision_log_path, _m3_decision_row(
                "spot", decision_id, symbol, now_ms, price, action, result,
                verdict, pf_line.equity_usd, regime, risk_flags=risk_names))
            return {"action": action, "result": result,
                    "equity": pf_line.equity_usd,
                    "has_position": pf_line.has_position,
                    "daily_pnl_pct": pf_line.daily_pnl_pct}
        except Exception as exc:  # fail-open: spot problems never break the loop
            print(f"spot: cycle error: {type(exc).__name__}: {exc} "
                  f"decision={decision_id}")
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _run_perps(self, verdict, pair, symbol, price, now_ms, decision_id,
                   cache_hit, burst, cost, regime=None):
        """Perps book: decide_perps + apply + save + log (paper_loop semantics)."""
        if pair.perps is None:
            return None
        try:
            perps_portfolio = pair.perps
            perps_cfg = perps_portfolio.cfg
            funding_rate = fetch_funding_rate(self.funding_exchange, symbol)
            pf = perps_portfolio.to_pf_state(price)  # pre-trade state drives the decision
            budget = self._entry_budget(symbol, "perps", price,  # M5 (None = v1)
                                        leverage=perps_cfg.max_leverage)
            action = decide_perps(verdict, pf, perps_cfg, now_ms, funding_rate,
                                  decision_id=decision_id, regime=regime,
                                  risk=budget)
            result = perps_portfolio.apply_action(action, symbol, price, now_ms,
                                                  funding_rate,
                                                  decision_id=decision_id)
            perps_portfolio.save()
            pf_line = perps_portfolio.to_pf_state(price)

            funding_str = "na" if funding_rate is None else f"{funding_rate:.6f}"
            risk_names = self._risk_flags_for(symbol)
            print(
                f"perps: {_ts_iso(now_ms)} decision={decision_id} {symbol} "
                f"price={price:.4f} regime={regime} "
                f"risk={','.join(risk_names) if risk_names else 'ok'} "
                f"fan_out={int((verdict or {}).get('fan_out') or 0)} "
                f"funding={funding_str} action={action['action']} "
                f"executed={result['executed']} detail={result['detail']} "
                f"{_veto_status(action)} "
                f"fees={result.get('fees', 0.0):.6f} "
                f"slippage={result.get('slippage', 0.0):.6f} "
                f"equity={pf_line.equity_usd:.2f} side={pf_line.side} "
                f"daily_pnl_pct={pf_line.daily_pnl_pct:.4f} "
                f"cache={cache_hit} burst={burst} cost={cost:.6f}"
            )
            append_jsonl(self.decision_log_path, _m3_decision_row(
                "perps", decision_id, symbol, now_ms, price, action, result,
                verdict, pf_line.equity_usd, regime, risk_flags=risk_names))
            return {"funding_rate": funding_rate, "action": action,
                    "result": result, "equity": pf_line.equity_usd,
                    "has_position": pf_line.has_position, "side": pf_line.side,
                    "daily_pnl_pct": pf_line.daily_pnl_pct}
        except Exception as exc:  # fail-open: perps problems never break the spot book
            print(f"perps: cycle error: {type(exc).__name__}: {exc} "
                  f"decision={decision_id}")
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- asyncio loops ----------------------------------------------------

    async def _sleep(self, seconds) -> None:
        """Interruptible sleep (wakes once per second to check the flags)."""
        deadline = self._time() + float(seconds)
        while not self._shutdown.is_set():
            remaining = deadline - self._time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 1.0))

    async def _wait_slow(self) -> None:
        """Slow-loop idle: wait for the interval OR an early burst trigger."""
        deadline = self._time() + self.slow_interval
        while not self._shutdown.is_set() and not self._burst_trigger.is_set():
            remaining = deadline - self._time()
            if remaining <= 0:
                return
            await self._sleep(min(remaining, 1.0))

    async def run_fast_loop(self) -> None:
        while not self._shutdown.is_set():
            self.fast_tick()
            if self._shutdown.is_set():
                break
            await self._sleep(self.fast_interval)

    async def run_slow_loop(self) -> None:
        cycles = 0
        while not self._shutdown.is_set():
            self.slow_cycle()
            cycles += 1
            if self.max_cycles is not None and cycles >= self.max_cycles:
                self.request_shutdown()
                break
            if self._shutdown.is_set():
                break
            await self._wait_slow()

    async def run(self) -> dict:
        """Run both loops until shutdown; always ends with a graceful close."""
        self.install_signal_handlers()
        try:
            if self.once:
                self.fast_tick()   # one slow cycle + N fast ticks, then exit
                self.slow_cycle()
                self.fast_tick()
            else:
                await asyncio.gather(self.run_fast_loop(), self.run_slow_loop())
        finally:
            return self.shutdown()








