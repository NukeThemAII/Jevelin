#!/usr/bin/env python3
"""Jev (TypeSafe System One) decision client — stdlib only, fail-open.

POST https://openrouter.ai/api/alpha/decisions with Authorization: Bearer <key>.
Key is read from repo-root .env (OPENROUTER_API_KEY) via load_api_key(), or
passed straight to JevClient(api_key=...). NEVER printed or logged.

Verified live 2026-09-25 (see scripts/jev_probe.py). No fabricated answers:
a failed call returns ok=False with an error string and answers=None.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = ROOT / "runtime" / "jev_decisions.jsonl"


def load_api_key(env_path: Optional[Path] = None) -> str:
    """Return OPENROUTER_API_KEY from repo-root .env. Never print the value."""
    path = Path(env_path) if env_path else ROOT / ".env"
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise KeyError(f"OPENROUTER_API_KEY not found in {path}")


class JevClient:
    """Small decision-API client. Fail-open: any failure yields ok=False."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 2.0,
        log_path: Optional[Path] = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    def __repr__(self) -> str:  # never expose the key
        return f"JevClient(model={self.model!r}, calls={self.calls})"

    def ask(self, state: str, questions: dict) -> dict:
        """One decision call. Returns ok/answers/error/usage/raw/latency_ms."""
        started = time.monotonic()
        self.calls += 1
        body = json.dumps(
            {"model": self.model, "state": state, "questions": questions}
        ).encode("utf-8")
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        result: dict = {
            "ok": False,
            "answers": None,
            "error": None,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cost": 0.0},
            "raw": None,
            "latency_ms": 0.0,
        }

        data, error = self._post_with_retry(request)
        if data is not None:
            answers = data.get("answers") or {}
            usage = data.get("usage") or {}
            usage_out = {
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "cost": float(usage.get("cost", 0.0)),
            }
            self.input_tokens += usage_out["input_tokens"]
            self.output_tokens += usage_out["output_tokens"]
            self.cost_usd += usage_out["cost"]
            result.update(
                ok=True, answers=answers, usage=usage_out, raw=data, error=None
            )
        else:
            result["error"] = error

        result["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        self._write_log(state, questions, result)
        return result

    # -- internals ---------------------------------------------------------

    def _post_with_retry(self, request) -> tuple:
        """Return (data, error). ONE retry on transport / 5xx; never on 4xx."""
        last_error = "unknown error"
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8")), None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = f"HTTP {exc.code}: {detail}"
                if 400 <= exc.code < 500:
                    return None, last_error  # no retry on 4xx
                if attempt == 1:
                    return None, last_error
            except Exception as exc:  # URLError, timeout, ConnectionError, ...
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 1:
                    return None, last_error
        return None, last_error

    def _write_log(self, state: str, questions: dict, result: dict) -> None:
        """Append one JSONL line per ask. Logging must never break a call."""
        line = {
            "ts": time.time(),
            "state_sha256": hashlib.sha256(state.encode("utf-8")).hexdigest(),
            "questions": questions,
            "raw": result["raw"],
            "error": result["error"],
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=str) + "\n")
        except OSError:
            pass


def _self_check() -> None:  # pragma: no cover - manual sanity only
    key = load_api_key()
    client = JevClient(key)
    print(client)  # repr must not contain the key


if __name__ == "__main__":  # pragma: no cover
    _self_check()
