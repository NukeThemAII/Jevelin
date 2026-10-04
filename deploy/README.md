## jevelin-monitor.service

Paper/monitor mode only. `jevelin_supervisor.py` never imports broker-write
code (confirmed by Rigel's safety-gate audit, War Room 2026-10-04) — there is
no live-mode flag to accidentally flip here.

**Not installed yet.** Gate: do not enable/start until Rigel's signed
checklist and Forge's tuned config both land. Until then, run manually in
the foreground for dry-run checks:

```
.venv/bin/python scripts/jevelin_supervisor.py --once
```

To install once cleared:

```
sudo cp deploy/jevelin-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jevelin-monitor.service
journalctl -u jevelin-monitor -f          # tail logs
tail -f runtime/heartbeat.jsonl           # liveness check
```

Liveness: a `{"ts", "loop", "fast_ticks", "slow_cycles"}` line is appended to
`runtime/heartbeat.jsonl` on every fast tick (default every 5s) and slow
cycle. Staleness past a few multiples of `--fast-interval` means the loop is
stuck or dead — the write itself never raises (caught in
`Supervisor._heartbeat`), so a full disk degrades to "heartbeat stopped
growing," not a crash loop.

Telegram alerting (`scripts/jev_telegram.py`) is wired but **not yet wired
into the supervisor loop** — it's a standalone fail-open sender ready for the
next slice (e.g. alert on stale heartbeat, risk-halt latch, or startup/
shutdown) once `TELEGRAM_CHAT_ID` is captured. Token lives in `.env` only
(gitignored); never committed or logged.

**Security note:** the bot token above was pasted in plaintext in War Room
chat history. Standard hygiene is to rotate it via `@BotFather` once alert
wiring is confirmed working end-to-end.
