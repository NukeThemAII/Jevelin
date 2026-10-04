# jev_base grid 2 — pre-registration, 2026-10-04 +07

Declared and committed BEFORE any grid-2 run on the fit window. Code is the source of truth:
`GRIDS["g2"]` / `GRID2` / `gate_failures` / `funded_pcts` in `scripts/jev_base_backtest.py`;
the grid is frozen by `Grids.test_grid2_is_frozen_as_pre_registered`. Ruling (Ada, 2026-10-04):
one more grid, ≤6 configs, Oracle checks the design before it runs; if it fails, the base
strategy does not go to forward paper and there is no grid 3 without a scope change.

**Amendment 1 (2026-10-04, still before any run)** — Oracle's design check: GO-WITH-CHANGES.
Applied as given: `dc55-4h-ema-w` dropped (EMA is a third lever — tested once, on dc20);
`dc20-1h-w` added (isolates the wider-exit lever from the timeframe lever); the raw
beats-random seed count is reported; a perps funding stress is reported (not gated).

## Why this grid

Grid 1 (`2026-10-04-base-backtest-fit.md`): best gross capture +0.089%/trade vs a ~0.20%
perps round trip. Hypothesis: 4h bars (≈¼ the signals) and wider exits (bigger move per trade)
make the same cost a smaller share of 1R. This grid tests that, nothing else.

Dropped, with reasons: tsmom (lost BEFORE costs, gross PF 0.89–0.91 — lower costs can't
rescue it); the `regime` filter (≈ no filter in grid 1, and it is a 15m/1h label).

## Configs — Donchian entry, ATR(14), perps long+short

| config | tf | entry | trend | stop ATR | trail ATR | exit | role |
|---|---|---|---|---|---|---|---|
| dc20-4h | 4h | 20-bar channel | none | 2 | 3 | — | timeframe lever alone (grid 1 exits) |
| dc20-1h-w | 1h | 20 | none | 3 | 5 | — | exit lever alone (grid 1 bars) |
| dc20-4h-w | 4h | 20 | none | 3 | 5 | — | both levers |
| dc20-4h-ema-w | 4h | 20 | EMA50/200 | 3 | 5 | — | + trend filter |
| dc55-4h-w | 4h | 55 | none | 3 | 5 | — | slower entry |
| dc55-x20-4h | 4h | 55 | none | 2 | off | opposite 20-bar channel | Turtle System 2 exits |

With grid 1's `dc20` (1h, 2/3 ATR) the first three complete a 2×2 of timeframe × exits.

Unchanged from grid 1: BTCUSDT/ETHUSDT/SOLUSDT, 2024-01-01 .. 2026-09-17 +07 (end exclusive,
CLI refuses later), 15m Binance spot history resampled, fee 0.05% + slippage 0.05% per side,
next-open fills, gap-aware resting stops, halves split at 2025-05-10, rate- and side-matched
random-entry control with the same exits. Exit multiples (3/5) picked once, not searched.

## Gate — a config is a forward-paper candidate only if ALL hold

1. n ≥ 100 trades (pooled);
2. net PF > 1 overall AND in both halves;
3. sum% > 0 in a strict majority of symbols (≥ 2 of 3);
4. beats the random control in ≥ 99.6% of 1,000 seeds, i.e. **≥ 996/1000** (strictly higher
   pooled sum%) — Bonferroni 0.05 / 12, since 12 configs (grids 1 + 2) have now been tested on
   this one window. Correlated configs make this conservative; that is the intended direction.

If no config passes: base strategy stays off forward paper; report and stop.

## Funding stress — reported, not gated

The cost model has no perps funding; at 4h / 3–5 ATR holds (days–weeks) it can rival the
round trip. The report adds net PF with longs paying **1 bp and 3 bp per 8h held** (pro rata on
entry notional, a stop's exit bar counted in full), shorts credited nothing. A config that
passes the gate but has PF ≤ 1 at 1 bp is a cost-fragile pass: the forward-paper call goes to
Ada with that flag, not straight to paper.

## Limits of the claim

One pre-registered follow-up on one window, with mild grid-1 conditioning (dropped losers,
EMA kept once). A pass earns forward paper, which is the real out-of-sample test — not a
claim of edge. The random null matches each symbol's entry rate and long share, so 2024–26
drift is priced into both arms; regime-timed exposure is the alpha under test.

## Run (once)

```
.venv/bin/python scripts/jev_base_backtest.py --grid g2 --start 2024-01-01 --end 2026-09-17 \
    --out docs/reports/2026-10-04-base-grid2-fit.md
```

1,000 seeds is the grid default (~150 s for the full grid on synthetic data of the same size).
