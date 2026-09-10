"""A cached view of the account (balance, positions, realized P&L, resting orders) for risk checks."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from .client import KalshiClient
from .models import Balance, ExchangeStatus, Order, Position, to_cents

log = logging.getLogger(__name__)


@dataclass
class AccountSnapshot:
    ts: float
    balance: Balance
    positions: list[Position]                 # nonzero, unsettled
    realized_cents: int                       # lifetime realized P&L net of fees (conservative)
    resting_orders: list[Order] = field(default_factory=list)
    exchange: ExchangeStatus | None = None

    @property
    def equity_cents(self) -> int:
        return self.balance.equity_cents

    @property
    def balance_cents(self) -> int:
        return self.balance.balance_cents

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.position != 0]


def realized_net_cents(all_positions: list[Position]) -> int:
    """Sum realized_pnl minus fees_paid over every market ever traded.

    Whether Kalshi's realized_pnl already nets fees is not documented; subtracting fees again is
    the conservative reading (a loss halt fires slightly early, never late)."""
    total = Decimal("0")
    for p in all_positions:
        total += (p.realized_pnl or Decimal("0")) - (p.fees_paid or Decimal("0"))
    return to_cents(total) or 0


class AccountView:
    def __init__(self, client: KalshiClient, ttl_seconds: float = 5.0):
        self.client = client
        self.ttl = ttl_seconds
        self.snapshot: AccountSnapshot | None = None

    async def refresh(self, force: bool = False, include_orders: bool = True) -> AccountSnapshot:
        if not force and self.snapshot and time.time() - self.snapshot.ts < self.ttl:
            return self.snapshot
        balance = await self.client.balance()
        exchange = await self.client.exchange_status()
        all_positions = await self.client.positions(settlement_status="all", count_filter=None)
        open_positions = [p for p in all_positions if p.position != 0]
        resting = await self.client.orders(status="resting") if include_orders else []
        self.snapshot = AccountSnapshot(ts=time.time(), balance=balance, positions=open_positions,
                                        realized_cents=realized_net_cents(all_positions), resting_orders=resting, exchange=exchange)
        return self.snapshot
