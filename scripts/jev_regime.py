#!/usr/bin/env python3
"""jev_regime (M3) — deterministic regime classifier, pure OHLCV math. Stdlib only.

``classify(ohlcv_15m, ohlcv_1h, cfg) -> "chop" | "trend_up" | "trend_down"``

Inputs are ccxt-style OHLCV rows ``[ts, open, high, low, close, volume]``.
No Jev, no network: this runs before every slow cycle and is free.

Features (all configurable via RegimeConfig / config/v2.yaml):
  * realized vol vs ATR expectation on 15m: stdev of the last ``vol_window``
    log returns divided by ATR(``atr_period``)/price. Ratio below
    ``vol_ratio_threshold`` (0.8) -> compressed.
  * Donchian(``donchian_period``) width on 15m: (HH-LL)/price, ranked against
    its own trailing ``donchian_history`` widths (mid-rank percentile, so a
    constant-width tape reads 0.5, not 0). Percentile below
    ``narrow_percentile`` (0.4) -> narrow.
  * short EMA slope on 1h closes: EMA(``ema_period``) slope over the last
    ``ema_slope_bars`` points, per bar, normalized by price. |slope| below
    ``flat_slope`` (~0.1%/bar) -> flat.

Combination (documented, configurable — the B.3 example rules in order):
    compressed AND flat -> chop
    slope > +flat_slope -> trend_up
    slope < -flat_slope -> trend_down
    otherwise           -> chop

Because rule 4 defaults to chop, the OUTPUT is the 1h slope classification;
the 15m vol/Donchian features are computed, configurable and exposed via
``classify_features()`` (rule 1 states the chop evidence explicitly, and M4's
calibration consumes the raw features). Fail-open: any missing/bad/insufficient
input classifies as "chop" — chop forbids all entries, which is the safe side.
"""
from __future__ import annotations

import math
import statistics
from typing import Optional

from jev_config import RegimeConfig

REGIMES = ("chop", "trend_up", "trend_down")


def _rows_ok(rows) -> bool:
    return isinstance(rows, (list, tuple)) and len(rows) > 0


def _closes(rows) -> list:
    if not _rows_ok(rows):
        raise ValueError("no OHLCV rows")
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            raise ValueError(f"bad OHLCV row: {row!r}")
        c = float(row[4])
        if not math.isfinite(c) or c <= 0:
            raise ValueError(f"bad close price: {row[4]!r}")
        out.append(c)
    return out


def _highs_lows(rows) -> tuple:
    if not _rows_ok(rows):
        raise ValueError("no OHLCV rows")
    highs, lows = [], []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            raise ValueError(f"bad OHLCV row: {row!r}")
        h, l = float(row[2]), float(row[3])
        if not (math.isfinite(h) and math.isfinite(l)) or h < l or l <= 0:
            raise ValueError(f"bad high/low: {row[2]!r}/{row[3]!r}")
        highs.append(h)
        lows.append(l)
    return highs, lows


def realized_vol_ratio(ohlcv_15m, cfg: Optional[RegimeConfig] = None) -> float:
    """stdev(last ``vol_window`` log returns) / (ATR(``atr_period``)/price).

    Raises ValueError on insufficient/bad data (classify fail-opens to chop).
    """
    cfg = cfg or RegimeConfig()
    closes = _closes(ohlcv_15m)
    highs, lows = _highs_lows(ohlcv_15m)
    n = int(cfg.vol_window)
    p = int(cfg.atr_period)
    if n < 2 or p < 1:
        raise ValueError(f"bad regime config: vol_window={n} atr_period={p}")
    if len(closes) < n + 1 or len(closes) < p + 1:
        raise ValueError(f"need {max(n + 1, p + 1)} bars, got {len(closes)}")
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - n, len(closes))]
    realized = statistics.stdev(rets)
    trs = []
    for i in range(len(closes) - p, len(closes)):
        prev = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    atr = sum(trs) / len(trs)
    price = closes[-1]
    expectation = atr / price
    if expectation <= 0:
        return 0.0 if realized <= 0 else float("inf")
    return realized / expectation


def donchian_width_percentile(ohlcv_15m, cfg: Optional[RegimeConfig] = None) -> float:
    """Mid-rank percentile of the current Donchian width in its own history.

    Width at bar t = (HH - LL)/close over the last ``donchian_period`` bars;
    the percentile ranks it among the previous ``donchian_history`` widths
    (ties count half -> a constant-width tape is 0.5, never "narrow").
    """
    cfg = cfg or RegimeConfig()
    closes = _closes(ohlcv_15m)
    highs, lows = _highs_lows(ohlcv_15m)
    per = int(cfg.donchian_period)
    hist = int(cfg.donchian_history)
    if per < 2 or hist < 1:
        raise ValueError(f"bad regime config: donchian_period={per} history={hist}")
    if len(closes) < per:
        raise ValueError(f"need {per} bars, got {len(closes)}")
    widths = []
    for t in range(per - 1, len(closes)):
        hh = max(highs[t - per + 1:t + 1])
        ll = min(lows[t - per + 1:t + 1])
        widths.append((hh - ll) / closes[t])
    current = widths[-1]
    history = widths[-(hist + 1):-1]
    if not history:
        return 0.5  # neutral: no history to rank against
    below = sum(1 for w in history if w < current)
    equal = sum(1 for w in history if w == current)
    return (below + 0.5 * equal) / len(history)


def ema_slope(ohlcv_1h, cfg: Optional[RegimeConfig] = None) -> float:
    """EMA(``ema_period``) slope over the last ``ema_slope_bars``, per bar / price.

    EMA seeds at the first close; slope = (ema[-1] - ema[-1-b]) / b / close[-1].
    """
    cfg = cfg or RegimeConfig()
    closes = _closes(ohlcv_1h)
    per = int(cfg.ema_period)
    b = int(cfg.ema_slope_bars)
    if per < 1 or b < 1:
        raise ValueError(f"bad regime config: ema_period={per} slope_bars={b}")
    if len(closes) < b + 2:
        raise ValueError(f"need {b + 2} bars, got {len(closes)}")
    k = 2.0 / (per + 1.0)
    ema = closes[0]
    series = [ema]
    for c in closes[1:]:
        ema = c * k + ema * (1.0 - k)
        series.append(ema)
    return (series[-1] - series[-1 - b]) / b / closes[-1]


def classify_features(ohlcv_15m, ohlcv_1h, cfg: Optional[RegimeConfig] = None) -> dict:
    """Regime plus every raw feature (M4 calibration consumes these).

    Fail-open: any bad/insufficient input yields the neutral chop feature set
    (vol_ratio/width_percentile/slope = None) and regime "chop".
    """
    cfg = cfg or RegimeConfig()
    try:
        vol_ratio = realized_vol_ratio(ohlcv_15m, cfg)
        width_pct = donchian_width_percentile(ohlcv_15m, cfg)
        slope = ema_slope(ohlcv_1h, cfg)
    except Exception:
        return {"regime": "chop", "vol_ratio": None, "width_percentile": None,
                "slope": None, "compressed": False, "narrow": False, "flat": True}
    compressed = vol_ratio < cfg.vol_ratio_threshold
    narrow = width_pct < cfg.narrow_percentile
    flat = abs(slope) < cfg.flat_slope
    # B.3 combination (documented above): compressed AND flat -> chop;
    # slope > +eps -> trend_up; slope < -eps -> trend_down; otherwise -> chop.
    if flat and compressed:
        regime = "chop"
    elif slope > cfg.flat_slope:
        regime = "trend_up"
    elif slope < -cfg.flat_slope:
        regime = "trend_down"
    else:
        regime = "chop"
    return {"regime": regime, "vol_ratio": vol_ratio, "width_percentile": width_pct,
            "slope": slope, "compressed": compressed, "narrow": narrow, "flat": flat}


def classify(ohlcv_15m, ohlcv_1h, cfg: Optional[RegimeConfig] = None) -> str:
    """"chop" | "trend_up" | "trend_down" — never raises (fail-open -> chop)."""
    try:
        return classify_features(ohlcv_15m, ohlcv_1h, cfg)["regime"]
    except Exception:
        return "chop"

