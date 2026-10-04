# Original Jevelin — pre-registration DRAFT, 2026-10-04 +07

**Status: DRAFT, amended. Not built, not run.**

- Ada ruled option A (this replay), 4 configs (v1 and supervisor).
- Oracle's design check came back GO-WITH-CHANGES. Amendments CH-1 to CH-4 are applied below
  and marked with their tag.
- Open: the operator's OK on about $33 of OpenRouter spend. The budget guard aborts the run
  at $40.

Nothing below has touched the fit window.

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

`confidence` is the minimum of the pump, dump and phase confidences, and pump/dump are scaled
from 0–3 to 0–100, at both pins. The rule code runs as it existed at those commits. It is
extracted into a replay module, and a parity test pins each config against its commit's
decisions.

## Replay design: declared deviations from live

1. **Cadence.** A fixed 5-minute cycle with no burst trigger and no decision cache, so every
   cycle gets a fresh verdict. Live bursts fired most cycles (median gap between scores 5.5 s)
   and depended on wall-clock thread timing, so they can't be reproduced offline. Both arms
   get the same cadence.
2. **Inputs (CH-3 wording).** `jev_state.build_state` is rebuilt from free, public Binance spot
   data: the last 200 trades and the last 10 1m closes before each cycle, which is what live
   fetched (`fetch_trades(limit=200)`, `fetch_ohlcv("1m", limit=10)`). `symbol` → `"ASSET"` and
   `asof_ms` → `0` before the call. This closes the **direct identifier leak only**. Two kept
   features still carry scale:
   - `last_10_trades_side_usd` lists absolute USD trade sizes (`jev_state.py:124–125`);
   - `oversized_{buy,sell}_count_60s` are raw counts against 3× the median trade size
     (`jev_state.py:95–107`), so they grow with trading activity.

   Both fingerprint the symbol (BTC vs SOL trade sizes) and the era (volume regime). They are
   kept on purpose: dropping them would move the replay further from the original. Leakage is
   expected to bias toward PASS, so a FAIL stays a kill. The pilot (§Pilot) checks that
   stripping doesn't change the decisions.
3. **Fan-out.** The second whipsaw sample is drawn only when it can change an entry, meaning
   every other entry gate passes. The decisions are the same as live's always-draw rule.
4. **Fills and costs.** Live model: price at the cycle plus 5 bp adverse slippage per side.
   Spot fee 0.10%/side, perps taker 0.05%/side. The perps 2% stop is checked against 1m
   high/low between cycles (the live fast loop ran every 5 s). Leverage doesn't change trade
   %, and the 2% stop triggers long before liquidation.
5. **Risk layer.** The M5 caps, daily kill and drawdown halt stay as they ran for the `sup-*`
   configs. They block entries but never exits.
6. **Funding.** As in grid 2: reported, not gated. Longs pay 1 and 3 bp per 8 h held.

## Verdict cache (CH-4: shared-cache assumption)

Each (symbol, cycle) gets one Jev verdict, drawn once and written to disk. All 4 configs,
both arms and all 1,000 seeds reuse it. A fan-out second sample is cached and shared the same
way.

- **Why this is valid.** `build_state` takes only `symbol`, 1m closes, trades and `now_ms`
  (`jev_state.py:37–42`). No position, book or config state reaches Jev, so a verdict doesn't
  depend on what any arm holds.
- **What it buys.** The strategy and its control see the same Jev draw on every cycle, so
  the comparison is paired and sampling noise can't favour either arm.
- **What it assumes.** Jev is stochastic. In the live log, 1,801 of 1,801 repeat calls on
  byte-identical inputs (the fan-out pairs, 09-27→09-30) returned different answers. One cached
  draw stands in for the live verdict distribution, and a different draw would change some
  individual decisions. The pilot's repeat arm measures how many.

## Control

Each config gets seeded random entries while flat, using the same two-draws-per-cycle
schedule as `random_control` (1,000 seeds; the seed fixes the schedule, not the path). The
exits are the strategy's own Jev exits, evaluated on the same cached verdicts. The perps
configs also keep the 2% stop. Minimum hold and cooldown are unchanged.

**Gated control (CH-1):**

| config | gated control | entry rate matched on | long share |
|---|---|---|---|
| `sup-spot`, `sup-perps` | **regime-matched**: random entries only on cycles where the strategy's own causal regime series reads `trend_up` | strategy entries ÷ cycles that are `trend_up` and flat, per symbol | per symbol (sup shorts are unreachable, so 1.0) |
| `v1-spot`, `v1-perps` | **unconditional**: every v1 entry gate is a Jev answer, so there's no deterministic filter to match | strategy entries ÷ flat cycles, per symbol | per symbol |

Off-regime draws are discarded, not redrawn, so the schedule stays path-independent. The
regime-matched control keeps `sup-*` from clearing the gate on the regime filter alone:
`trend_up` covers 23.7% of bars, and an unconditional control would be trading in the 76.3%
where sup can't. For `sup-*` the unconditional control is still **reported, not gated**. It
measures the regime filter and Jev together.

**Consequence: the replay must cover every cycle.** Spot has no stop, so exit timing comes
entirely from Jev. The v1 controls enter on any cycle, and sup positions (strategy or control)
outlive their `trend_up` episode. A cheaper replay that only scores `trend_up` cycles would
leave both controls without exits. CH-1 doesn't change the cost.

## Gate (unchanged code: `gate_failures`)

Each config is gated on its own. A config is a forward-paper candidate only if all of these
hold:

1. n ≥ 100 trades, pooled;
2. net PF > 1 overall and in both halves (split at 2025-05-10);
3. sum% > 0 in at least 2 of 3 symbols;
4. it beats its gated control (§Control) in **≥ 997/1000** seeds.

**p̂ convention (CH-4).** A loss is a seed whose pooled control sum% is ≥ the strategy's (ties
count against the strategy). p̂ = losses/1000, and the gate needs p̂ ≤ 0.05/16 = 0.003125,
i.e. losses ≤ 3, beats ≥ 997. In code it is `pass_pctile = 1 − 0.05/16` = 0.996875, and
`need = ceil(0.996875 × 1000) = 997`. This is the same convention as grid 2 (0.05/12 → 996).
It is not the permutation-test (losses+1)/(seeds+1) convention, which would need losses ≤ 2
(beats ≥ 998). Resolution: at 1,000 seeds, 13 to 16 configs all land on 997, and a 17th
config on this window moves the bar to 998.

## Report (CH-2: reported, not gated)

For each config, per symbol and pooled:

- **n per arm.** Strategy n. Control n as the median and 5th–95th percentile across seeds.
  `sup-*` reports both the regime-matched and the unconditional control.
- **Hold time per arm,** in 5-minute cycles: p25 / p50 / p75 / p90. Control holds are pooled
  over seeds. The strategy-vs-control p50 gap exposes the exit-proximity effect (§Limits).
- Beats-random seed count (raw, not just pass/fail), net PF overall and per half, per-symbol
  sum%, funding-stressed PF for perps.

## Limits: what a pass means (CH-2)

A PASS is a **system-level** claim: *this entry rule beats random timing under the same Jev
exits.* It is **not** a claim that Jev's pump scores predict returns.

- The entry gates and the exit trigger read the same autocorrelated Jev stream. The strategy
  enters only at low-dump states, so its exit tends to be far away. Random entries can land
  where a dump exit is about to fire. Part of any win can come from "don't enter next to the
  exit trigger" rather than from return prediction. The hold-time gap in §Report shows the
  size of this effect but doesn't remove it.
- For `sup-*`, the regime-matched control (CH-1) takes the regime filter out of the
  comparison. The exit-proximity effect remains.
- Contamination (§Contamination) caps what any PASS can mean: forward paper only.

## Contamination: this test can only falsify

The model was built on 2026-09-17, after the window, and we don't know its training data.
Residual leak channels after stripping (CH-3):

1. **Training data:** unknown and can't be closed.
2. **Scale fingerprints:** `last_10_trades_side_usd` and `oversized_*_count_60s` (§Replay 2)
   let the model re-infer symbol and era.
3. **Volatility level** in the returns and the price path: a weak era signal that any market
   input carries.

So:

- **FAIL** is a kill, **if the pilot's agreement check passed**. The strategy couldn't win
  even with possible leakage, and original Jevelin is closed. If the agreement check fails,
  there is no run and therefore no FAIL to claim.
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

### Pilot (≤ $0.15), 2026-09-28 +07, all three symbols

09-28 is outside the window and is the busiest logged day (3,429 distinct live states). The
pilot uses the replay's fixed 5-minute grid: 288 cycles × 3 symbols = 864 cycles, with three
calls per cycle (full inputs, full inputs again, stripped inputs). That is 2,592 calls, about
$0.10 at the logged mean. The cap is $0.15 to leave room for fan-out and retries.

1. **Service:** the model is still served; parse rate and latency.
2. **Rebuild fidelity:** rebuilt states and full-input verdicts compared with the live logs
   for the same day. If the verdict distributions disagree badly, the run stops and the replay
   isn't the original.
3. **Stripped-vs-full agreement (CH-3, pre-declared, stops the run).** Jev is stochastic
   (§Verdict cache), so identical raw verdicts are unattainable. Agreement is measured on the
   **decision vector**: the 8 Jev-dependent decision bits the 4 configs act on. Regime, risk,
   cooldown and minimum hold are deterministic and identical in both arms, so they're excluded.
   Whipsaw uses the first sample.

   | # | bit | rule |
   |---|---|---|
   | 1 | sup long entry | pump≥65 ∧ phase∈{breakout,accumulation} ∧ whipsaw≤0.45 ∧ exhaustion≤0.55 ∧ confidence≥0.65 |
   | 2 | sup exit, soft | dump≥65 |
   | 3 | sup exit, hard | dump≥75 |
   | 4 | sup short entry | phase∈{breakout,accumulation} ∧ dump≥65 |
   | 5 | v1 long entry | pump≥60 ∧ whipsaw≤0.5 ∧ exhaustion≤0.6 ∧ confidence≥0.6 |
   | 6 | v1 long exit | dump≥60 ∨ exhaustion≥0.8 |
   | 7 | v1 short entry | dump≥60 ∧ whipsaw≤0.5 ∧ exhaustion≤0.6 ∧ confidence≥0.6 |
   | 8 | v1 short exit | pump≥60 ∨ exhaustion≥0.8 |

   - **A** = the share of the 864 cycles where the stripped and full (first call) verdicts give
     an identical vector. **The run stops if A < 95%.**
   - **Validity:** the check counts only if chance agreement is ≤ 90%. Chance agreement pairs
     each stripped vector with the full vector of a random other cycle of the same symbol
     (seed 0). Above 90%, the day is too quiet to tell faithful from uninformative, and the
     run stops. Reference values from the live 09-28 verdicts, pooled across symbols: chance
     agreement 51.5%; an arm that never fires any bit would score 70.3%.
   - **Noise floor (reported, not gated):** R = the same agreement between the two full-input
     calls. Reference: the 1,801 live repeat pairs agree on 97.6% of vectors. That subset
     (whipsaw in 0.4–0.6, 09-27→09-30) fired fewer bits than 09-28, so R on 09-28 may be
     lower. If A < 95% and R < 95% as well, the stop comes from Jev's own sampling noise, not
     from stripping. **The run stops either way, and the 95% threshold is not moved after the
     fact.**

## Run (once, after the pilot passes and the spend is approved)

The CLI is defined when it is built. The window is 2024-01-01 .. 2026-09-17 +07, end
exclusive (`FIT_CUTOFF_MS`). Output: `docs/reports/2026-10-04-original-jevelin-fit.md`.
