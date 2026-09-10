# Operating rules

- Read BRIEF.md before every task. It is the spec. This file is how you behave.
- Demo is the default environment. Live requires the --live flag AND CONFIRM_LIVE=yes.
- No order is placed in any code path that has not passed through RiskEngine.
- Never commit .env, *.pem, *.key, or data/. Check .gitignore before any commit.
- No new dependency without asking me first.
- No mock or placeholder data in any path that can reach a live order. Fail loudly instead.
- If the API returns something the docs don't describe, stop and report it. Do not guess.
- When a backtest looks good, tell me what would make it wrong before you tell me it's good.
- One phase per session. Do not start the next phase without my sign-off.
- Kalshi only. No other venue, no other asset class.
