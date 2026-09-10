"""What a strategy asks for. Strategies never talk to the exchange; they emit Intents, the entry
gate and RiskEngine vet them, and the Executor places the surviving legs as limit orders."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import Market

ARB = "arb"
DIRECTIONAL = "directional"
CLOSE = "close"


@dataclass
class Leg:
    ticker: str
    book_side: str          # bid = buy YES (sell NO); ask = sell YES (buy NO). Price is always the YES price.
    price: Decimal          # limit price in dollars, YES-referenced
    count: int
    market: Market | None = None
    model_prob: Decimal | None = None   # strategy's P(YES); None for mechanical arbs
    fee_cents_per_contract: Decimal = Decimal("0")
    reason: str = ""
    reduce_only: bool = False

    @property
    def cost_cents(self) -> int:
        """Collateral required: buying YES costs price, buying NO (ask) costs 1 - price. Closing costs nothing."""
        if self.reduce_only:
            return 0
        per = self.price if self.book_side == "bid" else (Decimal("1") - self.price)
        return int((per * self.count * 100).to_integral_value(rounding="ROUND_CEILING"))

    @property
    def outcome_side(self) -> str:
        return "yes" if self.book_side == "bid" else "no"

    @property
    def outcome_price(self) -> Decimal:
        """Price paid per contract of the outcome actually bought (NO price for ask legs)."""
        return self.price if self.book_side == "bid" else (Decimal("1") - self.price)

    def to_dict(self) -> dict[str, Any]:
        return {"ticker": self.ticker, "book_side": self.book_side, "price": str(self.price), "count": self.count,
                "model_prob": str(self.model_prob) if self.model_prob is not None else None,
                "fee_cents_per_contract": str(self.fee_cents_per_contract), "reason": self.reason, "reduce_only": self.reduce_only}


@dataclass
class Intent:
    strategy: str
    event_ticker: str
    legs: list[Leg]
    kind: str = DIRECTIONAL
    expected_edge_cents: Decimal = Decimal("0")   # net of fees, per contract (basket for arbs)
    reason: str = ""
    settlement_strategy: bool = False
    category: str | None = None
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    @property
    def reduce_only(self) -> bool:
        return bool(self.legs) and all(l.reduce_only for l in self.legs)

    @property
    def max_cost_cents(self) -> int:
        return sum(l.cost_cents for l in self.legs)

    @property
    def tickers(self) -> list[str]:
        return [l.ticker for l in self.legs]

    def to_dict(self) -> dict[str, Any]:
        return {"intent_id": self.intent_id, "strategy": self.strategy, "event_ticker": self.event_ticker, "kind": self.kind,
                "expected_edge_cents": str(self.expected_edge_cents), "reason": self.reason, "max_cost_cents": self.max_cost_cents,
                "settlement_strategy": self.settlement_strategy, "category": self.category, "legs": [l.to_dict() for l in self.legs]}
