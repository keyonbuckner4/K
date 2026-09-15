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

## Risk limits

Set in `[risk]` in `config/bot.toml`; the defaults are the BRIEF.md numbers. Every fraction is of
account equity, so `0.01` is 1%.

| Limit | Default | On a $100 account |
|---|---|---|
| `max_position_fraction` | 1% | $1.00 per position |
| `max_daily_loss_fraction` | 3% | $3.00, then halt until the next trading day |
| `max_weekly_loss_fraction` | 6% | $6.00, then halt until `bot resume --weekly` |
| `max_drawdown_fraction` | 10% | $10.00 peak-to-trough, then a permanent full stop |

These form a ladder, and the bot refuses to start if it is incoherent. An event contract is binary:
a losing YES contract settles at zero, so a losing position loses **all** of its cost. "One position
fully lost" is therefore the ordinary outcome of a bad trade, not a tail case. So
`max_position_fraction` has to stay below both `max_daily_loss_fraction` and
`max_drawdown_fraction`, or the first losing trade trips a halt. With a 15% position against the
10% full stop, the bot would place one trade, lose it, and stop permanently.

Raising the position size does not create edge. It multiplies whatever edge exists, including a
negative one, so it belongs after the scorecard is green, not before. The lever that raises profit
per trade without raising risk of ruin is capital: 1% of $1,000 is ten times the position of 1% of
$100 at exactly the same percentage risk.

## Strategies (build order from the BRIEF)

1. **Ladder arbitrage** (`ladder_arb`): exhaustive mutually-exclusive ladders whose YES asks sum
   below $1 (or bids above $1) after fees, threshold-ladder monotonicity, crossed books. Every gap
   is logged to `arb_gaps` even when it does not clear fees.
2. **Weather** (`weather`): daily high/low ladders for seven cities. Before the day starts the
   settlement value is Normal(NWS point forecast, sigma) with sigma growing with lead time. Once
   the day is under way the model is bounded by the station's own observations (a high cannot end
   below what has already been measured), forecasts only the remaining hours from the NWS hourly
   grid, and shrinks sigma as those hours run out. Trades on the current day are only proposed
   before `today_candidates_until_local_hour` (11 AM local): after that the market's live read of
   the day beats a forecast, so the model keeps pricing for the scorecard but proposes nothing.
   This replaced the first version after its scorecard showed every afternoon "edge" was the model
   not knowing what the market already knew.
3. **Crypto thresholds** (`crypto`): BTC/ETH levels priced as barrier options from Kraken spot.
   The volatility input is matched to the horizon, because a 15-minute ladder and a daily one share
   an underlying but not a horizon: below `short_horizon_hours` the model uses realized volatility
   over a short window of fine-grained candles, above it Deribit's DVOL 30-day implied index
   (realized only as a logged fallback). Using DVOL on an intraday market would misprice every one.
   Intraday and hourly ladders are adopted by ticker prefix through discovery.
4. **Economics** (`economics`): normal consensus views you write in `config/econ_views.toml`. There
   is no free live feed for an economic release, so it prices only events you have a view for. With
   discovery on it still finds every open economics event and logs each by name as "no consensus
   view configured", which is the list worth writing views for. Arbitrage covers the same events
   structurally in the meantime.

Every directional candidate also passes the gate's fifth rule: it must be priced strictly inside
5c-95c. The bot never bets against a market that is already near certain; the first observe
period showed those "edges" were model error, and such a market is right about 97% of the time.

The observe-first rule applies to every strategy and every model change: it runs in `observe`
mode until the scorecard (`bot backtest`, the dashboard) shows the model beating the market's
prices on at least 20 contested settlements spread over several days, and the trade-time
comparison ("at the moment it would have traded") agrees. Only then does that one strategy's
`mode` switch to `trade`.

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

## Finding markets: `bot discover`

Series tickers cannot be guessed. `KXBTCD` is the daily Bitcoin ladder, but the name of the
15-minute one, the oil one, or a given economic release is not something to invent, and the
operating rules say the bot does not guess at the API. So it asks the exchange:

```
uv run bot discover                     # every open series, grouped
uv run bot discover --category oil      # by category, ticker or title text
uv run bot discover --max-minutes 30    # only the intraday ladders
uv run bot discover --unconfigured      # only what no strategy is pointed at yet
```

Each row gives the series ticker, its horizon (intraday, hourly, daily, multi-day) worked out from
when its markets actually close, the market count, the category, and which strategy already uses it.

Strategies then adopt what matches them, refreshed hourly, so a newly listed ladder is traded
without anyone editing a config file:

- **By ticker prefix**, where the underlying is known. `series_patterns = { BTC = ["KXBTC"], ETH =
  ["KXETH"] }` under `[strategies.crypto]` picks up every Bitcoin and Ethereum ladder, intraday and
  daily alike.
- **By event category**, where the tickers are unknowable in advance. `categories = ["commodit",
  "energy", "oil", "econom", ...]` under `[strategies.ladder_arb]` is how oil and the economic
  releases get covered: arbitrage needs no model, no forecast and no price feed, because a ladder
  whose YES asks sum below a dollar after fees is mispriced whatever the underlying is.

Blocked categories (sport, politics) are filtered out before any strategy sees them, and
`max_discovered_series` caps how many are adopted, keeping the busiest.

## Evidence without waiting: `bot backfill`

Forward observation is limited by how fast markets settle. The daily crypto ladders settle once a
day, so a verdict needing 20 contested settlements spread over several days takes about a week.

`uv run bot backfill --days 30` gets the same evidence from history. For every already-settled
market it replays the crypto model at each point in that market's life using only what existed at
that moment (Kraken spot, Deribit DVOL, and Kalshi's own candlesticks for what the market was
charging), applies the same gate, and scores the result with exactly the live verdict logic. It
writes to its own `data/backfill.<env>.db`, so it cannot contaminate the live scorecard.

What it can and cannot settle:

- It **can** say whether the model's probabilities beat the prices the market was charging, over
  hundreds of settled markets, in minutes rather than a week.
- It **cannot** say whether the orders would have filled. Candlesticks carry no order book, so
  depth and queue position are unknown and the P&L is optimistic even after the one-tick penalty.
  A green backfill is necessary evidence, not sufficient.
- It **cannot** cover the weather model. That needs the NWS forecast as it stood at decision time,
  and the public API serves only the current forecast. Weather has to be observed forward.

Nothing is interpolated: a hole in the spot or volatility history becomes a skipped decision point
with a counted reason, never an invented price. Candlestick price units are decided once per market
from the whole response (explicit `_dollars` keys or any non-integer value mean dollars, otherwise
integer cents), and an unrecognised shape raises instead of being guessed at.

`uv run bot gaps --summary` answers the other evidence question in one line: whether ladder
arbitrage found gaps that cleared the fee threshold. That strategy needs no forecast to be right,
so its bar is only whether the gaps existed.

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

1. **Observe period 1, done (2026-09-11 to 2026-09-12).** Scorecard: 3,044 markets scored, 93 contested,
   verdict "model does NOT beat the market" (Brier 0.1216 vs 0.0917), 32 would-be trades with 3 winners,
   pessimistic P&L −$4.63. Cause: the weather model only knew the morning forecast while the market watched
   the real temperature; the crypto model bet against near-certain strikes.
2. **Model rewrite, built 2026-09-14 (this phase).** Weather: observation floor/cap, remaining-hours forecast,
   morning trading window. Crypto: DVOL implied volatility. Gate: no bets against near-certain markets.
   Scorecard: per-strategy table and the trade-time Brier comparison. Engine: watchdog restart on a hang.
3. **Market breadth, built 2026-09-15.** Series discovery plus `bot discover`; intraday crypto by
   ticker prefix with horizon-matched volatility; oil and economics reached by category through
   ladder arbitrage, which needs no model. Weather unchanged.
4. **Backfill, built 2026-09-15.** `bot backfill` replays the crypto model over already-settled
   markets so its verdict does not have to wait for new ones; `bot gaps --summary` gives the
   arbitrage verdict directly. Neither can shortcut execution risk, which still needs a live run.
5. **Observe period 2, in progress.** Same scorecard, same bar: verdict green on at least 20 contested
   settlements over several days AND "model knew better" at trade time, per strategy. Expect a week.
6. **Position manager (approved, after step 5).** Take-profit / edge-gone / time-based exits for open
   positions through the same reduce-only path, observe-first.
7. **Edge-threshold sweep.** Only once a strategy's verdict is green: score the logged decisions as if the
   gate's minimum net edge were 3c or 4c instead of 5c, and report trades per week and pessimistic P&L per
   threshold. A lower threshold on a losing model only multiplies losing trades.
8. **24/7 hosting.** Move the bot to a small always-on Linux server before any live trading; one-paste installer.
9. **Housekeeping.** Read-only market commands (`markets`, `events`, `book`) should read from the market-data
   venue; set `strategies.weather.nws_user_agent` to a real contact. Directional oil pricing needs a WTI spot
   and volatility feed; none is verified yet, so oil is arbitrage-only for now.
