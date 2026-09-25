#!/usr/bin/env python3
"""Jev (TypeSafe System One) probe — verified live 2026-09-25 against OpenRouter.

Route: POST https://openrouter.ai/api/alpha/decisions  (the "decisions" wrapper;
chat/completions rejects this model class). Model slug is VERSIONED on OpenRouter:
typesafe/jev-1.13 — 'typesafe/jev-latest' is invalid there.

Usage:
    .venv/bin/python scripts/jev_probe.py                # default fan-out demo
    .venv/bin/python scripts/jev_probe.py --state '...'   # custom state string

Reads OPENROUTER_API_KEY and JEVELIN_MODEL from .env. Prints the raw answer JSON
plus measured cost — never fabricates or post-processes model output.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

DEFAULT_QUESTIONS = {
    "entry_quality": {
        "type": "choice",
        "instructions": "Given this state, rate the quality of a long entry now",
        "criteria": {
            "skip": "Setup is invalid or low probability",
            "normal": "Textbook setup",
            "high": "Exceptional confluence",
        },
    },
    "whipsaw": {
        "type": "noul",
        "instructions": "Is this EMA crossover likely a whipsaw / fakeout?",
    },
    "regime": {
        "type": "score",
        "instructions": "Market regime",
        "criteria": ["ranging/mean-reverting", "transitioning", "strong trend"],
    },
}

DEFAULT_STATE = json.dumps({
    "pair": "BTC/EUR", "timeframe": "2h",
    "last_close": 74122, "rsi_14": 28.5,
    "ema12": 74410, "ema26": 74380, "ema_crossed_up_candles_ago": 2,
    "range_24h": [72800, 75100], "volume_vs_avg": 0.8,
    "position": "none", "strategy": "RSI oversold + recent EMA bullish cross",
})


def load_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    return env


def ask(key: str, model: str, state: str, questions: dict) -> dict:
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return {"error": {"http_status": exc.code, "body": exc.read().decode()[:500]}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the Jev decisions API")
    parser.add_argument("--state", default=DEFAULT_STATE, help="State string or JSON string")
    parser.add_argument("--model", default=None, help="Override model slug")
    args = parser.parse_args()

    env = load_env()
    key = env.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY missing in .env", file=sys.stderr)
        return 1
    model = args.model or env.get("JEVELIN_MODEL", "typesafe/jev-1.13")

    result = ask(key, model, args.state, DEFAULT_QUESTIONS)
    print(json.dumps(result, indent=2))
    usage = result.get("usage") or {}
    if usage:
        print(f"\nmodel={result.get('model')} provider={result.get('provider')} "
              f"cost=${usage.get('cost')} in={usage.get('input_tokens')} out={usage.get('output_tokens')}",
              file=sys.stderr)
    return 0 if "answers" in result else 1


if __name__ == "__main__":
    raise SystemExit(main())
