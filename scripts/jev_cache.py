#!/usr/bin/env python3
"""DecisionCache — M2 decision cache for Jev verdicts. Stdlib only, pure logic.

Skips redundant Jev calls. Three cooperating operations (the supervisor owns the
policy, the cache owns the data):

  lookup(state_json)          exact state-hash hit inside cache_ttl -> stored
                              verdict copy with cache_hit="exact", else None
  is_material(price_now, ...) False when |dprice|/price < cache_min_move AND the
                              last score is younger than cache_ttl -> the caller
                              reuses the last verdict (cache_hit="stale_price").
                              True on a big enough move, an age-out, or no score.
  store(state_json, verdict)  remember a fresh verdict with its timestamp and
                              scored price.

Counters (surfaced in logs and tests) are moved explicitly: note_hit("exact" |
"stale_price") for every reuse, note_miss() for every fresh score. Pure and
fully unit-testable without network; ``clock``/``now`` are injectable for tests.
"""
from __future__ import annotations

import copy
import hashlib
import time
from typing import Optional

HIT_KINDS = ("exact", "stale_price")


class DecisionCache:
    """In-memory verdict cache keyed by state hash (per symbol; states embed it)."""

    def __init__(self, cache_min_move: float = 0.0005, cache_ttl: float = 1800.0,
                 clock=time.time) -> None:
        self.cache_min_move = float(cache_min_move)
        self.cache_ttl = float(cache_ttl)
        self._clock = clock
        self._entries = {}  # state_sha256 -> (ts, verdict)
        self.last_verdict: Optional[dict] = None
        self.last_price: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.hit_count = 0
        self.miss_count = 0
        self.hit_exact_count = 0
        self.hit_stale_count = 0

    # -- hashing ---------------------------------------------------------

    @staticmethod
    def state_hash(state_json: str) -> str:
        return hashlib.sha256(str(state_json).encode("utf-8")).hexdigest()

    # -- operations ------------------------------------------------------

    def lookup(self, state_json: str, now: Optional[float] = None):
        """Exact state-hash hit within cache_ttl -> verdict copy (cache_hit set).

        Pure probe: no counters move here — the caller classifies the cycle.
        """
        ts = self._clock() if now is None else float(now)
        entry = self._entries.get(self.state_hash(state_json))
        if entry is None:
            return None
        stored_ts, verdict = entry
        if ts - stored_ts > self.cache_ttl:
            return None
        out = copy.deepcopy(verdict)
        out["cache_hit"] = "exact"
        return out

    def is_material(self, price_now, price_last_scored,
                    now: Optional[float] = None) -> bool:
        """True when a fresh score is warranted: price moved >= cache_min_move
        since the last score, OR the last score is older than cache_ttl, OR
        there is no usable price/score pair to compare (fail-safe: score)."""
        ts = self._clock() if now is None else float(now)
        if self.last_ts is None:
            return True  # nothing was ever scored: always material
        if ts - self.last_ts > self.cache_ttl:
            return True
        try:
            p_now = float(price_now)
            p_last = float(price_last_scored)
        except (TypeError, ValueError):
            return True
        if p_now <= 0 or p_last <= 0:
            return True
        move = abs(p_now - p_last) / p_last
        # exactly cache_min_move is material (False only below it); the tiny
        # tolerance absorbs float representation error at the boundary.
        return move + 1e-12 >= self.cache_min_move

    def store(self, state_json: str, verdict: dict, price=None,
              now: Optional[float] = None) -> None:
        """Remember ``verdict`` for ``state_json`` and as the last score."""
        ts = self._clock() if now is None else float(now)
        snapshot = copy.deepcopy(verdict)
        self._entries[self.state_hash(state_json)] = (ts, snapshot)
        self.last_verdict = snapshot
        self.last_price = float(price) if price is not None else None
        self.last_ts = ts

    # -- counters --------------------------------------------------------

    def note_hit(self, kind: str = "exact") -> None:
        if kind not in HIT_KINDS:
            raise ValueError(f"unknown hit kind: {kind!r}")
        self.hit_count += 1
        if kind == "exact":
            self.hit_exact_count += 1
        else:
            self.hit_stale_count += 1

    def note_miss(self) -> None:
        self.miss_count += 1

    def stats(self) -> dict:
        return {
            "hit_count": self.hit_count,
            "miss_count": self.miss_count,
            "hit_exact_count": self.hit_exact_count,
            "hit_stale_count": self.hit_stale_count,
        }
