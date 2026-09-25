# Jevelin — AI Agent Guide

> **For:** Any AI agent (Hermes, Claude Code, Codex) working on this repo.
> **What:** Whitelabel spot-trading engine, forked from `sopersone/cabbage-trading-machine`.
> **Goal:** Build our own product on a proven runtime. Paper-first, free infra, real numbers only.

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

## 2. Architecture (verified)

```
CabbageStrategy (cabbage/strategy.py)
  → upstream RSI/EMA confluence pipeline (signal_cards, ScoreRules)
  → upstream risk rules (StopLossRule, ExposureRule, ...)
  → upstream order service
  → CCXTOrderExecutor (live) | PaperTradingOrderExecutor (paper)
  → portfolio/trade services → SQLite + RunReport (JSON + HTML)
```

- **Strategy:** RSI(14) + EMA(12/26) crossover. Long-only spot. Entry = RSI<30 (3 pts) + recent EMA cross (2 pts), confluence ≥5. Exit = RSI≥70 + recent crossunder. Closed candles only (drops the forming candle, refuses <30 bars).
- **Config:** `cabbage/config.py` — frozen dataclass, env-driven (`CABBAGE_*`), validates everything up front.
- **Modes:** `doctor` / `backtest` (offline fixture) / `paper` / `live` (explicit, requires `{MARKET}_API_KEY`+`{MARKET}_SECRET_KEY`).
- **State:** `runtime/<mode>/<market>/<pair>/` — SQLite, run reports. `.gitignore`d. Don't run two processes on one state dir.
- **Native:** `native/confluence/` = optional Rust wheel for hot loops. Python path works without it.

## 3. Verification status (this VPS, 2026-09-25)

| Check | Result |
|---|---|
| `pip install -r requirements-cabbage.txt` | ✅ clean, `pip check` OK |
| `unittest discover -s cabbage_tests` (6 integration tests) | ✅ 6/6 |
| `unittest tests.app.test_paper_trading` (12 upstream tests) | ✅ 12/12 |
| `python -m cabbage doctor` | ✅ config OK |
| `python -m cabbage doctor --online` | ✅ live Bitvavo public API reachable (BTC/EUR bid/ask verified) |
| `python -m cabbage backtest` | ✅ runs, HTML report written — **0 trades** on bundled fixture (known, honest) |

Note: upstream's own VALIDATION.md said Bitvavo was unreachable from their environment. From here it works. Their `runtime/` artifacts are gitignored; regenerate locally.

---

## 4. Audit findings

**Verdict (honest):** the framework is professional-grade — layered domain/services/infrastructure, event+vector engines with parity tests, deterministic accounting, hundreds of tests. That part is done and it is the hard part. The 220-line wrapper is clean and disciplined (validated config, explicit live gate, paper/live isolation guards, atomic writes) but strips risk features and is single-symbol. **Worth further development: yes. Worth live money: not yet** — P0-3 and P0-4 below are blockers, and the vanilla RSI/EMA strategy by itself is machinery, not an edge.

**Strengths (keep):**
- S1: Real framework, not a toy — event + vector backtest engines, SQLite accounting, run reports, risk-rule engine (stop-loss/TP/cooldown/exposure/scaling), studies/optimizer/permutation testing already inside.
- S2: Correct safety posture — `live` is an explicit command; paper mode **cannot** be flipped to live by upstream env overrides (guard in `application.py`); live refuses to start without keys; incomplete-candle guard in strategy.
- S3: Honest delivery — `VALIDATION.md` reports 0 trades and disclaims profitability. Atomic report writes (tmp+rename).
- S4: Test harness covers the full paper path (BUY placed→filled→SELL placed→filled, real SQLite accounting, no mocks on execution).

**Findings (fix in order):**
- **F1 (opportunity):** Jev integration absent — our differentiator. See §5.
- **F2 (bug-in-waiting):** Symbol plumbing leaks upstream constants. `strategy.py` uses identifier `'BTC_ohlcv'` and `super().prepare_signal_data` keys output by `simple_app.SYMBOL='BTC'`; the remap to `settings.base` works, but anything multi-symbol needs a refactor. Single-symbol only today.
- **F3 (risk config):** Wrapper strips `take_profits`, `cooldowns`, `scaling_rules` and sets `ExposureRule(max_portfolio_percentage=100)` — *less* protection than the upstream example (80% exposure, TP 10%/50%, sell-cooldown 12 bars). Restore sane risk defaults before any live run.
- **F4 (live risk):** Stops are client-side only. If the process dies mid-position, nothing protects it. Mitigation: watchdog + venue-side stop orders where supported (verify Bitvavo stop-loss order support) — before live.
- **F5 (backtest realism):** Paper fills at quote with flat fee. Slippage models exist in the framework (`test_slippage_models.py`) — use them; paper results are otherwise optimistic.
- **F6 (audit trail):** Framework has `decision_trace` / `record_schemas` — wire them so every signal decision is persisted. Required for Jev calibration analysis (§5).
- **F7 (dependency):** Framework is alpha (`9.0.0a18`) and vendored. We control it (good) but must track upstream fixes manually. Pin known-good; diff before upgrading.
- **F8 (docs):** README contains upstream promo framing + a RU starter. Ours will be rewritten in the whitelabel pass.
- **F9 (CI):** Upstream workflows are `.disabled`. Add our own CI (tests + lint) on the fork.
- **F10 (exchange defaults):** BITVAVO/BTC-EUR/2h defaults are fine for EU, but this is a global product — exchange/pair/timeframe are already env-driven; docs and defaults should follow.

---

## 5. Idea #1 — JevGate (the "try out Jev" plan)

**Jev (TypeSafe "System One" model) = calibrated typed decisions, not text.** Fast (~100ms), cheap
($0.042/1M input tokens, output free). API:

**Verified live 2026-09-25 via OpenRouter** (`scripts/jev_probe.py` reproduces it):

```
POST https://openrouter.ai/api/alpha/decisions        # NOT chat/completions — Jev is a "decisions model"
Authorization: Bearer $OPENROUTER_API_KEY
{ "model": "typesafe/jev-1.13",                       # slug is VERSIONED on OpenRouter; "jev-latest" is invalid there
  "state": "<string or JSON string>",
  "questions": {
    "entry_quality": {"type": "choice", "instructions": "...", "criteria": {...}},
    "whipsaw":       {"type": "noul",   "instructions": "..."},
    "regime":        {"type": "score",  "instructions": "...", "criteria": [...]} } }
```

→ `answers`: `choice{choice, probabilities, confidence}` | `score{score, legend, probabilities, confidence}` | `noul{noul}` — plus `usage{input_tokens, output_tokens, cost}`.
Measured: 3-question fan-out = 530 input tokens = **$0.000022**, ~360ms, provider "TypeSafe". Native TypeSafe API (`api.typesafe.ai/v1/systemone`, `jev-latest`, `TYPESAFE_API_KEY`) is the same model but waitlisted — OpenRouter is our route today.
⚠️ **Noul answers carry NO confidence field** (only the probability). Confidence-gating applies to choice/score; for noul gate on probability thresholds and/or self-consistency fan-out (TypeSafe's own cookbook pattern).

**Design principle (from TypeSafe's own patterns doc): keep code in control.** Jev never places
orders and never invents numbers. The deterministic RSI/EMA pipeline stays the authority;
Jev is a **confidence-gated veto/confirm layer** on top of it.

Architecture:
```
signal event (RSI/EMA confluence fires)
  → build state (last N closed candles summary, RSI, EMA delta, position context, vol)
  → ONE Jev call, fan-out questions:
      entry_quality: choice(skip | normal | high)
      whipsaw:       noul (is this cross a fakeout?)
      risk_event:    noul (does state invalidate technicals — cascade/expiry/news?)
      regime:        score(range | trend | chop)
  → JevGate: mode=shadow|enforce, confidence thresholds, fail-open
  → decision log (JSONL/SQLite) + framework decision_trace
  → deterministic executor (unchanged)
```

Rollout (hard gates, no skipping):
1. **Shadow mode** — Jev verdicts logged next to paper trades, zero execution impact.
2. **Analysis** — replay logs: would Jev vetoes have improved PnL / max drawdown vs baseline? Calibration check on `confidence`.
3. **Enforce mode** — confidence-gated vetoes (e.g. skip entries when `whipsaw > 0.6` AND `confidence ≥ 0.8`).
4. **Paper-tracked**, then live only on user approval.

Engineering rules:
- **Fail-open:** API timeout/error → fall back to pure deterministic rule. Never block trading on a model API. One retry, hard timeout (~2s), circuit breaker.
- **Cost control:** one call per *signal event*, not per bar (dozens/day, not thousands) → cents per month. Compact state (~1-2KB). Decision cache keyed by (bar_ts, signal, config_hash). Backtests replay recorded answers — free.
- **No fabricated numbers:** store raw API JSON. If Jev is unavailable, the log says `jev: unavailable` — never invent a probability (this is exactly the upstream's sin).
- Tests use recorded fixtures; no live API calls in CI.

## 6. Idea backlog (own ideas, prioritized)

| # | Idea | Why | When |
|---|---|---|---|
| 1 | **JevGate** (§5) | Differentiator; nobody has calibrated decision models in open bots | P0 |
| 2 | **Whitelabel pass** — `cabbage`→`jevelin` pkg, `JEVELIN_*` env, CLI/docs rename, README rewrite, drop promo framing | It's our product now | P0 |
| 3 | **Risk hard-limits** — restore TP/cooldowns/scaling, exposure ≤80%, daily-loss kill-switch, drawdown halt | F3/F4 — non-negotiable before live | P0 |
| 4 | **Decision audit trail** (framework `decision_trace`) | Feeds Jev analysis + user trust | P0 |
| 5 | **Telegram trade/decision feed** — same pattern as EarnGrid reporting | Operator visibility from phone | P1 |
| 6 | **Walk-forward validation** — framework study/optimizer + permutation testing | Kills overfitting; honest stats | P1 |
| 7 | **Paper→live promotion gate** — N days paper + stat thresholds + explicit user approval | User's demo-first doctrine (T212 style) | P1 |
| 8 | **Slippage-aware backtests** (F5) | Realistic numbers before any real money | P1 |
| 9 | **CI on fork** (tests + lint) | Upstream has none active | P1 |
| 10 | Multi-symbol universe / cross-sectional momentum (framework pipelines support it) | Scale beyond single-pair | P2 |
| 11 | Exchange-side stop orders / watchdog | Live safety | P2 (before live) |
| 12 | Onchain execution (Base DEX) — reuse EarnGrid RPC stack | Far future; the "Robinhood Chain" thing upstream promised doesn't exist either | P3 |

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

**Model (Jev):** $0.042/1M input tokens, output free → our volume ≈ **cents/month**. Keys:
1. **`console.typesafe.ai/keys`** — official route, early-access waitlist, approval in waves.
2. **OpenRouter** — `typesafe/jev-1.13` pay-per-token (credits needed; cheapest way in without waitlist).
3. **Vercel AI Gateway** — also serves it.
Ask the user to grab a TypeSafe key (or OpenRouter credits) — that's the one credential this
project genuinely needs. Everything else runs free today.

**Bottom line:** current bot = $0 infra (public CCXT data + local SQLite + paper mode).
Jev ≈ $0.01–0.10/mo. RPC = $0 until onchain.

## 8. Conventions (standing rules)

- **Branches:** `dev` = agent work; `main` = verified/stable. Never delete branches or files
  without explicit user approval. Never force-push. Merge dev→main only after user verification.
- **Delegated coder: MiMoCode** (`mimo run --yolo`, `xiaomi/mimo-v2.6-pro`, Token Plan = $0 cost;
  CLI at `~/.mimocode/bin/mimo`). Claude Code only when quota available. Hermes = orchestration/ops
  + small fixes. One delegated coding job at a time. Verify every agent's claims by running tests.
- **TDD:** tests before code for anything touching orders, money, or risk. `cabbage_tests/` is ours;
  `tests/` is upstream's — don't break upstream tests silently.
- **Live money safety:** paper-first, deterministic gates, no hidden broker writes. Live mode stays
  behind explicit user approval. Demo/paper stats must precede any real-money claim.
- **Honest numbers only:** live data or clearly-labeled fixtures. No invented PnL, no promo claims.
- **Docs describe live state**, not aspirations. When code and docs disagree, fix the doc.
- Repo is **private** — credentials in tracked files are accepted (family convention), but `.env`
  stays gitignored as-is; don't start a secrets-management campaign.
- Times reported in Asia/Bangkok.

## 9. Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-cabbage.txt

.venv/bin/python -m cabbage doctor              # config check, no orders
.venv/bin/python -m cabbage doctor --online     # + public exchange data, no orders
.venv/bin/python -m cabbage backtest            # offline event backtest + HTML report
.venv/bin/python -m cabbage paper --iterations 1  # one paper iteration (live data)
.venv/bin/python -m cabbage paper               # continuous paper runtime
.venv/bin/python -m cabbage live                # REAL orders — explicit, needs keys

.venv/bin/python -m unittest discover -s cabbage_tests -v        # our tests
.venv/bin/python -m unittest tests.app.test_paper_trading -v     # upstream paper tests
```

`.env` (gitignored) from `.env.example`: `CABBAGE_*` settings + `{MARKET}_API_KEY`/`{MARKET}_SECRET_KEY` for live only. (Names flip to `JEVELIN_*` in the whitelabel pass — update this table then.)

## 10. Open questions for the user

1. **Jev key:** join the TypeSafe waitlist (`console.typesafe.ai`) and/or grab OpenRouter credits — tell me which lands and I'll wire the shadow-mode gate.
2. **Exchange scope:** stay Bitvavo/BTC-EUR for the experiment, or standardize on Binance (deeper liquidity, more pairs) for the whitelabel product? Both are CCXT-trivial.

*(Name is locked: **Jevelin**. Working name "TradeGrid" retired 2026-09-25.)*
