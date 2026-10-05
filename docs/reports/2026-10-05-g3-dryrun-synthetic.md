# jev_base backtest grid g3 — BTCUSDT,ETHUSDT,SOLUSDT 4h, 2024-01-01 .. 2026-09-17 (+07, end exclusive)

**SYNTHETIC DATA — seeded random walk with Markov drift, NOT market data. Harness dry run only: these numbers say nothing about any strategy.**

SIMULATED unit-notional returns on synthetic klines — not real funds, not compounded. Fit window only: nothing at or after 2026-09-17 00:00 +07 is used.
Costs (spot): fee 0.100% + slippage 0.050% per side. Halves split by entry-signal time at 2025-05-10 00:00 +07. Random control: 5 seeds, same exits, rate- and side-matched entries.
Gate (pre-declared): n >= 100, net PF > 1 overall and in both halves, sum% > 0 in a majority of symbols, beats random >= 99.8% of seeds (>= 5/5 seeds).
Nulls (5 seeds each; routed exits follow the same labels in every arm): uncond = random entries on any flat bar; matched = random entries only on bars the config's router labels with-trend (CH-1); shift = the strategy's own entries with its exit labels circularly shifted by >= 42 bars (router timing). Control rates are exact: trades per eligible flat decision bar. "beats random" is the matched null for routed configs, uncond otherwise.
Routed configs must also beat the baseline c1-base's sum% (base).
Funding: none (spot).

| config | tf | n | L/S | PF | win | sum% | maxDD% | avgR | SQN | PF A | PF B | PF fund n/abp | rand sum% p50 | beats rand | gate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c1-base | 4h | 133 | 133/0 | 10.39 | 57% | +2616.0 | 21.2 | +3.375 | 6.06 | 11.77 | 9.23 | n/a | +1649.0 | 5/5 | PASS |
| c2-r1-gate | 4h | 133 | 133/0 | 10.39 | 57% | +2616.0 | 21.2 | +3.375 | 6.06 | 11.78 | 9.23 | n/a | +2510.0 | 5/5 | PASS |
| c3-r1-trail | 4h | 223 | 223/0 | 7.41 | 57% | +2127.9 | 32.1 | +1.655 | 7.40 | 7.79 | 7.10 | n/a | +2029.9 | 5/5 | base |
| c4-clf-trail | 4h | 218 | 218/0 | 7.60 | 56% | +2159.6 | 32.0 | +1.720 | 7.44 | 8.30 | 7.04 | n/a | +2189.9 | 1/5 | rand, base |

Per symbol (n / PF / sum%):

| config | BTCUSDT | ETHUSDT | SOLUSDT |
|---|---|---|---|
| c1-base | 49 / 5.05 / +588.0 | 45 / 15.77 / +1019.6 | 39 / 16.63 / +1008.4 |
| c2-r1-gate | 49 / 5.05 / +588.0 | 45 / 15.77 / +1019.6 | 39 / 16.64 / +1008.4 |
| c3-r1-trail | 74 / 4.91 / +516.0 | 73 / 11.15 / +930.3 | 76 / 7.30 / +681.6 |
| c4-clf-trail | 74 / 4.86 / +509.0 | 71 / 11.38 / +950.3 | 73 / 7.76 / +700.3 |

Nulls and routing (per-arm n and hold-time percentiles in bars, CH-2; on-share = share of window / held bars labelled with-trend):

| config | router | on-share bars / held | arm | n p5/p50/p95 | hold p50/p90 | sum% p50 | beats | gates |
|---|---|---|---|---|---|---|---|---|
| c1-base | none | n/a | strategy | 133 | 59/171 | +2616.0 | | |
| c1-base | none | n/a | uncond | 154/163/171 | 28/147 | +1649.0 | 5/5 | yes |
| c2-r1-gate | r1 | 47% / 67% | strategy | 133 | 59/171 | +2616.0 | | |
| c2-r1-gate | r1 | 47% / 67% | uncond | 154/163/171 | 28/147 | +1649.0 | 5/5 |  |
| c2-r1-gate | r1 | 47% / 67% | matched | 144/145/148 | 46/165 | +2510.0 | 5/5 | yes |
| c3-r1-trail | r1 | 47% / 78% | strategy | 223 | 27/88 | +2127.9 | | |
| c3-r1-trail | r1 | 47% / 78% | uncond | 244/253/259 | 16/66 | +1114.4 | 5/5 |  |
| c3-r1-trail | r1 | 47% / 78% | matched | 235/241/252 | 22/79 | +2029.9 | 5/5 | yes |
| c3-r1-trail | r1 | 47% / 78% | shift | 202/214/217 | 31/91 | +2168.4 | 1/5 |  |
| c4-clf-trail | clf | 47% / 85% | strategy | 218 | 28/88 | +2159.6 | | |
| c4-clf-trail | clf | 47% / 85% | uncond | 240/251/255 | 17/67 | +1125.6 | 5/5 |  |
| c4-clf-trail | clf | 47% / 85% | matched | 216/228/231 | 26/85 | +2189.9 | 1/5 | yes |
| c4-clf-trail | clf | 47% / 85% | shift | 201/202/213 | 31/96 | +2262.3 | 1/5 |  |

Claims:

- Router timing, c3-r1-trail: beats its shift null in 1/5 seeds (need >= 5/5) -> router claim NOT supported (needs the gate AND the shift null).
- Router timing, c4-clf-trail: beats its shift null in 1/5 seeds (need >= 5/5) -> router claim NOT supported (needs the gate AND the shift null).
- AI increment, c4-clf-trail vs c3-r1-trail: sum% +2159.6 vs +2127.9 (+31.7); c4-clf-trail gate rand, base -> AI claim REJECTED (the AI added nothing) (needs the gate AND sum% above its r1 twin).

Classifier folds (4h; walk-forward, purged, threshold exposure-matched to r1 on the training rows; weights not printed):

| fold start | n train | last label end | r1 on-share (train) | tau | test bars | test on-share |
|---|---|---|---|---|---|---|
| 2024-01-01 | 13119 | 2023-12-31 23:00 | 46.9% | 0.5728 | 1638 | 43.9% |
| 2024-04-01 | 14757 | 2024-03-31 23:00 | 46.5% | 0.5710 | 1638 | 55.3% |
| 2024-07-01 | 16395 | 2024-06-30 23:00 | 47.1% | 0.5757 | 1656 | 32.2% |
| 2024-10-01 | 18051 | 2024-09-30 23:00 | 46.1% | 0.5715 | 1656 | 45.5% |
| 2025-01-01 | 19707 | 2024-12-31 23:00 | 46.1% | 0.5703 | 1620 | 52.5% |
| 2025-04-01 | 21327 | 2025-03-31 23:00 | 46.5% | 0.5723 | 1638 | 49.6% |
| 2025-07-01 | 22965 | 2025-06-30 23:00 | 46.7% | 0.5731 | 1656 | 45.1% |
| 2025-10-01 | 24621 | 2025-09-30 23:00 | 46.6% | 0.5727 | 1656 | 44.2% |
| 2026-01-01 | 26277 | 2025-12-31 23:00 | 46.4% | 0.5708 | 1620 | 51.6% |
| 2026-04-01 | 27897 | 2026-03-31 23:00 | 46.8% | 0.5721 | 1638 | 49.2% |
| 2026-07-01 | 29535 | 2026-06-30 23:00 | 47.0% | 0.5729 | 1401 | 43.6% |
