# Original Jevelin — pre-registration DRAFT, 2026-10-04 +07

**Status: DRAFT. Not approved, not built, not run.** Two decisions are open:

- Ada: option A (this replay), B (forward paper only) or C (stop).
- Operator: an OpenRouter spend of about $33. The budget guard aborts the run at $40.

Oracle: please check §Control and §Gate for degeneracy. You don't need to approve the
strategy itself. Nothing below has touched the fit window.

## Scope change

This is a new strategy family, not config #13 of Donchian/ATR. Entries come from an LLM
verdict: Jev's five-question answer on a 60-second order-flow snapshot, filtered by a 1h EMA
regime gate. Exits come from Jev's dump score. There is no channel, ATR stop or trail. The
window, symbols and cost model are the same as in grids 1 and 2, so Bonferroni counts every
config ever tested on this window: 12 + 4 here = **16**.

## Strategies under test (excavated, pinned)

All four use one verdict replay: one question set (`8c9596ca6d`) and one model
(`typesafe/jev-1.13-20260917`) were used for every live call from 09-26 to 10-04.

| config | code pin | entry | exit | live record |
|---|---|---|---|---|
| `sup-spot` | `faa517d` `jev_gates.decide` + `config/v2.yaml` | pump≥65, phase∈{breakout,accumulation}, whipsaw≤0.45 (fan-out in 0.4–0.6), exhaustion≤0.55, confidence≥0.65, regime=`trend_up` | dump≥65 for 2 cycles OR ≥75 once, after a 3-cycle minimum hold; **no stop** | 09-27→09-30, 0 trades |
| `sup-perps` | `faa517d` `jev_perps.decide_perps` | same as spot. A short needs phase∈{breakout,accumulation} and dump≥65 together, which happened in 0 of 3,694 live verdicts (432 had dump≥65). Fixed later in `6b23699`; tested as it ran, not backported | same + 2% stop | 0 trades |
| `v1-spot` | `39a175a` `jev_gates` | pump≥60, whipsaw≤0.5, exhaustion≤0.6, confidence≥0.6; **no regime gate** | dump≥60 OR exhaustion≥0.8, no minimum hold | BTC 09-25→09-27, 10 round trips |
| `v1-perps` | `39a175a` `jev_perps` | same as v1 spot, plus shorts at dump≥60 | long: as spot; short: pump≥60 OR exhaustion≥0.8; + 2% stop | 10 round trips |

The rule code runs as it existed at those commits. It is extracted into a replay module, and
a parity test pins each config against its commit's decisions.

## Replay design: declared deviations from live

1. **Cadence.** A fixed 5-minute cycle with no burst trigger and no decision cache, so every
   cycle gets a fresh verdict. Live bursts fired most cycles (median gap between scores 5.5 s)
   and depended on wall-clock thread timing, so they can't be reproduced offline. Both arms
   get the same cadence.
2. **Inputs.** `jev_state.build_state` is rebuilt from Binance spot aggTrades (the 60 s before
   each cycle) and 1m closes, both free and public. `symbol` → `"ASSET"` and `asof_ms` → `0`
   before the call. Every other feature is relative (percentages, shares, counts).
3. **Fan-out.** The second whipsaw sample is drawn only when it can change an entry, meaning
   every other entry gate passes. The decisions are the same as live's always-draw rule.
4. **Fills and costs.** Live model: price at the cycle plus 5 bp adverse slippage per side.
   Spot fee 0.10%/side, perps taker 0.05%/side. The perps 2% stop is checked against 1m
   high/low between cycles (the live fast loop ran every 5 s). Leverage doesn't change trade
   %, and the 2% stop triggers long before liquidation.
5. **Risk layer.** The M5 caps, daily kill and drawdown halt stay as they ran for the `sup-*`
   configs. They block entries but never exits.
6. **Funding.** As in grid 2: reported, not gated. Longs pay 1 and 3 bp per 8 h held.

## Control

Each symbol gets seeded random entries while flat, matched to the strategy's entry rate and
long share (`random_control`, 1,000 seeds). The exits are the strategy's own Jev
dump-hysteresis exits, evaluated on the same replayed verdicts. The perps configs also keep
the 2% stop. Minimum hold and cooldown are unchanged.

**Consequence: the replay must cover every cycle.** Spot has no stop, so exit timing comes
entirely from Jev, and 1,000 random paths hold positions on effectively every cycle. A
cheaper replay that only scores `trend_up` cycles would leave the control with no exits,
which makes it degenerate. Full coverage is therefore the minimum cost, not padding.

Reported, not gated: a **regime-matched** control for `sup-*`, where random entries happen only
on `trend_up` cycles. It separates Jev's entry selection from the deterministic regime filter.

## Gate (unchanged code: `gate_failures`)

Each config is gated on its own. A config is a forward-paper candidate only if all of these
hold:

1. n ≥ 100 trades, pooled;
2. net PF > 1 overall and in both halves (split at 2025-05-10);
3. sum% > 0 in at least 2 of 3 symbols;
4. it beats the random control in **≥ 997/1000** seeds (Bonferroni 0.05/16 = 99.69%).

## Contamination: this test can only falsify

The model was built on 2026-09-17, after the window, and we don't know its training data.
Stripping `symbol` and `asof_ms` closes the input leak but not a training leak. So:

- **FAIL** is a kill. The strategy couldn't win even with possible leakage, and original
  Jevelin is closed.
- **PASS** earns forward paper only, and makes no claim of edge. Promotion is judged only on
  forward data.

## Feasibility pre-screen (free, no Jev calls, done 2026-10-04)

- Live supervisor log, 3,649 spot cycles: regime was `trend_up` on **5** of them, and Jev's
  own entry gates passed on 25 cycles, every one during chop or a downtrend. The live 0 trades
  came from the regime gate, not from Jev.
- Full window, live regime code (`regime_series`) on 15m bars: `trend_up` on **23.7%** of
  bars (BTC 17.6%, ETH 24.1%, SOL 29.4%), in 2,919 episodes (median 4 h). At the live Jev pass
  rate that gives about 1,400 qualifying cycles, so n ≥ 100 is plausible. This is an estimate,
  not a promise.

## Cost, time, pilot

- Calls: 990 days × 288 × 3 symbols = 855,360. At $3.846e-5 per call (mean of 5,833 logged
  calls) that is **$32.90**, plus under 1% for fan-out, plus retries. The budget guard aborts
  at $40. The verdict cache is written to disk as calls return, so an abort keeps what was
  already paid for.
- Time: unmeasured. It depends on OpenRouter concurrency.
- **Pilot (≤ $0.10), on live-logged days outside the window (09-28, all three symbols).** It
  checks that the model is still served, the parse rate and latency, and replay fidelity:
  rebuilt states and verdicts compared with the live logs for the same cycles. If the
  verdict distributions disagree badly, the run stops and the replay isn't the original.

## Run (once, after Oracle's check and both decisions)

The CLI is defined when it is built. The window is 2024-01-01 .. 2026-09-17 +07, end
exclusive (`FIT_CUTOFF_MS`). Output: `docs/reports/2026-10-04-original-jevelin-fit.md`.
