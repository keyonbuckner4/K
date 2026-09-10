import asyncio
from decimal import Decimal

import httpx

from kalshi_bot.account import realized_net_cents
from kalshi_bot.alerts import Alerts
from kalshi_bot.models import Position


def test_realized_net_subtracts_fees():
    ps = [Position.parse({"ticker": "A", "position_fp": "0", "realized_pnl_dollars": "1.50", "fees_paid_dollars": "0.10"}),
          Position.parse({"ticker": "B", "position_fp": "0", "realized_pnl_dollars": "-0.75", "fees_paid_dollars": "0.05"})]
    assert realized_net_cents(ps) == 60


def test_alerts_filter_dedupe_and_post():
    posted = []

    def handler(req: httpx.Request):
        posted.append(req.content)
        return httpx.Response(200)

    a = Alerts("https://hook.test/x", "warning", transport=httpx.MockTransport(handler))

    async def go():
        a.queue("info", "ignored")
        a.queue("warning", "spread wide")
        a.queue("warning", "spread wide")  # deduped within 60s
        await a.flush()
        assert len(a.sent) == 1 and len(posted) == 1 and b"spread wide" in posted[0]
        await a.close()

    asyncio.run(go())
