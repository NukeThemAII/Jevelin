#!/usr/bin/env python3
"""Heartbeat-staleness watchdog — run from cron, not from the supervisor.

The supervisor can't alert on its own staleness (if it's hung or dead it
isn't running any code, including an alert send), so this is an external
check: read the last line of runtime/heartbeat.jsonl, compare its ts to
now, and fire a Telegram alert on a fresh|stale TRANSITION only (state file
next to the heartbeat prevents re-alerting every cron tick while still down).

  .venv/bin/python scripts/jev_heartbeat_watchdog.py
  .venv/bin/python scripts/jev_heartbeat_watchdog.py --stale-seconds 120

Fail-open throughout: a missing/corrupt heartbeat file or send failure never
raises past main() (cron must never see a non-zero-looking crash loop here).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jev_telegram  # noqa: E402

DEFAULT_STALE_SECONDS = 120.0


def last_heartbeat_ts(heartbeat_path: Path) -> Optional[float]:
    """Epoch seconds of the last heartbeat line, or None (missing/empty/bad)."""
    try:
        with open(heartbeat_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            chunk = min(size, 4096)
            fh.seek(-chunk, 2)
            tail = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        obj = json.loads(lines[-1])
        return float(obj["ts"]) / 1000.0
    except (ValueError, KeyError, TypeError):
        return None


def load_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text())
    except (OSError, ValueError):
        return {"stale": False}


def save_state(state_path: Path, state: dict) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
    except OSError:
        pass


def check(heartbeat_path: Path, state_path: Path, stale_seconds: float,
          alerter, now: Optional[float] = None) -> dict:
    """One check: returns {'stale', 'age_s', 'alerted'}. Never raises."""
    now = float(now) if now is not None else time.time()
    last_ts = last_heartbeat_ts(heartbeat_path)
    age = (now - last_ts) if last_ts is not None else None
    stale = age is None or age > stale_seconds
    state = load_state(state_path)
    was_stale = bool(state.get("stale"))
    alerted = False
    if stale != was_stale:
        if alerter is not None:
            try:
                if stale:
                    detail = ("no heartbeat file/lines yet" if age is None
                               else f"{age:.0f}s since last tick")
                    text = f"Jevelin: heartbeat STALE ({detail})"
                else:
                    text = f"Jevelin: heartbeat recovered ({age:.0f}s since last tick)"
                alerter.send(text)
                alerted = True
            except Exception as exc:
                print(f"watchdog: alert send error: {type(exc).__name__}: {exc}")
        state["stale"] = stale
        save_state(state_path, state)
    return {"stale": stale, "age_s": age, "alerted": alerted}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--heartbeat", default="runtime/heartbeat.jsonl")
    p.add_argument("--state", default="runtime/watchdog_state.json")
    p.add_argument("--stale-seconds", type=float, default=DEFAULT_STALE_SECONDS)
    args = p.parse_args(argv)
    try:
        alerter = jev_telegram.from_env()
    except FileNotFoundError:
        alerter = None
    result = check(Path(args.heartbeat), Path(args.state),
                   args.stale_seconds, alerter)
    print(f"watchdog: stale={result['stale']} age_s={result['age_s']} "
          f"alerted={result['alerted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
