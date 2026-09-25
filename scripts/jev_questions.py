#!/usr/bin/env python3
"""Jev question set for short-horizon pump/dump shadow scoring.

Port of the CoinGecko Pump Pulse idea + our gate questions. Sent as-is to
JevClient.ask(state, QUESTIONS). Criteria sizes drive score normalization:
score is on 0..n_crit-1, mapped to 0..100 by the scorer.
"""
from __future__ import annotations

QUESTIONS = {
    "pump": {
        "type": "score",
        "instructions": (
            "Strength of an ongoing or imminent pump right now, from the last "
            "seconds-to-minutes of trading: short-horizon returns, buy share of "
            "flow, accelerating trade rate, oversized buys, price pressing its "
            "5m high."
        ),
        "criteria": ["none", "building", "strong", "euphoric"],
    },
    "dump": {
        "type": "score",
        "instructions": (
            "Strength of an ongoing or imminent dump or exit right now: falling "
            "short-horizon returns, sell share of flow, oversized sells, price "
            "breaking toward its 5m low, a few wallets dominating volume."
        ),
        "criteria": ["none", "building", "strong", "capitulating"],
    },
    "phase": {
        "type": "choice",
        "instructions": "Which market phase best describes the last few minutes?",
        "criteria": {
            "accumulation": "quiet steady buying, price holding",
            "breakout": "price breaking up on rising activity",
            "distribution": "heavy selling into strength, price stalling near highs",
            "capitulation": "sharp fall on heavy selling",
            "ranging": "no clear direction",
        },
    },
    "exhaustion": {
        "type": "noul",
        "instructions": "Is the current move losing steam and likely to reverse soon?",
    },
    "whipsaw": {
        "type": "noul",
        "instructions": (
            "Is the current breakout likely a fakeout that will retrace within "
            "a few minutes?"
        ),
    },
}

# n_crit for score questions — normalization uses score/(n_crit-1)*100
CRITERIA_SIZES = {
    "pump": 4,
    "dump": 4,
}
