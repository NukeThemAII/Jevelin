# Frankenstein proposal — Gunbot mechanics x Jevelin verdict loop x v2 harness

Status: PROPOSAL — green-lights scope only, not a build spec.
Author: Oracle (Research & Intel), 2026-10-04 +07. For: Ada (scope ruling), Forge (build owner if green-lit).
Nothing here has been built or run. No funds touched. The original-Jevelin replay thread
(`docs/reports/2026-10-04-original-jevelin-prereg.md`, Forge, dev `b7555b2`) runs in parallel and is
referenced, not duplicated.

---

## 0. One-paragraph summary

Gunbot's public mechanics (strategy composition, trailing, DCA, config automation) are worth
stealing as *structure*; original-Jevelin's real asset is its supervisor loop and dual-book risk
caps, not its LLM-as-entry-source architecture; the v2 harness owns the only trustworthy decision
pipeline (pre-reg + Bonferroni + random control). Grid 2 falsified raw entry-timing prediction, so
the AI belongs in **regime classification and per-regime configuration (exits, risk budget, on/off)** —
AI chooses the machine's settings, never the trade. Bounded v1: one new pre-declared grid (g3,
fresh venue = spot) of regime-routed composite configs, judged by the existing gate at
**>= 998/1000 seeds**, no new pipeline, no paid API, no live funds.

---

## 1. Gunbot public mechanics our harness lacks (generalizable only)

Sourced from Gunbot's own docs (links in Sources; public mechanics only — no proprietary code, and
none needed: every mechanism below is a description of behavior, re-implementable from scratch).

| mechanic | what Gunbot does (public docs) | our harness today | gap / verdict |
|---|---|---|---|
| multi-strategy switching | AutoConfig jobs change pair/strategy settings on a schedule, filtered by market conditions (RSI thresholds, liquidity scans) [S3] | one static `BaseConfig` frozen per run (`jev_base.py`) | real gap — but live retuning is exactly what breaks pre-reg discipline. Steal the *idea* (condition -> config), implement it as a **pre-declared router inside the config**, not as live job scheduling [I] |
| DCA / averaging down | futures DCA places averaging orders when Tenkan crosses and price is >= `DCA_SPREAD` from last order; docs warn position size grows and liquidation price worsens [S4] | no pyramiding; one position, ATR stop | gap, but **defer**: averaging down is negative-curve exposure and collides with the risk-cap philosophy of original-Jevelin. v1 = flat, no DCA [I] |
| trailing buy / sell | TSSL: percentage trailing ranges around bid/ask, executes only after reversal; Take Buy/Take Profit secondary trailing layers; ROE trailing for futures; StepGrid trails every grid step [S1][S2] | chandelier trail (`trail_atr`, ratchets) on exits only; entries fill at next price, no entry trailing | partial. Exit trailing exists. **Entry trailing (buy the dip after reversal) and secondary layers are untested levers** — cheap to add as an exit/entry module later, not in v1 [I] |
| indicator ensembles | builder: any two methods as long/short triggers (adx, bb, emaspread, macd, ichimoku, tssl, support/resistance, ...) plus Confirming Indicators module — all conditions must agree in the same cycle [S5] | entry family (`donchian`/`tsmom`) + trend filter (`none`/`ema`/`regime`) — effectively a 2-condition ensemble | covered in spirit; the builder's lesson is compositional: **triggers AND confirmers must co-fire in one cycle** (and its own docs warn over-ANDing kills trade frequency) [S5]. Matches our n>=100 reality |
| triggers / automation | AutoConfig: schedule + pair scope + filters + variables + changes; recommended review-before-enable workflow [S3] | none (config is code, frozen per run) | see multi-strategy: adopt as pre-declared router only |
| backtester approach | parameter optimization / simulation over historical data; community-documented TradingView backtesting add-on [S6][S7] (marketing-grade sourcing — treat exact tooling claims as soft) | pre-declared grids, run once, 1,000-seed random control, Bonferroni across all configs ever tested on the window, halves + per-symbol checks (`jev_base_backtest.py`) | **ours is strictly stronger statistically.** Gunbot-style parameter sweeps on one window is precisely the "search over this window" our reports forbid. Nothing to steal here except ergonomics |

**Take:** steal composition (triggers + confirmers), trailing layers, and condition->config routing as
*mechanisms*. Do not steal DCA (v1) or optimizer-style backtesting (ever, on a spent window).

## 2. Original-Jevelin and the v2 harness: fit and conflict

Grounded in Forge's archaeology + pinned code (`jev_base.py`, `jev_base_backtest.py`, `jev_state.py`).
Forge's backtest-validation thread is the parallel workstream; see the pre-reg [R1] — not duplicated.

**Original-Jevelin (Jev-verdict dual-book loop):** each cycle sends a market snapshot to the Jev API
which answers 5 questions (pump, dump, phase, exhaustion, whipsaw); **every entry depends on those
answers**; deterministic regime gates, cooldowns, risk caps and sizing only filter/size; two books
(spot + perps) run the loop independently. v1 `paper_loop.py` traded 10+10 round trips; the
supervisor (M3-M5) made 0 trades in 3,694 cycles — its regime gate (trend_up 5/3,649 cycles), not
Jev, was the blocker [R1].

**v2 harness:** price-only base strategy decides every entry and exit from OHLCV; Jev is
**veto-only** (`jev_veto` can block, never create/size/exit) [C1]; decisions judged by
pre-registration + Bonferroni + rate/side-matched random control over 1,000 seeds + halves +
per-symbol [C2].

**Fit:**
- dual-book maps 1:1 to `--book perps|spot` runs of the harness; risk caps/sizing sit outside the
  unit-notional backtest and stay in the live/paper supervisor.
- original-Jevelin's regime gate maps to `trend: "regime"` (`jev_regime` labels) and to the
  supervisor's on/off logic — reusable as the deterministic router baseline.
- the supervisor loop (snapshot -> verdict -> gate -> act -> heartbeat/alerts) is the right live
  skeleton for anything that passes the harness.

**Conflict (the load-bearing one):** original-Jevelin makes the LLM the *entry source*. That is the
exact claim class grid 2 falsified for price rules ("the best of 12 configs landing in the top 10%
of its null is what chance delivers" [F2]) — an LLM entry trigger faces the same random-control bar,
plus Jev's contamination problem (model built 2026-09-17, training data unknown; replay is
falsification-only [R1]). So the Frankenstein keeps v2's decision architecture (price/rules decide,
LLM may veto or route) and takes original-Jevelin's *loop and risk skeleton*. Secondary conflicts:
DCA-style averaging down vs risk caps (deferred), and Gunbot-style live retuning vs frozen pre-reg
configs (resolved: router = part of the pre-declared config).

## 3. The AI angle — strongest honest case after grid 2's falsification

**What grid 2 actually killed** (sourced [F2]): raw entry timing under Donchian/ATR. Best config
dc20-4h-w: net PF 1.20, positive in 3/3 symbols, funding-robust — yet random entries with the same
exits beat or match it in 99/1,000 seeds (901/1000 vs gate 996). Grid 1: even gross, best capture
0.089%/trade vs ~0.20% round-trip cost [F1]. Also falsified along the way: tsmom entries (loses
gross), EMA50/200 as an entry filter at 4h (PF 1.03 vs 1.20), and the deterministic `regime` label as
an *entry trend filter* at 1h (dc20-regime ~= dc20) [F1].

**What the same data says value lives in** (inference from [F1][F2]):
1. **Exit/cost structure was the only lever that moved numbers**: fewer, larger trades + wider exits
   took the same entry rule from PF 0.83 to 1.20 (lever-attribution table [F2]).
2. **Regime matters for exposure, not for entry direction**: the supervisor's 0 trades came from the
   regime gate (trend_up 23.7% of bars over the window) [R1] — being *in the market* is regime-
   dependent even though *entry direction* is not predictable.
3. **Edge is front-loaded** (PF B <= 1 in 3 of 6 grid-2 configs) [F2] — whatever works must be
   re-derivable, i.e. classification/selection that adapts, not a fixed rule fished once.

So the AI buys us **configuration intelligence, not price prediction**:

- **(A) Regime classification -> per-regime config routing** (recommended). A small classifier maps
  market state to one of a few pre-validated rule-sets: trend -> wider trail + exposure, chop ->
  flat or short-hold, stress -> risk-off. This is a *classification over a small label set* — the
  kind of problem ML is actually good at — and every routed outcome is still judged by the same
  gate. Note the honest caveat: as an *entry trend filter* the deterministic regime label already
  failed at 1h [F1]; the claim here is narrower — regime for **exits and exposure**, which grid 2's
  lever table supports.
- **(B) Ensemble signal filtering/ranking** (second choice, partially built). Cheap families
  (donchian, tsmom, bb-style) vote/rank; a model (or the LLM via the existing `jev_veto` [C1])
  *blocks or ranks* signals instead of creating them. This is already 80% of the v2 architecture.
- **(C) Position sizing** (defer to v1.5). Vol-target / capped fractional-Kelly sizing exploits the
  same finding (returns concentrate per regime), but the harness is unit-notional; honest testing
  needs a small reporting extension. Not free, not in v1.

**Original idea, for the record (speculation, clearly labeled):** "regime-budgeted dual-book
allocator" — the AI is a fund-of-funds manager over our own cheap strategy families and the two
books (spot/perps), allocating a risk budget per regime while every underlying entry stays
deterministic. It is the Gunbot-AutoConfig idea with pre-reg discipline. Elegant, but it needs (C)
to test honestly — v2 candidate, not v1.

**Why not "AI predicts entries" one more time:** grid 2 is a direct falsification of the claim class,
and Jev's entry answers carry an unclosable training-data leak [R1]. Spending compute there re-runs
a failed experiment with a fancier model.

## 4. Bounded v1 — "g3: regime-routed exits, fresh venue"

Testable in the existing harness with **no new pipeline**: `simulate`/`random_control`/
`gate_failures`/`run` reused as-is except one extension to `random_control` (regime-matched
schedule — the CH-1 precedent already accepted in the amended pre-reg [R1]).

- **Scope change (required — Ada's ruling after grid 2: no grid 3 on this window without a declared
  fresh scope [F2]):** venue perps -> **spot** (grid 1/2 were priced at perps costs [F1][F2]), and
  the hypothesis class changes from entry-timing to routing/exits. Ada must explicitly rule this is
  "fresh enough"; if not, the fallback fresh scope is timeframe (1d bars) at the cost of trade count.
- **4 composite configs** (config budget is deliberate — see Bonferroni below):
  - `C1` baseline: best grid-2 rule (dc20-4h-w shape), spot costs, static exits.
  - `C2` C1 + deterministic regime router (jev_regime): flat in chop, on in trend_up/trend_down.
  - `C3` C2 + per-regime exit profiles (trend: `trail_atr` 5 / `stop_atr` 3; chop: time-stop exit;
    stress: widened stop) — the grid-2 lever, parameterized by regime.
  - `C4` C3 with the router swapped for a **small local classifier** (logistic/GBM over OHLCV +
    regime features, walk-forward trained with the `FIT_CUTOFF_MS` contamination discipline, no
    paid API). **C4 vs C3 is the entire AI increment** — if C4 does not beat C3, the AI adds nothing
    and we say so.
- **Gate (unchanged mechanics):** n >= 100, net PF > 1 overall and in both halves, majority of
  symbols positive, beats random control — all pre-registered before the single run, peer-checked
  (Oracle) before it fires.
- **Bonferroni arithmetic:** 16 configs tested to date on this window (6 g1 + 6 g2 + 4 in the
  original-Jevelin pre-reg [R1]) + these 4 = **20**; 0.05/20 = 0.0025 -> losses <= 2/1000 ->
  **pass bar >= 998/1000 seeds** (`pass_pctile = 0.9975`). Stated up front so nobody re-derives it
  after seeing results.
- **Control:** regime-matched random entries (same regime schedule as C2-C4, rate- and side-matched,
  same exits) for routed configs — CH-1's lesson: an unmatched control lets the router win on the
  deterministic filter alone. Per-arm n and hold-time percentiles reported (CH-2).
- **Risk:** on 4h spot bars n >= 100 is plausible (grid 2's spot-priced siblings reached 238-591
  trades [F2]) but regime-routed configs trade less; if pre-run counting shows n < 100 is likely,
  the grid dies at the gate — cheap failure, that is the point.
- **Explicitly out of v1:** DCA, entry trailing, LLM in the loop (veto stays forward-paper-only),
  sizing, any live or demo execution.

## 5. Guardrails (non-negotiable)

1. **No live funds. Ever, in this thread.** Backtest on the fit window, then forward paper only.
   A pass at the gate *earns forward paper* — the same ceiling the original-Jevelin replay has.
2. **Demo-first / paper-first:** even after a g3 pass, execution goes through the existing paper
   supervisor path before anything with a balance is discussed.
3. **Nothing bypasses the pipeline:** pre-registration committed before the run, peer design check
   (Oracle) before it fires, one run, results reported with all failures. No grid 4 on this window
   without Ada's scope ruling.
4. **Bonferroni ratchet:** every config ever tested on this window counts. 20 configs -> >= 998/1000.
   The bar is set before the run and never moved after.
5. **No paid API in v1.** The classifier is local and cheap. Any future LLM judge is veto-only
   (`jev_veto`) and needs the operator's explicit spend approval, exactly like the pending ~$33
   OpenRouter call in [R1] — which remains a separate, parallel, still-unapproved spend.
6. **Contamination discipline:** walk-forward training, `FIT_CUTOFF_MS` respected, no post-cutoff
   data in any fitted component; the fitted classifier's features must be leak-audited the way [R1]
   audits Jev's inputs (absolute-USD features fingerprint symbol/era — CH-3).
7. **Deterministic-first honesty:** C4 must beat C3 (the free deterministic router) or the AI claim
   is rejected regardless of gate outcome. We report "AI added nothing" if that is the truth.

---

## Sources

Repo (code is the source of truth):
- [C1] `scripts/jev_base.py` — price-only base, `jev_veto` veto-only architecture.
- [C2] `scripts/jev_base_backtest.py` — pre-declared grids, `random_control`, `gate_failures`.
- [F1] `docs/reports/2026-10-04-base-backtest-fit.md` — grid 1 result (12 tested total with g2).
- [F2] `docs/reports/2026-10-04-base-grid2-fit.md` — grid 2 falsification + lever attribution.
- [R1] `docs/reports/2026-10-04-original-jevelin-prereg.md` (dev `b7555b2`, amended per CH-1..CH-4)
  — original-Jevelin archaeology, contamination findings, replay design. Parallel thread.

Gunbot public docs (mechanics descriptions only; vendor docs = marketing-adjacent, [S6]/[S7] soft):
- [S1] StepGrid — https://www.gunbot.com/support/docs/built-in-strategies/spot-strategies/stepgrid/
- [S2] Trailing guide (TSSL, Take Buy/Take Profit, ROE trailing) —
  https://www.gunbot.com/support/docs/guides/trading-logic-and-optimization/trailing-in-gunbot/
- [S3] AutoConfig — https://www.gunbot.com/support/docs/gunbot-unlimited/automate-and-extend/autoconfig/
- [S4] Futures DCA (Tenkan trigger, DCA_SPREAD, stated risks) —
  https://www.gunbot.com/support/docs/built-in-strategies/futures-strategies/builder/dca/
- [S5] Futures strategy builder (buy/sell methods, confirming indicators, execution-gate model) —
  https://www.gunbot.com/support/docs/built-in-strategies/futures-strategies/builder/about-builder/
- [S6] Backtesting overview (vendor) — https://www.gunbot.com/topics/trading-bot-backtesting-improving-strategies/
- [S7] Backtesting add-on via TradingView (community) — https://www.reddit.com/r/gunbot/comments/l80wox/gunbot_backtesting_addon_faq_ultimate_guide/

Labels: [S*] sourced fact (vendor docs), [F*]/[C*]/[R*] sourced fact (our repo/records), [I] =
inference, "speculation" is called out inline.
