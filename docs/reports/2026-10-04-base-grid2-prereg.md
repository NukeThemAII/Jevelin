# jev_base grid 2 (4h) — pre-registration, 2026-10-04 +07

Declared and committed BEFORE any 4h run on the fit window. Code is the source of truth:
`GRIDS["g2"]` / `GRID_4H` / `gate_failures` in `scripts/jev_base_backtest.py`; the grid is
frozen by `Grids.test_grid2_is_frozen_as_pre_registered`. Ruling (Ada, 2026-10-04): one more
grid, ≤6 configs, Oracle checks the design before it runs; if it fails, the base strategy does
not go to forward paper and there is no grid 3 without a scope change.

## Why this grid

Grid 1 (`2026-10-04-base-backtest-fit.md`): best gross capture +0.089%/trade vs a ~0.20%
perps round trip. Hypothesis: 4h bars (≈¼ the signals) and wider exits (bigger move per trade)
make the same cost a smaller share of 1R. This grid tests that, nothing else.

Dropped, with reasons: tsmom (lost BEFORE costs, gross PF 0.89–0.91 — lower costs can't
rescue it); the `regime` filter (≈ no filter in grid 1, and it is a 15m/1h label).

## Configs — 4h signal bars, Donchian entry, ATR(14), perps long+short

| config | entry | trend | stop ATR | trail ATR | exit | role |
|---|---|---|---|---|---|---|
| dc20-4h | 20-bar channel | none | 2 | 3 | — | timeframe-only control (grid 1 exits) |
| dc20-4h-w | 20 | none | 3 | 5 | — | wider exits |
| dc20-4h-ema-w | 20 | EMA50/200 | 3 | 5 | — | + trend filter |
| dc55-4h-w | 55 | none | 3 | 5 | — | slower entry |
| dc55-4h-ema-w | 55 | EMA50/200 | 3 | 5 | — | slower entry + trend |
| dc55-x20-4h | 55 | none | 2 | off | opposite 20-bar channel | Turtle System 2 exits |

Unchanged from grid 1: BTCUSDT/ETHUSDT/SOLUSDT, 2024-01-01 .. 2026-09-17 +07 (end exclusive,
CLI refuses later), 15m Binance spot history resampled, fee 0.05% + slippage 0.05% per side,
next-open fills, gap-aware resting stops, halves split at 2025-05-10, rate- and side-matched
random-entry control with the same exits. Exit multiples (3/5) picked once, not searched.

## Gate — a config is a forward-paper candidate only if ALL hold

1. n ≥ 100 trades (pooled);
2. net PF > 1 overall AND in both halves;
3. sum% > 0 in a strict majority of symbols (≥ 2 of 3);
4. beats the random control in ≥ 99.6% of 1,000 seeds — Bonferroni 0.05 / 12, since 12
   configs (grids 1 + 2) have now been tested on this one window.

If no config passes: base strategy stays off forward paper; report and stop.

## Run (once, after Oracle's check)

```
.venv/bin/python scripts/jev_base_backtest.py --grid g2 --start 2024-01-01 --end 2026-09-17 \
    --out docs/reports/2026-10-04-base-grid2-fit.md
```

1,000 seeds is the grid default (~77 s for the full grid on synthetic data of the same size).
