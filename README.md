# kalshi-bot

Kalshi event-contract trading system built to `BRIEF.md` under the rules in `CLAUDE.md`.
Ladder arbitrage plus independent pricing models (weather, crypto thresholds, economics), every
order behind one RiskEngine, demo by default, observe-only by default.

## Setup (10 minutes)

```bash
uv sync                                   # Python 3.11+, installs httpx / websockets / cryptography
cp .env.example .env                      # then edit:
#   KALSHI_API_KEY_ID=<key id from https://demo.kalshi.co profile -> API keys>
#   KALSHI_PRIVATE_KEY_PATH=./kalshi-demo.pem   (the private key Kalshi showed you once)
uv run bot doctor                         # probes both Kalshi host families, checks auth + rate limits
uv run bot balance                        # Session-1 gate: demo balance prints
uv run pytest                             # everything passes offline, no credentials needed
```

Production needs both `--live` on the command line and `CONFIRM_LIVE=yes` in `.env`, with a
separate production key. Nothing else switches environments.

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
