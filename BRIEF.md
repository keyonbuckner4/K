# BRIEF.md — Kalshi Event Contract Trading System

## Mission
Trade binary event contracts on Kalshi using models that price events independently of the
market, plus mechanical arbitrage where the order book is internally inconsistent. Capital is
limited to funds in this Kalshi account and nothing else.

## Non-goals
- No leverage, no margin, no borrowed funds.
- No trading a market I cannot price independently or arb mechanically.
- No autonomous change to risk limits. Limits change only when I edit this file.
- No sports, politics, or culture markets in v1.

## Stack
- Python 3.11+, uv for dependency management.
- Kalshi REST + WebSocket. Auth is RSA-PSS request signing with headers
  KALSHI-ACCESS-KEY (key ID), KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE.
  There is no bearer token. The signed path excludes the query string.
- Confirm the current base URLs and demo host against docs.kalshi.com before writing the
  client. Do not assume a URL from memory or from a blog post.
- SQLite for storage. TOML for config. Secrets in .env only.
- Every module runs standalone from the CLI. No framework.

## Environments
- Default everywhere: demo. Production requires --live AND CONFIRM_LIVE=yes.
- First 30 days live: max $25 per position regardless of what the risk math allows.

## Rate limiting (Kalshi meters reads and writes in separate token buckets)
- ONE shared limiter and request queue in front of the account. Not one per worker.
- Reserve write budget for cancels and risk actions. Background polling never consumes
  the last of the write bucket.
- Kalshi returns HTTP 429 with no Retry-After header. Use bounded backoff with jitter.
- Poll GET /account/limits at startup to read actual live budgets rather than hard-coding.
- Cache slow-changing REST data. Live book and fills come over WebSocket, not polling.

## Risk limits (hard-coded, enforced in one chokepoint, not per-strategy)
- 1% of account equity max per position.
- 3% max daily realized loss → flat and halt until next day.
- 6% max weekly realized loss → halt until I re-enable.
- 10% peak-to-trough drawdown → full stop, manual restart required.
- Max 5 concurrent open positions; max 2 correlated to the same underlying event.
- No entry inside the final 10 minutes before settlement unless the strategy is
  explicitly a settlement strategy.
- Limit orders only. Market orders are forbidden.
- A HALT file in the repo root blocks all order placement.
- Daily and weekly loss counters persist to disk. A restart does not reset them.

## Fees — model these before anything else
Kalshi's fee scales with contract price and is largest near 50 cents, which is exactly where
most modelable edges sit. Pull the current fee schedule from the docs, implement it as a
function, and subtract it in every edge calculation and every backtest. An edge that is
positive gross and negative net does not exist.

## Entry gate (every strategy passes all four)
- edge = model_prob - ask_price, in cents. Minimum net edge after fees: 5 cents.
- Reject if bid/ask spread > 3 cents.
- Reject if resting size at my price < 2x my intended size.
- Reject if the market settles in under 10 minutes.

## Strategies, in build order
1. **Ladder arbitrage** (no forecast required). Within a strike ladder, mutually exclusive
   YES prices should sum to ~100. YES + NO on one contract should not sum below 100.
   When they do, that is a mechanical edge. Build this first — it validates the whole
   execution stack without needing a model to be right.
2. **Weather.** Daily high/low temp, rain, snow by city. Model input is NOAA/NWS
   probabilistic forecasts. Objective settlement, recurs daily, cleanest real edge.
3. **Crypto thresholds.** BTC/ETH above X at a given hour. Price as a barrier option from
   spot and implied vol.
4. **Economics.** CPI, Fed, jobless claims, priced off consensus distributions. Low
   frequency — it supplements, it does not carry the bot.

## Definition of done for any phase
1. Runs against demo for the stated duration with zero unhandled exceptions.
2. Tests pass offline, with no network and no real credentials.
3. Every decision is logged with its reason, including rejections.
4. I have read the diff and approved it.
