# Replay tuning report — 2026-10-04 (Forge)

Status: **analysis + proposal. Nothing in this report is applied.** The proposed config below
waits for Ada's review; shipped `config/v2.yaml` behavior is unchanged.

Data:
- `runtime/paper_decisions.jsonl`, the 2026-09-27 18:33 → 09-30 12:38 (+07) paper run:
  - 7388 rows (spot 3694 / perps 3694), one row per book per cycle.
  - BTC 1287 / ETH 1201 / SOL 1201 cycles, plus 5 single-cycle scout pairs.
- The 520 pre-M0 v1 BTC calls in `runtime/jev_decisions.jsonl`, 09-26 00:02 → 09-27 17:41.
- Public Binance 1m spot klines (keyless, cached in `runtime/klines`).

Machine output (all tables, reproducible): [`2026-10-04-replay-data.md`](2026-10-04-replay-data.md),
regenerated with:

```
.venv/bin/python scripts/jev_forward.py --decisions /home/xaos/Jevelin/runtime/paper_decisions.jsonl \
  --v1-calls /home/xaos/Jevelin/runtime/jev_decisions.jsonl --out docs/reports/2026-10-04-replay-data.md
```

`runtime/backtest/` holds only the upstream bitvavo fixture, and nothing there replays Jev decisions.
`jev_calibrate.py` (M4) values vetoes on the store's own recorded cycle prices. The new
`scripts/jev_forward.py` complements it: forward moves on klines (15m/1h/4h/24h, incl. after the run
ended), per-gate attribution, IC, and a rule-faithful sim that drives the REAL `decide` /
`decide_perps`.

## Headline

1. **No edge is shown, and n is too small to show one.** The market fell over the window
   (BTC −1.67%, ETH −0.61%, SOL −3.10%). The regime classifier labelled 2339 cycles `trend_down`,
   1305 `chop` and only **5** `trend_up`. Spot longs were blocked almost everywhere, which was the
   right call ex post. But that is market beta, not proven gate skill.
2. **Jev scores did not predict direction.**
   - Decimated IC (1 row per 15 min per symbol, n=221, SE ≈ 0.067) for pump−dump is −0.091 / −0.010 /
     −0.136 / −0.110 at 15m/1h/4h/24h: weakly *contrarian* and not significant.
   - The v1 BTC window flips sign (+0.235 / +0.318 / +0.071 / +0.170, n=41, SE ≈ 0.16).
   - A signal whose sign flips between adjacent windows is not a usable signal yet.
3. **Perps shorts were structurally unreachable** (fixed, configurable; defaults unchanged —
   `6b23699`, see V2-DESIGN B.3 "Short-side bars").
   - Making them reachable does not make them profitable.
   - The dump signal under-performed a coin-flip short in this window: on dump≥65 rows the short
     moved +0.24% at 4h, against roughly +0.5% for an unconditional short.
4. **Drawdown halt (51c29d5): verified, plus one more hole fixed** (`a0072ed`). The fix holds. A
   *real* halt was laundered by a universe change, both in-process (scout rotation) and across a
   restart, and is now carried. The false latch blocked 39 cycles in this run but was **never the
   sole veto**, so it cost 0 trades.

## Filter → blocked → forward move → recommendation (spot, long-only)

Directional forward move of the blocked long, gross, % (all vetoes / sole vetoes at 4h). A veto
only cost money where the move beats the 0.30% round trip. "sole" = the gate was the only blocker.

| filter | blocked | sole | fwd 1h | fwd 4h | sole 4h | recommendation |
|---|---|---|---|---|---|---|
| low_pump (<65) | 3578 | 0 | −0.15 | −0.52 | n/a | **keep** — loosening to 55 + counter-trend allow lost (sim: 13 trades, PF 0.12) |
| low_confidence (<0.65) | 3365 | 4 | −0.16 | −0.55 | −0.47 | **keep** — 0.55 adds 1 trade (−0.06%), no information |
| high_whipsaw (>0.45) | 2708 | 0 | −0.12 | −0.46 | n/a | **keep** for longs; short side gets its own bar (below) |
| phase_not_in_entry_set | 2526 | 0 | −0.13 | −0.46 | n/a | **keep** for longs; short side gets its own set (below) |
| regime_counter (trend_down) | 2339 | 17 | −0.02 | −0.31 | −0.20 | **keep** — sole 24h +0.42 on n=17 overlapping rows is noise |
| regime_chop | 1305 | 8 | −0.42 | −0.92 | −0.94 | **keep** — strongest saver; chop forward moves worst at every horizon ≥1h |
| high_exhaustion (>0.55) | 356 | 0 | −0.06 | −0.45 | n/a | **keep** |
| capitulation | 287 | 0 | −0.11 | −0.27 | n/a | **keep** (shared block, both sides) |
| whipsaw_fanout_tie | 68 | 0 | −0.24 | −0.85 | n/a | **keep** |
| drawdown_halt | 39 | 0 | +0.12 | +1.12 | n/a | false latch, fixed in 51c29d5 (+ a0072ed); never sole, so 0 trades lost |

The perps gate table is in the data file. Perps `vetoed_by` is one flat list across both sides, so
its per-gate direction is the stronger signal's side and is approximate. The short-side numbers below
come from the dump≥65 slice directly.

## Perps short side

| slice (dump≥65) | rows | decimated | short fwd 1h | short fwd 4h | short fwd 24h |
|---|---|---|---|---|---|
| all | 432 | 100 | +0.10 / +0.12 | +0.26 / +0.24 | −0.10 / +0.04 |
| phase distribution | 185 | 72 | +0.10 / +0.08 | +0.27 / +0.16 | −0.05 / −0.13 |
| phase capitulation | 247 | 75 | +0.09 / +0.12 | +0.24 / +0.22 | −0.14 / −0.10 |
| conf ≥ 0.65 | 83 | 34 | +0.15 / +0.12 | +0.29 / +0.19 | −0.05 / −0.17 |

(all rows / decimated). Perps round trip is 0.20%. The unconditional 4h move over the window was
about −0.5%, so a random short earned roughly +0.5% gross. Conditioning on dump≥65 *lowered* that,
which matches the positive IC of `dump` vs forward return (+0.150 decimated at 4h).

## Rule-faithful sim (unit notional, net of fees + slippage)

The real `decide` / `decide_perps` run on the logged verdict + regime. Cooldown and hysteresis
counters are carried as at runtime. Perps stop/liq are checked intrabar on 1m klines, which is
stricter than the runtime cycle-price check. Funding, the daily kill and M5 risk are not modeled.

| variant | book | n | PF | win | sum % | H1 (n, PF) | H2 (n, PF) |
|---|---|---|---|---|---|---|---|
| baseline (shipped) | spot / perps | 0 / 0 | — | — | 0 | — | — |
| shorts: `[distribution]`, whipsaw off | perps | 5 S | 0.40 | 40% | −0.91 | 2, inf | 4, 0.00 |
| shorts + 60m time stop | perps | 8 S | 0.15 | 25% | −1.61 | 2, inf | 6, 0.00 |
| loose longs: pump 55, counter-trend allow | spot | 13 L | 0.12 | 8% | −5.00 | 4, 0.00 | 9, 0.19 |
| loose longs: pump 55, counter-trend allow | perps | 13 L | 0.16 | 8% | −3.75 | 4, 0.00 | 9, 0.26 |
| conf 0.55 | spot / perps | 1 / 1 | 0.00 / inf | — | −0.06 / +0.04 | 1 / 1 | 0 / 0 |

H1/H2 split at the median timestamp; a position open at the cut is marked open in H1 and does not
carry into H2, so H1 + H2 can exceed ALL. Every n here is far too small for a PF to mean anything.
The only robust read is directional: loosening the long gates lost in both halves.

## Proposed config (for Ada's review — NOT applied)

```diff
 perps:
-  short_entry_phases: [breakout, accumulation]   # mirrors longs: shorts unreachable
+  short_entry_phases: [distribution]             # the phase dump≥65 rows actually carry
-  short_max_whipsaw: 0.45                        # upside-phrased question
+  short_max_whipsaw: 1.0                         # bar off for shorts (min on dump≥65 rows: 0.56)
```

Everything else stays as shipped: all spot long gates, chop block, counter-trend block, conf 0.65,
pump/dump 65, exits 65×2 / 75, min hold 3, no time stop.

Rationale:
- This is a **paper-only, data-collection change**, not a profit claim. With shorts unreachable the
  perps book can never learn anything about the short side, and the in-sample evidence (5 trades,
  PF 0.40, signal below drift) is too thin to decide either way.
- Proposed promotion bar before shorts are considered tuned:
  - ≥30 closed shorts;
  - PF ≥ 1.2 on both time halves;
  - decimated `dump` IC < 0 at 1h/4h.
- `capitulation` stays a shared block (shorting a selling climax is shorting the low).

## Next steps (proposals)

1. **Shadow multi-config books on one verdict stream** (Lin: daemon wiring).
   - One Jev call per cycle feeds N paper books (baseline / shorts / loose).
   - Configs get compared on live forward data without N× Jev cost.
   - `jev_forward.simulate` is the offline twin.
2. **Re-run `jev_forward` weekly** as data accumulates; the decimated IC needs n≈400+ for SE≈0.05.
3. **Dedicated short-side Jev question** ("is this breakdown likely a fakeout?") instead of turning
   the upside-phrased whipsaw bar off.
4. **Side-specific confidence.** Confidence is min(pump, dump, phase) confidence. The long-side
   pump/phase answers drag down short-side conviction.
5. **Jev as a filter on a base strategy** (Oracle research). The stand-alone direction signal shows
   no IC. The open question is whether it improves a simple trend/breakout base when used as a veto.
