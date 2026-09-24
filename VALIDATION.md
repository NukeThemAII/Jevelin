# CABBAGE validation — 2026-09-24

Executed on Linux / Python 3.12, with no real exchange credentials.

## Passed

- Installation using the actual `requirements-cabbage.txt` (editable original framework).
- `pip check`: no broken dependencies.
- Configuration command: actual strategy creation and imports.
- 12 original tests in `tests.app.test_paper_trading`.
- 6 CABBAGE integration tests:
  - Original confluence rules generate long entry and exit signals.
  - Configured market/pair, fixed position size and stop survive initialization.
  - Explicit paper mode cannot silently be overridden to live via upstream env flags.
  - Live mode rejects absent credentials.
  - Live configuration initializes the original CCXT executor (without submitting orders).
  - Full original paper execution path: BUY placed, BUY filled, SELL placed,
    SELL filled, portfolio/trade accounting and JSON report persisted. Market
    quotes and signal timing are fixtures; execution/accounting are not mocked.
- Original event-driven backtest on the supplied historical BTC/EUR fixture,
  June 10–20, 2024, with native artifacts and HTML report creation. The configured
  RSI/EMA strategy produced **zero trades** in this interval; no profit is claimed.
- All 394 files in the original framework package matched upstream byte-for-byte.

## Not verified / unavailable

- Public BITVAVO API access from this environment failed at `/v2/assets`.
  Consequently no online paper session or live order was executed here.
- Windows/macOS launchers were not executed on those operating systems.
- Full original test suite (beyond the 12 named tests) was not run.
- Real exchange order minimums, credentials, account permissions, balances,
  execution price and pending-order behaviour need account-specific checks.
- Jev and Robinhood Chain integrations do not exist in the supplied upstream.

Upstream commit: fff7436f12ea95a2e5f794ce6800663fc21ef8ec.
The tests above establish framework integration; they do not establish trading
profitability or production readiness for the promotional strategy.
