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
| `scripts/paper_loop.py` | dual-book cycle loop (one verdict → both books) |

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
| Sizing | `size_fraction = 0.20 × confidence` | `margin = 0.10 × confidence`, `notional = margin × leverage` |
| Leverage | 1x | hard cap **3x** |
| Stop-loss | exit rules only | **mandatory on every position** (2% default) |
| Liquidation | n/a | computed liq price; forced close loses whole margin |
| Funding | n/a | paid/received on close per 8h periods; entries vetoed when funding runs against the side (>±0.01%/8h) |

### Entry/exit gates (both books, in check order)
1. No/broken verdict (`ok=False`, `confidence=None`, NaN/missing keys) → **skip** (`no_verdict`/`malformed`)
2. Position held → exit rules only (**no flip** same cycle): long exits on `dump≥60` or `exhaustion≥0.8`; short exits on `pump≥60` or `exhaustion≥0.8`; stops/liq fire automatically in the portfolio
3. Daily loss ≤ −5% → block entries only (`daily_loss_kill`); exits and stops always allowed
4. `phase=capitulation` blocks longs only
5. Shared entry gates: `whipsaw≤0.5`, `exhaustion≤0.6`, `confidence≥0.6`, 15-min cooldown
6. Long needs `pump≥60`; short needs `dump≥60`; funding veto per book table
7. Both sides qualify → prefer the stronger (`pump≥dump` → long, else short)

Sizing is decided by code (`RiskConfig` / `PerpsConfig` constants × Jev confidence). Jev never
chooses amounts. Change caps by changing config — nothing can exceed them.

### Cadence (split — deliberately not per-second)
- **Jev verdicts:** every **5 min** per pair + **event bursts** (volatility/trade-rate spike → score immediately for a few cycles). Jev calls cost ~$0.00002; redundancy is the enemy, not cost.
- **Stop/liquidation checks:** every **10 s** — deterministic, free, and the real clock for risk.
- **Cooldown between entries:** 15 min (in the gates).

### Pairs
- **v1 (now):** BTCUSDT — clean single-stream stats.
- **v1.5:** BTC, ETH, SOL (static liquid majors).
- **v2 (dynamic universe):** CoinGecko `/search/trending` + `/coins/markets` (free) pick hot coins → filter to Binance-listed pairs with a liquidity floor → score those. CoinGecko supplies discovery, Binance supplies data/execution. Trending coins are where pump/dump questions shine — and where slippage bites; build after baseline stats exist.

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
.venv/bin/python scripts/paper_loop.py --once                  # one dual-book cycle
.venv/bin/python scripts/paper_loop.py --interval 300          # loop mode (5-min verdicts)
.venv/bin/python scripts/jev_probe.py                          # API probe

# tests — all 5 suites must stay green
for t in test_jev_client test_jev_scorer test_jev_gates test_jev_paper test_jev_perps; do
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

## 9. Roadmap

| # | Item | Status |
|---|---|---|
| 1 | Jev decision stack (client → scorer → gates → books) | ✅ done, 77/77 |
| 2 | **Loop service launch** — supervised process, auto-restart, decision logs | next |
| 3 | **Telegram decision/trade feed** (EarnGrid reporting pattern) | P1 |
| 4 | Multi-pair (BTC/ETH/SOL) then **CoinGecko trending scout** | P1 → P2 |
| 5 | **Analysis report** from accumulated logs (vetoes vs baseline, calibration) | P1, gates promotion |
| 6 | Whitelabel pass: `cabbage`→`jevelin` pkg rename, `JEVELIN_*` env | P2 (cosmetic) |
| 7 | Walk-forward validation + slippage-aware replay | P2 |
| 8 | Paper→live promotion gate (stats thresholds + explicit user approval) | gated on #5 |
| 9 | Live: Binance keys, exchange-side stops, tiny float | gated on #8 |
| 10 | Onchain execution (Base) — reuse EarnGrid RPC stack | P3 |

*(Name locked: **Jevelin**. "TradeGrid" retired 2026-09-25.)*
