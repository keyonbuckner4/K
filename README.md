# kalshi-bot

Kalshi event-contract trading system built to `BRIEF.md` under the rules in `CLAUDE.md`.
Ladder arbitrage plus independent pricing models (weather, crypto thresholds, economics), every
order behind one RiskEngine, demo by default, observe-only by default.

## Setup (10 minutes)

```bash
uv sync                                   # Python 3.11+, installs httpx / websockets / cryptography
uv run bot setup                          # asks for your key IDs, saves the keys, writes .env, checks the connection
uv run bot balance                        # Session-1 gate: demo balance prints
uv run pytest                             # everything passes offline, no credentials needed
```

`bot setup` is the guided path. The manual equivalent is `cp .env.example .env` and filling in
`KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` for demo and `KALSHI_LIVE_API_KEY_ID` /
`KALSHI_LIVE_PRIVATE_KEY_PATH` for production, then `uv run bot doctor`.

Both keys sit in `.env` side by side. Demo commands read only the demo key. Production needs
`--live` on the command line and `CONFIRM_LIVE=yes` in `.env`, and then reads only the
`KALSHI_LIVE_*` key; it refuses to start if that key is missing or is the same key as demo.
Save private keys as `*.pem` files so `.gitignore` covers them.

## Commands

| Command | What it does |
|---|---|
| `bot doctor` | Probe every known REST host, signed balance call, `GET /account/limits`. |
| `bot balance` / `bot positions` / `bot orders` | Account reads. |
| `bot markets --series KXHIGHNY` / `bot events --series ...` / `bot book TICKER` / `bot series KXHIGHNY` | Market data, including a series' fee parameters. |
| `bot watch TICKER... --minutes 10` | WebSocket book reconstruction vs REST (Session-2 gate). |
| `bot scan [-v]` | One observe-only pass of every enabled strategy; logs every decision with its reason. |
| `bot run [--dashboard] [--trade]` | Continuous loop. Orders are sent only with `--trade` **and** `mode = "trade"` on the strategy in `config/bot.toml`. |
| `bot gaps` / `bot decisions` / `bot status` | The evidence: detected ladder gaps, the decision log, risk state and halts. |
| `bot backtest` | Scores logged model probabilities against settlements (Brier vs market, calibration, pessimistic P&L) and prints what would make it wrong. |
| `bot review` | Weekly calibration review. Proposes config diffs; never applies them. |
| `bot halt` / `bot resume [--file --daily --weekly --full-stop]` | Kill switch and manual restarts. |
| `bot flatten --yes` / `bot cancel-all --yes` | Emergency close-out with reduce-only limit orders. |
| `bot dashboard` | Local status page with a HALT button. |

## Safety model

* **One chokepoint.** `RiskEngine.approve` is the only path to `client.create_order`. It checks the
  HALT file (the only place that file is read), exchange status, the 1% per-position cap (and
  the $25 cap for the first 30 live days), no leverage, the 10-minute settlement window, blocked
  categories (sports, politics, culture), 5 concurrent positions, 2 per event, and limit prices.
* **Loss halts persist.** Daily (3%) and weekly (6%) realized-loss halts and the 10% drawdown
  full stop live in SQLite with day/week keys in America/New_York. A restart does not reset them.
  Daily halts release the next trading day; weekly and full-stop need `bot resume --weekly` /
  `--full-stop`.
* **Limit orders only.** The client cannot express a market order. Entries are marketable IOC
  limits so nothing rests un-managed; partial arb baskets are unwound immediately with
  reduce-only orders.
* **No placeholders.** Missing fee parameters, forecasts, spot or vol data reject the decision
  with a logged reason. An undocumented API shape stops the bot (`UnexpectedApiResponse`).
* **Fees first.** Every edge subtracts Kalshi's fee: `round_up_to_cent(multiplier * C * P * (1-P))`
  per order, with the multiplier read from `GET /series/{ticker}` (`fee_type`, `fee_multiplier`).

## Strategies (build order from the BRIEF)

1. **Ladder arbitrage** (`ladder_arb`): exhaustive mutually-exclusive ladders whose YES asks sum
   below $1 (buy all) or YES bids sum above $1 (sell all), nested-threshold monotonicity breaks,
   crossed books. Every gap is logged to `arb_gaps` with its net edge, tradeable or not.
2. **Weather** (`weather`): daily high/low ladders priced from NWS grid forecasts with a
   lead-time-dependent error sigma. Verify each city's station against the market's
   `rules_primary` (logged with every decision) before enabling `trade`.
3. **Crypto thresholds** (`crypto`): BTC/ETH levels priced as barrier options from Kraken spot and
   realized vol (or Deribit DVOL).
4. **Economics** (`economics`): normal consensus views you write in `config/econ_views.toml`.

All strategies start in `mode = "observe"`. Flip one to `trade` in `config/bot.toml` only after
its observe-only gate in the BRIEF is met.

## What was verified and what was not

Verified from Kalshi's published SDKs and independent clients: RSA-PSS signing (timestamp ms +
method + path without query string, SHA-256, salt = digest length), the three header names, the
V2 order endpoint `POST /portfolio/events/orders` with YES-referenced `side: bid|ask`, fixed-point
`price`/`count` strings, required `time_in_force` and `self_trade_prevention_type`, the
`DELETE /portfolio/events/orders/{id}` cancel, `GET /account/limits` token buckets, the
`orderbook_fp` book shape, WebSocket `orderbook_snapshot`/`orderbook_delta` fields, and the series
`fee_type`/`fee_multiplier` fields.

Not verified from inside the build environment (Kalshi hosts were unreachable there): which host
family answers today. Both are in `config/bot.toml`; `bot doctor` tells you in seconds. Also
unverified: whether Kalshi's `realized_pnl` already nets fees (the bot subtracts fees again,
which only makes loss halts fire earlier), and the exact NWS station behind each temperature
series (check `rules_primary`).

## Layout

```
src/kalshi_bot/
  auth.py ratelimit.py client.py ws.py orderbook.py models.py fees.py   # exchange access
  risk.py gate.py execution.py account.py intent.py halt.py            # safety and execution
  strategies/{ladder_arb,weather,crypto,economics}.py data/{nws,crypto_feed}.py pricing.py
  engine.py cli.py storage.py backtest.py review.py dashboard.py alerts.py
tests/                                                                 # offline, no network
config/bot.toml                                                        # non-secret config
```

## Running it unattended on Windows

`scripts\install-windows-task.ps1` registers a Scheduled Task that starts `bot run --dashboard` at logon and
restarts it a minute after any exit. Run it once from the repo folder
(`powershell -ExecutionPolicy Bypass -File .\scripts\install-windows-task.ps1` if scripts are disabled).

To pick up new code, or to restart for any reason, use the restart script rather than stopping and starting the
task by hand: `.\scripts\restart-windows-task.ps1 -Pull` (pull + `uv sync` + restart; drop `-Pull` to restart only).
A hand-typed `Stop-ScheduledTask; Start-ScheduledTask` can race: if the old process is still shutting down the
task ignores the start and nothing runs. The script waits for the old process to be gone, kills any leftover from
an older install, starts the task, waits until the dashboard answers, and prints the task state, the bot's phase
and the last log lines.

The dashboard (http://127.0.0.1:8787) is served from the moment the process holds its lock, before the exchange
handshake, and its first line is the bot's own status: `starting`, `running` with the time of the last scan, or
`startup failed` with the error. "Connection refused" therefore means no bot process is running: check
`(Get-ScheduledTask -TaskName kalshi-bot).State` and run the restart script. Every exit reason (config error,
undocumented API response, crash with traceback) is written to `data\logs\bot.demo.log`, so
`Get-Content data\logs\bot.demo.log -Tail 20` explains any stop. The bot holds a lock per environment so a second
copy refuses to start; stop an interactive run (Ctrl-C) before installing the task. The PC still has to be on and
logged in; for true 24/7 use a small always-on server (roadmap item 5).

## Decision log volume

Every scan prices every open market (about 1,400). A row per market per scan was 2-3 million rows a day,
and the scorecard, which read only the newest 100,000 rows, stopped seeing settled markets after the first
hours. Now the scorecard resolves "latest model view per market" in SQL with no row cap, and per-market model
rows and quotes are written only when they change to the cent or every `log_heartbeat_sec` (30 min). Gate,
execute, order and fill rows are never throttled. Rows older than `retention_days_decisions` /
`retention_days_quotes` are pruned hourly. A database written before this change can be shrunk once with
`uv run bot compact` while the bot is stopped (it keeps one model row and one quote per market per 30 minutes). A failed hourly scoring shows in red on the dashboard and in the
log as `model scoring failed` with a traceback.

## Roadmap (agreed with the operator)

1. **Observe period, in progress.** `bot run --dashboard` started on demo on 2026-09-11 00:53 UTC with all
   strategies in `observe`, reading production market data. Gates from the BRIEF: 48 h of ladder-gap logs
   before `ladder_arb` may trade, 24 h of weather decisions before `weather` may.
2. **Backtest review.** After the first settlements, `bot backtest` (Brier vs market, calibration, pessimistic
   P&L) decides which strategy, if any, is switched to `trade` on demo. Caveats are printed first, by design.
3. **Position manager (approved, build after step 2).** Take-profit / edge-gone / time-based exits for open
   positions, using the existing reduce-only close path through the RiskEngine. Exit rules must compare the
   locked-in value after a second taker fee against the model's expected value of holding. Ships observe-first
   ("would sell" logged), then switched on in config, with tests.
4. **Edge-threshold sweep (agreed).** After the observe period, score the logged decisions as if the gate's
   minimum net edge had been 3c and 4c instead of 5c: trades per week and pessimistic P&L per threshold, from the
   same settlements. The operator decides whether to lower `[gate] min_net_edge_cents` in BRIEF.md/config for
   faster capital turnover. A trade quota is explicitly not on the table; frequency must come from edges.
5. **24/7 hosting.** Move the bot to a small always-on Linux server before any live trading; one-paste installer.
6. **Housekeeping.** Read-only market commands (`markets`, `events`, `book`) should read from the market-data
   venue; set `strategies.weather.nws_user_agent` to a real contact.
