# Jevelin v2 — Audit of the first paper run + v2 design blueprint

> **Status:** design approved as the build blueprint for sessions M0..M7 (2026-09-26).
> **Scope:** Part A = audit of the 2026-09-25/26 BTC/USDT paper run (numbers recomputed from raw
> artifacts, not copied from summaries). Part B = v2 architecture, rule tables, schemas, CoinGecko
> evaluation, promotion gate, milestone plan.
> **Honesty rules:** every number in Part A is reproducible from `runtime/` (method in §A.0).
> Anything not measured is labelled **ASSUMPTION** or **ASSUMPTION-TO-VERIFY**. n=10 round trips is a
> smoke test, not a statistic — nothing here claims an edge exists or does not exist.
> Times: UTC in tables (log native); Bangkok = UTC+7.

---

# PART A — AUDIT

## A.0 Sources, method, reconciliation

| Artifact | Content (verified) |
|---|---|
| `runtime/loop_24h.log` | 1026 lines = **513 spot + 513 perps** status lines, 0 unparsed, 0 `cycle error` lines. First cycle 2026-09-25T18:20:32Z, last 2026-09-26T03:04:45Z (01:20→10:04 Bangkok) |
| `runtime/jev_decisions.jsonl` | 518 lines: 1 probe (2-question), 4 pre-run 5-question calls, **513 in-run 5-question calls**, **0 errors** |
| `runtime/paper_btc.json.trades.jsonl` | 20 lines = **10 buy/sell round trips** |
| `runtime/perps_btc.json.trades.jsonl` | 20 lines = **10 enter_long/exited round trips**, 0 shorts |
| `runtime/paper_btc.json`, `runtime/perps_btc.json` | flat; spot cash 9998.9246, perps equity 9998.3868 |

Method: log lines parsed with one regex (100 % match); verdicts re-derived from the raw Jev answers
(`score/3*100`, confidence = min of pump/dump/phase confidences, exactly as `jev_scorer.py`); log
cycles joined to verdicts by nearest timestamp (offset median 0.92 s, max 3.04 s — the decision log
has **no cycle/decision id**, see F-P1-3). Scratch scripts lived in `/tmp`, nothing in the repo changed.

**Reconciliation with the brief:**
- "493 cycles, →02:45" — the log actually continues to 03:04:45; **494** cycles fall ≤ 02:45:59, **513** total.
- "19 trades each, spot −0.67 / perps −1.00" — at the 02:45 snapshot there were 19 trade lines
  (10 entries + 9 exits; 10th position open). Realized after 9 closed trips = **−0.6685 / −1.0029** ✅.
  The 10th trip closed at 02:59:38 for −0.4069 / −0.6103, so the **final realized PnL is
  −1.0754 (spot) and −1.6132 (perps)**, 20 lines each.

## A.1 Run facts

| Metric | Spot book | Perps book |
|---|---|---|
| Cycles | 513 | 513 (same verdict, same price) |
| Cadence | median 61.4 s (60 s sleep + ~1.4 s work → drift), min 61.3, max 63.9, no gaps > 90 s | same |
| Round trips | 10 (all long) | 10 (all long, **0 shorts**) |
| Win / loss | 4 / 6 | 4 / 6 |
| Realized PnL | **−$1.0754** | **−$1.6132** |
| Sum wins / sum losses | +1.7063 / −2.7817 | +2.5592 / −4.1724 |
| Profit factor | **0.613** | **0.613** |
| Mean / median trip | −0.1075 / −0.2927 | −0.1613 / −0.4390 |
| Best / worst trip | +0.8514 / −0.7107 | +1.2770 / −1.0660 |
| Notional per trip | $1,200–1,380 (0.20 × conf × equity) | $1,800–2,069 (0.10 × conf × 3x) |
| Cycles holding a position | 74 / 513 (14.4 %) | 74 / 513 |
| Equity min / max | 9998.09 / 10000.22 | 9997.14 / 10000.33 |
| Max drawdown (cycle marks) | **$2.13 (0.021 %)**, trough 01:05:00Z | **$3.19 (0.032 %)**, trough 01:05:00Z |
| Funding paid | n/a | **0.0** on all 10 trips (see F-P1-6) |
| Stops / liquidations | n/a | 0 / 0 (2 % stop vs. trip moves of ≤ 6.2 bp) |

Equity path (spot / perps vs $10,000, hourly marks): 19:00 −0.54/−0.80 · 20:00 −0.38/−0.57 ·
22:00 −0.66/−0.99 · 00:00 −1.20/−1.80 · 02:00 −1.06/−1.59 · 03:04 −1.08/−1.61.

Market context: BTC 84,060 → 84,000; range 83,638.01–84,156.00 (**0.616 %** over 8.7 h);
1-cycle return sd 2.71 bp, mean |r| 1.82 bp; 43/512 cycles with zero price change. A quiet, choppy tape.

**The perps book is not a second strategy.** Shorts were structurally impossible (A.4), so perps
= the spot trades at 1.5× notional (0.30×conf vs 0.20×conf). Identical PF 0.613 confirms it.
The "dual book" currently doubles exposure to one signal, not diversification.

## A.2 Veto distribution (first failing gate, per book)

| Outcome | Spot | Perps |
|---|---|---|
| `low_pump` | **361** | 7 |
| `high_whipsaw` | — (checked after pump) | **242** |
| `low_confidence` | 45 | 176 |
| `capitulation` | 22 | 0 (checked after whipsaw/conf) |
| `cooldown` | 1 | 3 |
| `low_dump` | n/a | 1 |
| hold (in position) | 64 | 64 |
| enter / exit | 10 / 10 | 10 / 10 |

Same verdicts, different veto labels: attribution depends on check order (`jev_gates.py` checks
pump first, `jev_perps.py` checks whipsaw first). v1 logs only the *first* failing gate, so
"which gate saved us" cannot be answered from logs (F-P1-4).

## A.3 Jev decision statistics (recomputed from `runtime/jev_decisions.jsonl`, 518 calls)

| Statistic | Value | Read |
|---|---|---|
| Calls | 518 (513 in-run, 5 pre-run), **0 errors** | reliability perfect |
| Pump score | mean 27.9, median 27.3, max 77.0 | mostly weak pumps — quiet tape |
| Dump score | mean 35.1, median 34.3, max 76.7 | slightly stronger on the downside |
| Phase | distribution 263 (51%) · accumulation 124 (24%) · breakout 68 (13%) · ranging 36 (7%) · capitulation 26 (5%) | 75% no-trend states — phase reads the chop honestly |
| Confidence | mean 0.749; **only 2.5% exactly at 0.60**; 19% below 0.6 | not stuck at threshold (good) |
| Exhaustion noul | mean 0.415, median 0.420 | sane |
| Whipsaw noul | mean 0.498, **median 0.520; 52.8% of cycles > 0.5** | the 0.5 veto is cutting at the median of its own distribution — a coin-flip gate, see F-P0-2 |
| Autocorrelation (lag-1, 60s) | pump 0.161, whipsaw 0.300 | verdicts are weakly autocorrelated — 60s cadence is not as redundant as feared; keep as burst/data-collection mode |

## A.4 Code review findings (prioritized)

**F-P0-1 — No fee/slippage model; paper PnL is overstated.** `apply_action` fills at quote with zero fees.
At 0.1% taker + ~2 bp slippage a $1,300 round trip pays ~$3.1 — larger than every gross win except one.
Realistic net PnL is meaningfully worse than the −$1.08/−$1.61 printed. **Fix before trusting any paper stat.**

**F-P0-2 — No regime filter → churn in chop (the core finding).** 10 trips inside a 0.62% overnight
range; PF 0.613. Entries fired on `pump≥60`; exits followed minutes later on `dump≥60`. The whipsaw
gate cuts at its own median (A.3), so it can't distinguish chop from danger. **v2 must gate entries
on a deterministic regime classifier + add exit hysteresis** (B.3).

**F-P1-3 — No cycle/decision ids.** Verdicts, trades, and states are joined only by timestamp
(the audit needed a median-0.92s nearest-join). Any serious analysis needs a decision id end-to-end.

**F-P1-4 — Logs record only the FIRST failing gate.** "Which veto saved us" is unanswerable from
v1 logs. Attribution must be per-gate (a bitmask or a row per gate).

**F-P1-5 — Single-tick exits.** One `dump≥60` cycle closes a position (min hold observed: 2 min).
Hysteresis (N consecutive cycles or a higher single-tick bar) is the standard cure.

**F-P1-6 — The perps book is a leverage copy, not a second strategy.** 0 shorts in 513 cycles:
short entries need `dump≥60` AND `whipsaw≤0.5` AND `conf≥0.6` simultaneously — that conjunction
never occurred. Funding accrual: 0 on all trips (holds < 8h). Both books have identical PF (0.613).
Either make shorts reachable (relax to `whipsaw≤0.55` or use self-consistency fan-out) or accept
one book with a leverage tier.

**F-P2-* — hygiene:** single-symbol hardcoded defaults; JSON persistence not transactional (a crash
mid-write can corrupt state — use atomic tmp+rename or SQLite); `fetch_ohlcv`+`fetch_trades` repeated
per book per cycle; no config file (argparse only); no metrics export; no decision cache (replays
cost real money); `runtime/` writes interleave with tests' tmp paths (verify before CI).

**S-* (keep):** fail-open everywhere; pure deterministic gates with 77/77 tests; raw verdict JSON
stored (no fabrication); code-in-control sizing; cheap (2 cents/8h).

---

# PART B — v2 DESIGN

## B.1 Design goals (driven by the audit)
1. **Kill the chop churn** (F-P0-2): regime filter + entry/exit hysteresis + recalibrated whipsaw gate.
2. **Honest accounting** (F-P0-1): fees + slippage in every book.
3. **Analyzable** (F-P1-3/4): decision ids, per-gate attribution, SQLite store, free replay.
4. **Two books that mean something**: either a reachable short path or a single multi-tier book.
5. **Portfolio-grade risk**: per-pair caps, correlation awareness, global daily kill, drawdown halt.
6. **CoinGecko only where it earns** (B.4): discovery + context features — never execution data.

## B.2 Architecture (layered, split-cadence supervisor)

```
                    ┌──────────────── Supervisor (asyncio, stdlib) ────────────────┐
 Binance public ──▶ │ market-data layer: REST poll + optional WS (ccxt pro)         │
 (REST keyless)     │   → ticks/OHLCV/trades → ring buffers + SQLite market store   │
                    │                                                                │
                    │ fast loop (5–10 s): mark prices · stops/liq · portfolio risk  │
                    │   checks · daily-kill/drawdown enforcement  [FREE, no Jev]    │
                    │                                                                │
                    │ slow loop (5 min + burst trigger on vol/flow spike):           │
                    │   state builder (+regime features) → decision cache? ──┐      │
                    │        │ no                                            │      │
                    │        ▼                                               │      │
                    │   Jev fan-out (pump/dump/phase/exhaustion/whipsaw/regime)      │
                    │        ▼                                                       │
                    │   gates: regime → Jev gates (hysteresis) → portfolio gates     │
                    │        → sizing tiers → book executors                        │
                    └────────────────────────────────────────────────────────────────┘
                            │                        │
                     SQLite decision/trade/position store (ids, replay, calibration)
                            │                        │
                  PaperExecutor (fees+slippage)  ·  reporting (JSONL/Telegram/daily)
```

Modules: `jevelin/data/` (market data), `jevelin/state/`, `jevelin/decision/` (cache, fan-out,
gates, regime), `jevelin/portfolio/` (books, risk), `jevelin/execution/` (paper, future live),
`jevelin/store/` (SQLite, replay), `jevelin/reporting/`. v1 scripts map into these layers — no
throwaway, but the supervisor + store are new.

## B.3 Regime + calibration (anti-churn core)

**Regime classifier (deterministic, before Jev):** 15m/60m realized vol vs. ATR-based expectation,
Donchian(20) width percentile, short EMA slope. Output: `chop | trend_up | trend_down`. In `chop`:
entries FORBIDDEN (all books). In `trend`: entries allowed with the trend side preferred. Classifier
is a pure function of OHLCV — free, testable, and auditable.

**Entry/exit hysteresis (kills single-tick churn):**
- Enter: `pump≥65` (spot) / `dump≥65` (short) AND `phase ∈ {breakout, accumulation}` AND
  `whipsaw ≤ 0.45` AND `exhaustion ≤ 0.55` AND `conf ≥ 0.65`, plus regime `≠ chop`.
- Exit: `dump≥65` (long) / `pump≥65` (short) for **2 consecutive cycles**, OR single cycle ≥75,
  OR stop/liq. Min hold: 3 cycles.
- Whipsaw gate recalibrated: cut at **0.45** (below the observed median) after self-consistency
  fan-out (2 samples, majority) when `0.40 < noul < 0.60` — the coin-flip band (A.3).

**Sizing tiers (confidence banding):** conf 0.65–0.70 → 60% of cap · 0.70–0.85 → 80% · >0.85 → 100%.
Caps unchanged (spot 20%, perps 10% margin / 3x). All thresholds live in a versioned config file
(`config/v2.yaml` or frozen dataclass), never in code — calibration mutates config, not code.

**Calibration harness:** replay recorded verdicts + price paths: PF per gate combination, per-gate
attribution (F-P1-4 fixed), confidence vs. realized win rate (calibration curve), veto value
(what each veto saves). Weekly re-tune proposal → human approval → new config version. Replay is
free (recorded verdicts) — this is where we learn whether Jev has an edge at all.

## B.4 CoinGecko evaluation (build-or-cut, brutal)

| Idea | Value | Risk | Verdict |
|---|---|---|---|
| (a) Trending/gainers scout → dynamic pair universe w/ Binance liquidity floor | feeds discovery; catches moving coins before they are everywhere | illiquid junk, pumps-and-dumps | **BUILD P1** — hourly pull, rank by 24h vol + price Δ, filter: Binance-listed, 24h vol ≥ $5M, listing age ≥ 30d, cap at N=5 pairs. Discovery only — every trade still goes through the full gate stack. Free-tier limits (ASSUMPTION-TO-VERIFY: ~10–30 req/min keyless, 5-min staleness) are fine for hourly pulls |
| (b) Cross-venue divergence (CG aggregate vs Binance price) | cheap alpha if CG lags | CG free data is 5-min stale aggregates; not tradeable | **CUT** |
| (c) Market-cap/volume/age as Jev state context features | better AI priors ("this is a low-cap coin") | none material | **BUILD P2** — one CG call per pair per hour, cached |
| (d) CG vote/trending score as a trade signal | hype timing | uncalibrated, gameable | **CUT** — may revisit as P3 after calibration data exists |
| (e) Top-gainers hourly list as a burst-trigger source | focuses the slow loop on where action is | noise | **BUILD P2** — trigger only, never a signal |

**CoinGecko's role in v2, in one line:** *tell us where to look (a/c), never what to do (b/d).*

## B.5 Store schemas (SQLite, `runtime/jevelin.db`)

`decisions(id, ts, cycle_id, symbol, regime, state_json, verdict_json, cache_hit, cost, latency_ms, ok)`
· `trades(id, decision_id, book, side, price, qty, notional, fees, slippage, realized_pnl, funding_paid, reason, veto_bitmask)`
· `positions(id, book, symbol, side, qty, entry_price, stop_price, liq_price, entry_ts, close_ts)`
· `marks(ts, symbol, price, equity, book)` · `config_versions(id, yaml, applied_ts)` · `calibration_runs(id, params, metrics_json)`.
Every trade carries its `decision_id` (F-P1-3) and `veto_bitmask` (F-P1-4). Replay = SQL over these tables.

## B.6 Promotion gate (paper → live, no skipping)

All of: ≥ **500 round trips** (or ≥ 21 days) with fees+slippage; **profit factor ≥ 1.15 net**;
**max drawdown ≤ 3%** of book equity; win rate ≥ 45%; positive expectancy with a confidence
interval; ≥ 2 distinct regime days (chop + trend); calibration report approved by the user.
Live starts at **$100 float**, ≤2x leverage, exchange-side stops mandatory, human approves
every scale-up. Anything failing → stay paper, adjust config, re-run.

## B.7 Milestones (each = ONE coding session, file-level, acceptance tests)

| M | Build | Acceptance |
|---|---|---|
| **M0** | Instrument v1: fee+slippage model (F-P0-1), decision ids (F-P1-3), per-gate veto bitmask (F-P1-4), atomic state writes | tests green; one 6h run; replay-summary script |
| **M1** | SQLite store + importer (B.5) + replay utilities | all v1 JSONL imports losslessly; replay reproduces −1.08/−1.61 |
| **M2** | Split-cadence supervisor: fast risk loop (5–10s, free) + slow Jev loop (5 min + burst trigger) + decision cache | stops react ≤10s in test; cache hits logged; 24h run at ≤ 40% of v1 Jev spend |
| **M3** | Regime classifier + hysteresis gates + sizing tiers (B.3), config file v2 | unit tests for classifier on synthetic chop/trend; gates reject all chop entries; PF improvement target in replay |
| **M4** | Calibration harness: per-gate attribution, confidence curve, weekly re-tune report | report generated from M0–M3 data; veto-value table rendered |
| **M5** | Multi-pair (BTC/ETH/SOL) + portfolio risk (per-pair caps, global daily kill, drawdown halt, correlation lite) | 3-pair paper run, no pair > cap; risk halts trigger in fault-injection tests |
| **M6** | CoinGecko discovery scout (B.4a) with liquidity/age filters + Jev candidate scoring | hourly scout list reproducible; rejected-junk rate > 90% on live data; pairs feed M5 |
| **M7** | Observability: metrics, Telegram decision/trade feed, daily summary (EarnGrid pattern), promotion-gate report generator | daily report lands in Telegram; go/no-go report auto-generated from store |

M0–M3 are the core (anti-churn + honest numbers). M5–M6 add scale. Every milestone: TDD,
tests-first for money/risk paths, full suite green, commit + push, verified by Hermes.

## B.8 v1 reuse map
`jev_client.py` → `jevelin/decision/client.py` (unchanged) · `jev_questions.py` → +regime question ·
`jev_state.py` → +regime features · `jev_scorer.py` → split from gating (scorer vs gates) ·
`jev_gates.py`/`jev_perps.py` → unified gate engine with hysteresis + bitmask · `jev_paper.py`/
`jev_perps.py` → `jevelin/portfolio/books.py` with fees/slippage · `paper_loop.py` → replaced by
supervisor (M2). Engine (`cabbage/`, framework) untouched.
