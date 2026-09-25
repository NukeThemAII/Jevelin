# Jevelin

**Jev-powered crypto trading on Binance — dual-book paper trading with hard risk gates.**

Jevelin puts a calibrated AI decision model ([Jev](https://openrouter.ai/typesafe/jev-1.13), TypeSafe "System One") in the loop of a deterministic trading engine: the model reads the market state and answers typed questions (how strong is the pump/dump, what phase, is this a fakeout), and **code** — not the model — turns those answers into trade actions with strict risk limits. Paper-first: everything below runs and logs real decisions against live market data without moving real money.

## Why a decision model?

Classic bots compute indicators and hope. Jevelin asks a calibrated model for probabilities on the questions that actually decide momentum trades, then gates on those probabilities and confidence:

```
pump     score  none → building → strong → euphoric     "how strong is the move up?"
dump     score  none → building → strong → capitulating "how strong is the move down?"
phase    choice accumulation | breakout | distribution | capitulation | ranging
exhaustion noul  "is the move losing steam?"
whipsaw  noul    "is this breakout a fakeout?"           (ours, not in the original set)
```

The question set is ported from CoinGecko's open-source Pump Pulse Jev demo; the state builder, risk gates, and dual-book system are ours. Every answer is stored raw with a decision log — nothing is fabricated, ever.

## How it works

```
Binance public data (free, keyless)
   → state builder (price path, trade flow, features)
   → ONE Jev call with 5 questions          ~400ms, ~$0.00004
   → verdict (pump/dump 0-100, phase, exhaustion, whipsaw, confidence)
   → risk gates (deterministic)             vetoes: whipsaw, exhaustion, capitulation,
   │                                        daily-loss kill, cooldown, funding, malformed
   ├→ spot book   long-only,  ≤20% equity × confidence
   └→ perps book  long+short, ≤10% margin × confidence, leverage ≤3x,
                  mandatory stop-loss, liquidation accounting, funding accrual
   → JSONL decision/trade logs (full audit trail)
```

Key invariants:
- **Code is in control.** Jev answers questions; it never sizes orders, never places them.
- **Fail-open.** Any API error → skip the trade and log why. No fabricated verdicts.
- **Hard risk caps.** Nothing can exceed configured exposure, leverage, or loss limits.
- **Paper-first.** Promotion to live requires accumulated stats and explicit human approval.

## Status (2026-09-25, verified)

| | |
|---|---|
| Tests | 77/77 across 5 suites + 18 upstream tests green |
| Live pipeline | verified end-to-end on real Binance data |
| Live decisions | e.g. `skip, vetoed_by=capitulation` — risk gates working on real state |
| Trading | **paper only** — no live orders, no profit claims yet |

Numbers will come from accumulated paper stats. Until then, this page makes no PnL claims.

## Costs (measured, not estimated)

| What | Cost |
|---|---|
| One Jev decision (5 questions) | ~$0.00004 |
| Full-day run (5-min cadence, 1 pair) | ~$0.01 |
| Market data | $0 (Binance public API, keyless) |
| Infrastructure | $0 (runs on any $5 VPS or a laptop) |

## Quickstart

```sh
git clone https://github.com/NukeThemAII/Jevelin && cd Jevelin
python3 -m venv .venv
.venv/bin/pip install -r requirements-cabbage.txt

# .env: OPENROUTER_API_KEY=sk-or-...   (get credits at openrouter.ai)
cp .env.example .env

# see a live AI verdict on real market data (no trading):
.venv/bin/python scripts/shadow_scorer.py --once

# one paper cycle across both books (spot + perps):
.venv/bin/python scripts/paper_loop.py --once

# run it:
.venv/bin/python scripts/paper_loop.py --interval 300
```

Tests:

```sh
for t in test_jev_client test_jev_scorer test_jev_gates test_jev_paper test_jev_perps; do
  .venv/bin/python scripts/$t.py; done
```

## Roadmap

- [x] Jev decision stack: client → questions → state → scorer → risk gates → paper books
- [x] Dual-book paper loop (spot + perps) with full decision logging
- [ ] Multi-pair (BTC, ETH, SOL) + CoinGecko trending pair discovery
- [ ] Telegram decision/trade feed
- [ ] Analysis report: do Jev vetoes beat baseline? calibration check
- [ ] Promotion gate → live (tiny float, exchange-side stops, human-approved)

## Provenance & license

Built on the [Investing Algorithm Framework](https://github.com/mdutr/algorithm-framework) 9.0.0a18 (vendored), forked from [sopersone/cabbage-trading-machine](https://github.com/sopersone/cabbage-trading-machine) (whose engine layer we keep and extend — see `UPSTREAM-README.md`). Apache-2.0 — see `LICENSE` and `AUTHORS.md`.

This is experimental software for research. Not financial advice. Paper results do not guarantee live results.
