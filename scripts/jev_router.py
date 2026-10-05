#!/usr/bin/env python3
"""jev_router — walk-forward logistic regime router for grid g3 (config c4). Stdlib only.

One question per signal bar: keep the trend settings (``ON`` = "trend_up")
or not (``OFF`` = "chop"). The router never creates, sizes or exits a trade;
c4 reads its label exactly like a jev_regime label, on the per-regime trail
``trail_atr_off`` only. c4's entries stay c3's r1 gate (D6, Oracle 2026-10-05:
the AI routes exits, never picks entries).

Features (``FEATURES``), causal (bars closed by the signal close) and
scale-free (CH-3: no absolute-USD level reaches the model):
  slope_4h, vol_ratio_1h, width_pct_1h — jev_regime.classify_features on the
      R1 windows (60 x 1h + 120 x 4h);
  slope_1h — the same on the live windows (60 x 15m + 120 x 1h);
  mom6, mom42 — log return over 6 / 42 bars divided by ATR / close;
  chan20 — the close's position in the last 20 bars' high-low range.
Label: 1 if close[i + LABEL_BARS] > close[i] (the next 24 h on 4h bars).

Model: L2 logistic regression on standardized features (intercept free),
Newton-IRLS, pooled over symbols. Walk-forward: one refit per calendar
quarter (+07) from the window start. A fold trains on every row from
``train_start`` whose label window closes by the fold start (purged) and
labels that quarter only. Threshold: exposure-matched — tau switches the same
share of TRAINING rows on as the R1 router has on them, so c4 vs c3 compares
when the trail is wide at equal exposure, not more vs less time on it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

FEATURES = ("slope_4h", "slope_1h", "vol_ratio_1h", "width_pct_1h",
            "mom6", "mom42", "chan20")
LABEL_BARS = 6          # 24 h on 4h bars
L2 = 1.0                # on standardized slopes; ~nothing at n ~ 10^4
ON, OFF = "trend_up", "chop"
BKK = timezone(timedelta(hours=7))


def features(bars, atr, r1_feats, live_feats) -> list:
    """Per bar: a tuple in ``FEATURES`` order, or None (warmup / missing)."""
    out = []
    for i, b in enumerate(bars):
        r1, lv, a = r1_feats[i], live_feats[i], atr[i]
        if i < 42 or r1 is None or lv is None or a is None or a <= 0:
            out.append(None)
            continue
        vals = (r1.get("slope"), lv.get("slope"), r1.get("vol_ratio"),
                r1.get("width_percentile"))
        if any(v is None or not math.isfinite(v) for v in vals):
            out.append(None)
            continue
        c = b[4]
        atr_pct = a / c
        hh = max(x[2] for x in bars[i - 19:i + 1])
        ll = min(x[3] for x in bars[i - 19:i + 1])
        out.append(vals + (math.log(c / bars[i - 6][4]) / atr_pct,
                           math.log(c / bars[i - 42][4]) / atr_pct,
                           (c - ll) / (hh - ll) if hh > ll else 0.5))
    return out


def labels(bars, h=LABEL_BARS) -> list:
    """1 if the close h bars ahead is higher, else 0; None for the last h."""
    n = len(bars)
    return [(1 if bars[i + h][4] > bars[i][4] else 0) if i + h < n else None
            for i in range(n)]


# ---------------------------------------------------------------------------
# L2 logistic regression, Newton-IRLS
# ---------------------------------------------------------------------------

def standardize(X) -> tuple:
    k, n = len(X[0]), len(X)
    mu = [sum(x[j] for x in X) / n for j in range(k)]
    sd = []
    for j in range(k):
        s = math.sqrt(sum((x[j] - mu[j]) ** 2 for x in X) / n)
        sd.append(s if s > 0 else 1.0)
    return mu, sd


def _sigmoid(z) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _solve(A, b) -> list:
    """Gaussian elimination with partial pivoting (A is small and SPD)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for j in range(c, n + 1):
                    M[r][j] -= f * M[c][j]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][j] * x[j] for j in range(r + 1, n))) / M[r][r]
    return x


def fit_logistic(X, y, l2=L2, iters=100, tol=1e-10) -> list:
    """Weights [intercept, w1..wk] minimizing log-loss + l2/2 |w|^2 (the
    intercept is not penalized). X is used as given (standardize first)."""
    k = len(X[0]) + 1
    rows = [(1.0,) + tuple(x) for x in X]
    w = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k
        Hm = [[0.0] * k for _ in range(k)]
        for z, t in zip(rows, y):
            p = _sigmoid(sum(a * b for a, b in zip(w, z)))
            r, s = p - t, p * (1.0 - p)
            for a in range(k):
                za = z[a]
                g[a] += r * za
                sza = s * za
                Ha = Hm[a]
                for c in range(a + 1):
                    Ha[c] += sza * z[c]
        for a in range(k):
            for c in range(a):
                Hm[c][a] = Hm[a][c]
        for a in range(1, k):
            g[a] += l2 * w[a]
            Hm[a][a] += l2
        for a in range(k):
            Hm[a][a] += 1e-12           # keeps an all-zero-curvature case solvable
        step = _solve(Hm, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(v) for v in step) < tol:
            break
    return w


def _prob(w, mu, sd, x) -> float:
    return _sigmoid(w[0] + sum(wj * (xj - m) / s
                               for wj, xj, m, s in zip(w[1:], x, mu, sd)))


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------

def quarter_starts(start_ms, end_ms) -> list:
    """Fold starts: ``start_ms``, then every calendar-quarter start (+07)
    before ``end_ms``."""
    out = [int(start_ms)]
    d = datetime.fromtimestamp(start_ms / 1000.0, BKK)
    y, m = d.year, (d.month - 1) // 3 * 3 + 1
    while True:
        y, m = (y + 1, m - 9) if m > 9 else (y, m + 3)
        t = int(datetime(y, m, 1, tzinfo=BKK).timestamp() * 1000)
        if t >= end_ms:
            return out
        out.append(t)


def walk_forward(data, start_ms, end_ms, train_start_ms, period_ms,
                 h=LABEL_BARS, l2=L2) -> tuple:
    """data: {sym: {"bars", "X", "y", "r1"}} (lists aligned to bars).
    Returns ({sym: label per bar, None outside [start, end)}, fold stats)."""
    routes = {sym: [None] * len(d["bars"]) for sym, d in data.items()}
    starts = quarter_starts(start_ms, end_ms)
    folds = []
    for f, t0 in enumerate(starts):
        t1 = starts[f + 1] if f + 1 < len(starts) else int(end_ms)
        X, y, r1_on, last_end = [], [], 0, None
        for d in data.values():
            bars = d["bars"]
            for i in range(len(bars) - h):
                end = bars[i + h][0] + period_ms
                if (bars[i][0] < train_start_ms or end > t0 or d["X"][i] is None
                        or d["y"][i] is None or d["r1"][i] is None):
                    continue
                X.append(d["X"][i])
                y.append(d["y"][i])
                r1_on += d["r1"][i] == ON
                last_end = end if last_end is None else max(last_end, end)
        if len(X) < 2 * len(FEATURES) or len(set(y)) < 2:
            raise ValueError(f"fold {f}: too few training rows ({len(X)})")
        mu, sd = standardize(X)
        w = fit_logistic([[(v - m) / s for v, m, s in zip(x, mu, sd)] for x in X], y, l2)
        probs = sorted((_prob(w, mu, sd, x) for x in X), reverse=True)
        share = r1_on / len(X)
        k = round(share * len(X))
        tau = math.inf if k == 0 else probs[k - 1]
        n_pred = on = 0
        for sym, d in data.items():
            for i, b in enumerate(d["bars"]):
                if t0 <= b[0] < t1 and d["X"][i] is not None:
                    p = _prob(w, mu, sd, d["X"][i])
                    routes[sym][i] = ON if p >= tau else OFF
                    n_pred += 1
                    on += p >= tau
        folds.append({"start": t0, "n_train": len(X), "last_label_end": last_end,
                      "r1_on_share": share,
                      "train_on_share": sum(p >= tau for p in probs) / len(X),
                      "tau": tau, "w": w, "n_pred": n_pred,
                      "on_share": on / n_pred if n_pred else None})
    return routes, folds
