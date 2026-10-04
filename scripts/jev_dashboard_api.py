#!/usr/bin/env python3
"""jev_dashboard_api — read-only FastAPI backend for the v0 dashboard.

Serves parsed history from runtime/*.jsonl (decisions + trades) and the
static dashboard UI. No writes, no broker access, no live trading hooks —
strictly a view over logs already on disk. Binds to 127.0.0.1 only; this is
v0 scope (localhost), so no auth gate is added (see AGENTS.md admin-gate
convention for anything exposed beyond localhost).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"

DECISION_FILES = {
    "paper": RUNTIME / "paper_decisions.jsonl",
    "live": RUNTIME / "jev_decisions.jsonl",
}

app = FastAPI(title="Jevelin Dashboard API (v0, read-only)")

_cache: dict[str, Any] = {"mtimes": {}, "rows": {}}


def _load_source(name: str) -> list[dict]:
    path = DECISION_FILES[name]
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    if _cache["mtimes"].get(name) == mtime:
        return _cache["rows"][name]
    rows = []
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    _cache["mtimes"][name] = mtime
    _cache["rows"][name] = rows
    return rows


def _all_decisions() -> list[dict]:
    out = []
    for name in DECISION_FILES:
        out.extend(_load_source(name))
    out.sort(key=lambda d: d.get("ts_ms", 0))
    return out


def _trade_files() -> dict[str, Path]:
    return {p.stem.replace(".json.trades", ""): p for p in RUNTIME.glob("*.trades.jsonl")}


def _load_trades(key: str) -> list[dict]:
    path = _trade_files().get(key)
    if not path or not path.exists():
        return []
    rows = []
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


@app.get("/api/meta")
def meta():
    decisions = _all_decisions()
    books = sorted({d.get("book") for d in decisions if d.get("book")})
    symbols = sorted({d.get("symbol") for d in decisions if d.get("symbol")})
    trade_keys = sorted(_trade_files().keys())
    return {
        "books": books,
        "symbols": symbols,
        "trade_series": trade_keys,
        "decision_count": len(decisions),
        "generated_at_ms": int(time.time() * 1000),
    }


@app.get("/api/equity")
def equity(book: str = Query(...), symbol: str | None = None, max_points: int = 500):
    decisions = [d for d in _all_decisions() if d.get("book") == book]
    if symbol:
        decisions = [d for d in decisions if d.get("symbol") == symbol]
    points = [{"ts_ms": d["ts_ms"], "equity": d["equity"]} for d in decisions if d.get("equity") is not None]
    if len(points) > max_points:
        step = len(points) / max_points
        points = [points[int(i * step)] for i in range(max_points)]
    return JSONResponse(points)


@app.get("/api/decisions")
def decisions(
    book: str | None = None,
    symbol: str | None = None,
    vetoed_by: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    rows = _all_decisions()
    if book:
        rows = [d for d in rows if d.get("book") == book]
    if symbol:
        rows = [d for d in rows if d.get("symbol") == symbol]
    if vetoed_by:
        rows = [d for d in rows if vetoed_by in (d.get("vetoed_by") or [])]
    rows = list(reversed(rows))
    total = len(rows)
    page = rows[offset : offset + min(limit, 500)]
    return {"total": total, "rows": page}


@app.get("/api/veto-breakdown")
def veto_breakdown(book: str | None = None, symbol: str | None = None):
    rows = _all_decisions()
    if book:
        rows = [d for d in rows if d.get("book") == book]
    if symbol:
        rows = [d for d in rows if d.get("symbol") == symbol]
    counts: dict[str, int] = {}
    for d in rows:
        for cat in d.get("vetoed_by") or []:
            counts[cat] = counts.get(cat, 0) + 1
    breakdown = sorted(({"category": k, "count": v} for k, v in counts.items()), key=lambda x: -x["count"])
    return {"total_decisions": len(rows), "breakdown": breakdown}


@app.get("/api/trades")
def trades(series: str = Query(...)):
    rows = _load_trades(series)
    return {"series": series, "count": len(rows), "rows": rows}


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="dashboard")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8777)
