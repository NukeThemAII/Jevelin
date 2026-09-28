# Jevelin — AI Agent Guide

> **For:** Any AI agent (Hermes, MiMoCode, Cline, Claude Code, Codex) working in this repo.
> **What:** Jev-powered dual-book crypto trading system (spot + perps), paper-first, on Binance.
> **Goal:** Prove an AI-decision edge with real numbers before any money moves. Honest numbers only.

---

## 1. Identity & provenance

| | |
|---|---|
| **App name** | **Jevelin** — Jev is in the name because the decision brain IS the product; javelin = fast, precise, hits the target |
| **Repo** | `NukeThemAII/Jevelin` (fork of `sopersone/cabbage-trading-machine`) |
| **Local** | `/home/xaos/Jevelin` (venv: `.venv`, Python 3.11, works; upstream recommends 3.12) |
| **Upstream pin** | `fff7436f12ea95a2e5f794ce6800663fc21ef8ec` (upstream is 1 day old, moving fast — re-check before merging) |
| **Runtime** | Investing Algorithm Framework **9.0.0a18** (by MDUYN / Finterion), **vendored in-repo** (`investing_algorithm_framework/`), installed as `-e .` |
| **License** | Apache-2.0 — whitelabel OK. Keep `LICENSE`, `AUTHORS.md`, `UPSTREAM-README.md` attribution. |
| **Our app layer** | `cabbage/` (4 files, ~220 lines) → becomes `jevelin/` in the whitelabel pass (P0-2) |

Fork relationship is live (`upstream` remote configured). Upstream got 30 stars on day 1 — expect churn; do not blindly merge.

**What the upstream marketing claimed vs reality (verified 2026-09-25):**
- Tweet: "Jev (AI) scores smart-money wallets, calibrated 80% model probability, +$6,400".
- Reality: **no Jev code, no wallet scanner, no verified trade history.** `README.md` itself admits it ("Still absent from the supplied upstream"). The bundled backtest produces **0 trades / 0.0 gain** on its own fixture.
- Takeaway: the *runtime* is real and good; the *story* is vapor. We build the missing intelligence ourselves — that's the product opportunity. Never inherit promo claims into our docs.

---

## 2. System architecture (as built and verified)

```
Binance public data (ccxt, keyless: OHLCV, trades, ticker, funding rate)
   │
   ▼
jev_state.build_state()          60s price path + last trades + flow features
   │
   ▼
jev_questions.QUESTIONS          5 questions: pump, dump, phase, exhaustion, whipsaw
   │                              (CoinGecko Pump Pulse port + our whipsaw gate)
   ▼
jev_client.JevClient             POST openrouter.ai/api/alpha/decisions
   │                              fail-open, 1 retry, usage/cost tracking, JSONL audit log
   ▼
jev_scorer.ShadowScorer          verdict: pump_0_100, dump_0_100, phase, exhaustion_prob,
   │                              whipsaw_prob, confidence (0-100 normalization)
   ├──────────────────────────────┐
   ▼                              ▼
jev_gates.decide()               jev_perps.decide_perps()
spot: enter/exit/skip            perps: enter_long/enter_short/exit/skip
size = 0.20 × confidence         margin = 0.10 × confidence, leverage ≤ 3
   ▼                              ▼
jev_paper.PaperPortfolio         jev_perps.PerpsPortfolio
   │                              stops, liq price, funding accrual, no-flip
   └──────────┬───────────────────┘
              ▼
   paper_loop.py                  one verdict per cycle drives BOTH books
   runtime/*.json + *.trades.jsonl (decision + trade audit logs)
```

| File | Role |
|---|---|
| `scripts/jev_client.py` | Jev API client (OpenRouter decisions endpoint) |
| `scripts/jev_probe.py` | standalone API probe |
| `scripts/jev_questions.py` | the question fan-out (CoinGecko Pump Pulse port + whipsaw) |
| `scripts/jev_state.py` | deterministic market-state builder |
| `scripts/jev_scorer.py` | data → state → Jev → normalized verdict |
| `scripts/shadow_scorer.py` | CLI: score a symbol live (no trading) |
| `scripts/jev_gates.py` | spot decision layer (verdict → action + risk vetoes) |
| `scripts/jev_paper.py` | spot paper portfolio (PnL, persistence, trade log) |
| `scripts/jev_perps.py` | perps paper book (long+short, stops, liq, funding) |
| `scripts/jev_risk.py` | M5 portfolio risk (per-pair/basket caps, global kill, drawdown halt) |
| `scripts/paper_loop.py` | dual-book cycle loop (one verdict → both books) |
| `scripts/jev_scout.py` | M6 CoinGecko discovery scout (pair-universe discovery, reproducible passes) |

The original RSI/EMA engine (`cabbage/` + vendored framework, below) remains as the runtime base.

**Design principle (TypeSafe's own guidance): keep code in control.** Jev answers questions —
it never outputs order sizes, never places orders, never invents numbers. All money math is
deterministic, auditable code. On any error the system fails OPEN: skip the trade, log the
reason, never fabricate a verdict.

## 3. Trading design

### Books (paper; promotion to live is a separate, human-gated decision)

| | Spot book | Perps book |
|---|---|---|
| Direction | long-only | long + short |
| Sizing | confidence tiers of the 20% cap (60/80/100%) | confidence tiers of the 10% margin cap, `notional = margin × leverage` |
| Leverage | 1x | hard cap **3x** |
| Stop-loss | exit rules only | **mandatory on every position** (2% default) |
| Liquidation | n/a | computed liq price; forced close loses whole margin |
| Funding | n/a | paid/received on close per 8h periods; entries vetoed when funding runs against the side (>±0.01%/8h) |

### Entry/exit gates (both books, M3 numbers — all in `config/v2.yaml`)
1. No/broken verdict (`ok=False`, `confidence=None`, NaN/missing keys) → **skip** (`no_verdict`/`malformed`)
2. Position held → exit rules only (**no flip** same cycle): `dump≥65` × 2 consecutive cycles OR one cycle `dump≥75` (pump mirrored on shorts); min hold 3 cycles before signal exits; stops/liq fire automatically in the portfolio and always bypass
3. **Portfolio risk (M5, entries only — exits/stops never risk-blocked)**: portfolio daily PnL ≤ −5% → `global_daily_kill`; drawdown ≥ 10% from running peak → `drawdown_halt` (clears only below 5%); per-pair caps → `pair_cap`; basket caps → `basket_cap`; remaining capacity < 1% equity → dust veto `pair_cap`/`basket_cap`. Risk computation error → entries fail CLOSED. See the caps table below (`scripts/jev_risk.py`)
4. Daily loss ≤ −5% → block entries only (`daily_loss_kill`); exits and stops always allowed
5. Regime (deterministic `jev_regime`, computed before Jev): `chop` → ALL entries blocked (`regime_chop`); `trend_down` blocks longs / `trend_up` blocks shorts (`regime_counter`; `counter_trend: allow` re-enables by config)
6. `phase ∈ {breakout, accumulation}` (`phase_not_in_entry_set`; `capitulation` keeps its own flag for attribution)
7. Shared entry gates: `whipsaw≤0.45` (2-sample fan-out majority vote when raw noul ∈ 0.40–0.60; split → `whipsaw_fanout_tie` fail-closed), `exhaustion≤0.55`, `confidence≥0.65`, 15-min cooldown
8. Long needs `pump≥65`; short needs `dump≥65`; funding veto per book table
9. Both sides qualify → prefer the stronger (`pump≥dump` → long, else short)

**Portfolio risk caps (M5, `config/v2.yaml` `portfolio:` / `runtime/risk_state.json`):**

| cap | limit | base | veto flag |
|---|---|---|---|
| pair spot | 30% | that pair's spot book equity (notional at mark) | `pair_cap` |
| pair perps | 10% margin | that pair's perps book equity | `pair_cap` |
| basket long | 40% | total equity (all books), notional at mark | `basket_cap` |
| basket short | 20% | total equity (all books), notional at mark | `basket_cap` |
| global daily loss | −5% (UTC-midnight reset) | portfolio total equity | `global_daily_kill` |
| drawdown halt | ≥10% from running peak (<5% clears) | portfolio total equity | `drawdown_halt` |
| dust floor | <1% book equity deployable | book equity | `pair_cap`/`basket_cap` |

Sizing is decided by code — confidence tiers: [0.65, 0.70) → 60% of cap, [0.70, 0.85) → 80%, ≥0.85 →
100% (caps unchanged: spot 20%, perps 10% margin / 3x; `size_tier` logged on every fill), then
clamped by the remaining pair/basket capacity (M5). Every threshold lives in `config/v2.yaml`
(`config_version: 4`, loader in `scripts/jev_config.py`; CLI flags override the yaml). Jev never
chooses amounts. Change caps by changing config — nothing can exceed them.

**Fill model (M0, live since 2026-09-27):** every fill pays a fee + slippage per side —
spot `fee_rate=0.001` (operator's real Binance rate 0.10%), perps `taker_fee_rate=0.0005`
(Binance USDT-M taker 0.05%), `slippage_rate=0.0005` (5 bp, adverse; settable to 0 for
perfect-limit-fill analysis). All in `scripts/jev_config.py`, overridable via
`paper_loop.py --fee-rate/--slippage-rate`. Every decision has a `decision_id` end-to-end
and every decision record carries the full `veto_bitmask` of ALL failing gates.

### Cadence (split — deliberately not per-second)
- **Jev verdicts:** every **5 min** per pair + **event bursts** (volatility/trade-rate spike → score immediately for a few cycles). Jev calls cost ~$0.00002; redundancy is the enemy, not cost.
- **Stop/liquidation checks:** every **10 s** — deterministic, free, and the real clock for risk.
- **Cooldown between entries:** 15 min (in the gates).

### Pairs
- **v1 (now):** BTCUSDT — clean single-stream stats.
- **v1.5:** BTC, ETH, SOL (static liquid majors).
- **v2 (dynamic universe — BUILT as M6, 2026-09-28):** `scripts/jev_scout.py` pulls CoinGecko `/search/trending` + `/coins/markets`, maps to Binance USDT pairs, filters (Binance 24h quote volume ≥ $5M, listing age ≥ 30d, `not_binance_listed`/`low_volume`/`young_listing`/`trending_only` rejects), ranks deterministically (`candidate_score` = 0.5·vol rank-norm + 0.5·|24h change| rank-norm; ties lexicographic) and caps at 5. Hourly pass cadence; every pass is reproducible offline (`jev_scout.py --replay <pass-ts>`, byte-identical). Opt-in supervisor mode `--universe scout` re-reads the latest pass at slow-cycle boundaries (stale > 2× interval / empty / broken → static fallback, logged; pairs with open positions stay managed until flat). CoinGecko supplies discovery, Binance supplies data/execution — every trade still passes the full gate stack.

### Rollout doctrine (hard gates, no skipping)
1. **Shadow/paper** — dual books running, everything logged ← *we are here*
2. **Analysis** — replay logs: did Jev vetoes beat baseline? Calibration check on `confidence`
3. **Promotion gate** — N days paper + stat thresholds + explicit user approval
4. **Live** — tiny float first, exchange-side stops required, human confirms every promotion step

---

## 4. Jev — verified API facts (2026-09-25)

```
POST https://openrouter.ai/api/alpha/decisions     # NOT chat/completions — Jev is a "decisions model"
Authorization: Bearer $OPENR..._KEY
{ "model": "typesafe/jev-1.13",                    # slug VERSIONED on OpenRouter ("jev-latest" invalid)
  "state": "<string or JSON string>",
  "questions": { <qid>: {"type": "choice"|"score"|"noul", "instructions": ..., "criteria": ...} } }
```

→ `answers`: `choice{choice, probabilities, confidence}` | `score{score, legend, probabilities, confidence}` | `noul{noul}` + `usage{input_tokens, output_tokens, cost}`.

- Measured cost: **$0.0000141–$0.000038 per call** (5-question fan-out ≈ $0.00004), latency 360–475 ms.
- **`noul` answers carry NO confidence field** — gate those on probability thresholds only.
- Native TypeSafe API (`api.typesafe.ai/v1/systemone`) is the same model, waitlisted; OpenRouter is the working route. Key lives in `.env` (gitignored).
- `scripts/jev_probe.py` reproduces live verification; `runtime/jev_decisions.jsonl` is the audit log (state hash + raw response per call — never fabricate, always store raw JSON).

---

## 5. Verification status (this VPS, 2026-09-25)

| Check | Result |
|---|---|
| 5 test suites (client/scorer/gates/paper/perps) | ✅ **77/77** |
| Upstream suites (`cabbage_tests`, `tests.app.test_paper_trading`) | ✅ 6/6 + 12/12 |
| `hermes verify` (recipe in `.hermes/environment.json`) | ✅ ok: True |
| Live Jev calls | ✅ real answers, e.g. `pump strong p=0.87 conf=0.86` |
| Live Binance → verdict pipeline | ✅ `pump=43 dump=40 phase=distribution whipsaw=0.46` |
| Live dual-book cycle | ✅ both books act on one verdict (spot + perps lines logged) |
| Live risk veto | ✅ `skip vetoed_by=capitulation` on real market state |

---

## 6. Delegation & tooling

- **Default coder: Cline + mimo-v2.6-pro** (`cline -P xiaomi-token-plan-sgp --yolo`), Token Plan = **$0**. Handles multi-file jobs reliably (long iteration loops).
- **Fast scalpel: Cline + Opus 5.5** (`cline -P openrouter --yolo`), risk-critical code or urgency. Measured **$0.37–1.36 per job** (scales with spec size).
- MiMoCode CLI (`~/.mimocode/bin/mimo`) also available ($0), same model.
- **One delegated coding job at a time.** Hermes specs, delegates, and **verifies every claim by running tests + a live smoke before commit** — agent self-reports are not evidence.
- **Cost measurement pitfall (verified):** OpenRouter `/api/v1/auth/key` usage counters LAG real billing (read $0.09 minutes before it settled at $0.37); agent self-reported costs undershoot too. Trust the dashboard or settled reads only.

---

## 7. Free stack — data, RPC, models (DYOR 2026-09-25)

**Market data (what this bot actually needs):** CCXT public endpoints — **keyless and free**.
`enableRateLimit=True` is already set. Bitvavo public API verified reachable from this VPS.
Backfill/cache OHLCV locally (the framework caches; `scripts/bench_ccxt_ohlcv_cache.py` exists).
Binance/Bitvavo public REST rate limits are handled by ccxt's rate limiter — don't hammer with
parallel workers. No API key needed until live trading (exchange account keys, which are trading
credentials, not data keys).

**RPC (only needed if we ever go onchain — P3):** free tier reality, and we already have keys:

| Provider | Free tier (verify at signup) | Notes |
|---|---|---|
| **Alchemy** | 30M compute units/mo, 25 rps | **Keys already exist** in EarnGrid env (Base). Reuse for reads; create a separate app for Jevelin if we go serious |
| **dRPC** | ~10M req/mo public | EarnGrid primary; keyless endpoints exist |
| **Dwellir** | 100K responses/day, 20 rps | Archive node — good for history |
| **Chainstack** | permanent free dev plan | Fallback |
| **publicnode / base.org** | free, rate-limited | Last-resort fallbacks |

EarnGrid's full measured RPC stack (fallback order, degradation) lives in `EarnGrid/RPC.md` —
copy the pattern, not the keys, when the day comes. Base is where our infra already is.

**Model (Jev):** measured **~$0.00002/call** via OpenRouter → run rate ≈ **$0.005–0.01/day**. Access routes:
1. **OpenRouter** — `typesafe/jev-1.13` pay-per-token ✅ **live** (key in `.env`).
2. **`console.typesafe.ai/keys`** — official TypeSafe route (early-access waitlist) — grab one when approved.
3. **Vercel AI Gateway** — also serves it.

**CoinGecko (v2 pair scout):** free public API (`/search/trending`, `/coins/markets`), keyless,
rate-limited — discovery only; Binance supplies execution data.

**Bottom line:** current bot = $0 infra (public CCXT data + local SQLite + paper mode).
Jev ≈ $0.01–0.10/mo. RPC = $0 until onchain.

## 7. Conventions (standing rules)

- **Branches:** `dev` = agent work; `main` = verified/stable. Never delete branches or files
  without explicit user approval. Never force-push. Merge dev→main only after user verification.
- **Delegated coding:** see §6 — Cline+mimo default, Opus scalpel, one job at a time. Hermes specs
  and verifies every agent's claims by running tests.
- **TDD:** tests before code for anything touching orders, money, or risk. `cabbage_tests/` is ours;
  `tests/` is upstream's — don't break upstream tests silently.
- **Live money safety:** paper-first, deterministic gates, no hidden broker writes. Live mode stays
  behind explicit user approval. Demo/paper stats must precede any real-money claim.
- **Honest numbers only:** live data or clearly-labeled fixtures. No invented PnL, no promo claims.
- **Docs describe live state**, not aspirations. When code and docs disagree, fix the doc.
- Repo is **private** — credentials in tracked files are accepted (family convention), but `.env`
  stays gitignored as-is; don't start a secrets-management campaign.
- Times reported in Asia/Bangkok.

## 8. Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-cabbage.txt   # NOT uv sync (broken lock)

# Jev decision system (our layer)
.venv/bin/python scripts/shadow_scorer.py --once               # live Jev score, no trading
.venv/bin/python scripts/jevelin_supervisor.py --once          # M2 supervisor: 1 slow cycle + fast ticks
.venv/bin/python scripts/jevelin_supervisor.py --once --pairs BTCUSDT,ETHUSDT,SOLUSDT  # M5: explicit universe
.venv/bin/python scripts/jevelin_supervisor.py --max-cycles 12 # bounded live supervisor run
.venv/bin/python scripts/paper_loop.py --once                  # deprecated fallback: one dual-book cycle
.venv/bin/python scripts/paper_loop.py --interval 300          # deprecated fallback loop
.venv/bin/python scripts/jev_probe.py                          # API probe
.venv/bin/python scripts/jev_summary.py                        # offline stats: net PnL, fees, per-gate vetoes
.venv/bin/python scripts/jev_import.py                         # JSONL -> SQLite (idempotent)
.venv/bin/python scripts/jev_replay.py --summary               # store vs JSONL cross-check
.venv/bin/python scripts/jev_calibrate.py --out report.md      # M4: calibration report, on-demand (no cron)
.venv/bin/python scripts/jev_calibrate.py --since 2026-09-27   #   (optional: filter data from a date)
.venv/bin/python scripts/jev_scout.py --once                  # M6: one discovery scout pass (CoinGecko + Binance)
.venv/bin/python scripts/jev_scout.py --replay <pass-ts>      # M6: reproduce a pass offline (byte-identical)
.venv/bin/python scripts/jevelin_supervisor.py --once --universe scout  # M6: scout-driven pair universe (opt-in)

# tests — all 16 script suites must stay green
for t in test_jev_client test_jev_scorer test_jev_gates test_jev_paper test_jev_perps \
         test_jev_store test_jev_import test_jev_replay test_jev_radar \
         test_jev_cache test_jev_supervisor test_jev_regime test_jev_config \
         test_jev_calibrate test_jev_risk test_jev_scout; do
  .venv/bin/python scripts/$t.py; done

# original engine (upstream RSI/EMA app layer, unchanged)
.venv/bin/python -m cabbage doctor              # config check, no orders
.venv/bin/python -m cabbage doctor --online     # + public exchange data, no orders
.venv/bin/python -m cabbage backtest            # offline event backtest + HTML report
.venv/bin/python -m cabbage paper --iterations 1  # one paper iteration (live data)
.venv/bin/python -m cabbage paper               # continuous paper runtime
.venv/bin/python -m cabbage live                # REAL orders — explicit, needs keys

.venv/bin/python -m unittest discover -s cabbage_tests -v        # upstream-adjacent tests
.venv/bin/python -m unittest tests.app.test_paper_trading -v     # upstream paper tests
```

`.env` (gitignored) from `.env.example`: `OPENROUTER_API_KEY`, `JEVELIN_MODEL=typesafe/jev-1.13`,
`CABBAGE_*` settings + `{MARKET}_API_KEY`/`{MARKET}_SECRET_KEY` for live only. (Names flip to
`JEVELIN_*` in the whitelabel pass — update this section then.)

## 9. v2 build plan (design approved 2026-09-26)

Full blueprint: **`docs/V2-DESIGN.md`** (audit of the first 8.7h paper run + v2 design). Coding
sessions implement it milestone by milestone — one milestone per session, in order:

| M | Build (file scope in V2-DESIGN.md B.7) | Why |
|---|---|---|
| **M0** | Instrument v1: fee+slippage model, decision ids, per-gate veto bitmask, atomic writes | honest numbers + analyzable (audit F-P0-1/3/4) |
| **M1** | SQLite store + JSONL importer + replay utilities | free replay = the calibration backbone |
| **M2** | Split-cadence supervisor (fast risk loop 5-10s, slow Jev 5min + burst trigger) + decision cache | stops react fast, Jev spend ↓ |
| **M3** | Regime classifier (chop filter) + entry/exit hysteresis + confidence sizing tiers + config file | kills the churn the audit found (PF 0.613 in chop) |
| **M4** | Calibration harness: per-gate attribution, confidence curve, weekly re-tune report | the machine that learns whether Jev has an edge |
| **M5** | Multi-pair (BTC/ETH/SOL) + portfolio risk (per-pair caps, global kill, drawdown halt) | scale + portfolio-grade safety |
| **M6** | CoinGecko discovery scout (trending → Binance liquidity/age filters) | dynamic universe (B.4a; CG = where to look, never what to do) — ✅ built 2026-09-28, see roadmap #7 |
| **M7** | Observability: Telegram feed, daily summary, promotion-gate report | operator visibility + go/no-go automation |

Key v2 decisions already made (don't re-litigate): fees+slippage mandatory in all PnL; regime
`chop` forbids entries; exits need hysteresis (2 consecutive cycles or ≥75 single tick); whipsaw
gate cuts at 0.45 with self-consistency fan-out in the 0.40–0.60 band; shorts must become
reachable or the perps book merges into a leverage tier; CoinGecko cross-venue divergence and
vote-score signals are CUT. Promotion gate thresholds: ≥500 round trips, net PF ≥1.15, DD ≤3%.

## 10. Roadmap

| # | Item | Status |
|---|---|---|
| 1 | Jev decision stack (client → scorer → gates → books) | ✅ done, 77/77 |
| 2 | 8.7h paper run + full audit (docs/V2-DESIGN.md Part A) | ✅ done 2026-09-26 |
| 3 | v2 design blueprint (Part B) | ✅ approved — build plan §9 |
| 4 | M0: instrument v1 (fees/slippage, ids, bitmask, atomic writes) | ✅ done 2026-09-27 (commit 39a175a, 94/94 tests; `jev_summary.py`) |
| 5 | M1: SQLite store + JSONL importer + replay utilities | ✅ done 2026-09-27 (commit 70858a6; `jev_replay.py --summary` cross-check) |
| 6 | M2: split-cadence supervisor + decision cache (`jevelin_supervisor.py`) | ✅ done 2026-09-27 (24h sim 73/288 Jev calls = 25.3% ≤ 40%; stops ≤10s; 32/32 new tests) |
| 7 | M3 regime+hysteresis → M4 calibration → M5 multi-pair → M6 CoinGecko scout → M7 observability | M3 ✅ done 2026-09-27 (`jev_regime.py`, config v2, fan-out, tiers; 276 tests / 13 suites); M4 ✅ done 2026-09-28 (commit `2814ab5`; `jev_calibrate.py`: counterfactual veto-value engine + per-gate attribution/confidence curve/fan-out + hysteresis stats/re-tune proposals, `gate_decisions`+`calibration_runs` store tables; 304 tests / 14 suites); M5 ✅ done 2026-09-28 (commit `9b5c0fb`; `jev_risk.py` portfolio risk: per-pair/basket caps, global daily kill, drawdown halt w/ hysteresis, dust floor; config `config_version: 3` with `pairs`/`portfolio`; 3-pair supervisor default universe + batch `fetch_tickers` marks + `risk_state.json`; symbol-aware trades dedup key; 368 tests / 15 suites); M6 ✅ done 2026-09-28 (`jev_scout.py` reproducible CoinGecko discovery scout: trending → Binance map → liquidity/age filters → deterministic candidate_score rank → cap 5, every pass cached + `--replay` byte-identical, `scout_runs` store table, fault injection keeps last good list; opt-in `jevelin_supervisor.py --universe scout` at slow-cycle boundaries w/ static fallback; config `config_version: 4` + `scout:` section; 435 tests / 16 suites) — M7 queued |
| 8 | Whitelabel pass: `cabbage`→`jevelin` pkg rename, `JEVELIN_*` env | P2 (cosmetic) |
| 9 | Paper→live promotion gate (B.6 thresholds + explicit user approval) | gated on M4 data |
| 10 | Live: Binance keys, exchange-side stops, tiny float ($100, ≤2x) | gated on #9 |
| 11 | Onchain execution (Base) — reuse EarnGrid RPC stack | P3 |

*(Name locked: **Jevelin**. "TradeGrid" retired 2026-09-25.)*
