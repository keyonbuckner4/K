"""Executor: turns an approved Intent into limit orders on the exchange.

Rules: every intent passes through ``RiskEngine.approve`` first (there is no other entry point to
``client.create_order`` in the engine); orders are marketable limit IOCs so nothing rests
un-managed; multi-leg baskets that only partially fill are unwound immediately with reduce-only
orders; everything is written to SQLite with its reason. In observe mode no order is ever sent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .account import AccountSnapshot
from .alerts import Alerts
from .client import KalshiClient
from .errors import ApiError, Halted, RiskRejected
from .intent import ARB, Intent, Leg
from .models import OrderAck, Position
from .orderbook import OrderBook
from .risk import RiskEngine
from .storage import Storage

log = logging.getLogger(__name__)

OBSERVE = "observe"
TRADE = "trade"


@dataclass
class LegResult:
    leg: Leg
    order_id: str | None
    filled: Decimal
    remaining: Decimal
    avg_price: Decimal | None
    fee: Decimal | None
    error: str | None = None


@dataclass
class ExecutionResult:
    intent: Intent
    mode: str
    status: str                      # observed | rejected | halted | filled | partial | unfilled | unwound | error
    reason: str
    legs: list[LegResult] = field(default_factory=list)
    cost_cents: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"intent_id": self.intent.intent_id, "mode": self.mode, "status": self.status, "reason": self.reason,
                "cost_cents": self.cost_cents,
                "legs": [{"ticker": r.leg.ticker, "order_id": r.order_id, "filled": str(r.filled), "remaining": str(r.remaining),
                          "avg_price": str(r.avg_price) if r.avg_price is not None else None, "fee": str(r.fee) if r.fee is not None else None,
                          "error": r.error} for r in self.legs]}


class Executor:
    def __init__(self, client: KalshiClient, storage: Storage, risk: RiskEngine, *, mode: str = OBSERVE,
                 time_in_force: str = "immediate_or_cancel", stp: str = "taker_at_cross", alerts: Alerts | None = None):
        if mode not in (OBSERVE, TRADE):
            raise ValueError(f"mode must be observe or trade, got {mode!r}")
        self.client = client
        self.storage = storage
        self.risk = risk
        self.mode = mode
        self.tif = time_in_force
        self.stp = stp
        self.alerts = alerts

    # ---- entry point ------------------------------------------------------------------------
    async def execute(self, intent: Intent, snapshot: AccountSnapshot) -> ExecutionResult:
        try:
            approval = self.risk.approve(intent, snapshot)
        except Halted as e:
            self._log(intent, "risk", False, f"halted: {e}")
            return ExecutionResult(intent, self.mode, "halted", str(e))
        except RiskRejected as e:
            self._log(intent, "risk", False, f"risk rejected: {e}")
            return ExecutionResult(intent, self.mode, "rejected", str(e))

        self._log(intent, "risk", True, "risk approved; " + "; ".join(approval.notes) if approval.notes else "risk approved")
        if self.mode == OBSERVE:
            self.storage.save_intent(intent.intent_id, intent.strategy, intent.event_ticker, "observed", self.mode,
                                     intent.expected_edge_cents, intent.max_cost_cents, [l.to_dict() for l in intent.legs])
            self._log(intent, "execute", True, "OBSERVE mode: order not sent")
            return ExecutionResult(intent, self.mode, "observed", "observe mode", cost_cents=approval.max_cost_cents)

        self.storage.save_intent(intent.intent_id, intent.strategy, intent.event_ticker, "placed", self.mode,
                                 intent.expected_edge_cents, intent.max_cost_cents, [l.to_dict() for l in intent.legs])
        results: list[LegResult] = []
        for i, leg in enumerate(intent.legs):
            results.append(await self._place_leg(intent, i, leg))
            if results[-1].error and intent.kind == ARB:
                break  # do not keep building a broken basket

        filled_legs = [r for r in results if r.filled > 0]
        full = [r for r in results if r.remaining == 0 and r.error is None]
        if len(full) == len(intent.legs):
            status, reason = "filled", "all legs filled"
        elif not filled_legs:
            status, reason = "unfilled", "no leg filled"
        elif intent.kind == ARB:
            status, reason = "partial", "basket partially filled; unwinding"
        else:
            status, reason = "partial", "partially filled"

        if status == "partial" and intent.kind == ARB:
            unwound = await self._unwind(intent, filled_legs)
            status = "unwound" if unwound else "partial"
            reason += "; unwound" if unwound else "; UNWIND FAILED - manual attention"
            if not unwound and self.alerts:
                self.alerts.queue("critical", f"arb basket {intent.intent_id} partially filled and could not be unwound", intent.event_ticker)

        cost = sum(int((r.avg_price or r.leg.price) * r.filled * 100) if r.leg.book_side == "bid"
                   else int((Decimal("1") - (r.avg_price or r.leg.price)) * r.filled * 100) for r in results)
        self.storage.update_intent(intent.intent_id, status, {"reason": reason, "legs": ExecutionResult(intent, self.mode, status, reason, results).to_dict()["legs"]})
        self._log(intent, "execute", status in ("filled",), f"{status}: {reason}")
        return ExecutionResult(intent, self.mode, status, reason, results, cost)

    async def _place_leg(self, intent: Intent, index: int, leg: Leg) -> LegResult:
        coid = f"{intent.intent_id}-{index}"
        try:
            ack: OrderAck = await self.client.create_order(ticker=leg.ticker, book_side=leg.book_side, price=leg.price, count=leg.count,
                                                           client_order_id=coid, time_in_force=self.tif, self_trade_prevention_type=self.stp,
                                                           reduce_only=True if leg.reduce_only else None)
        except ApiError as e:
            log.error("order failed for %s: %s", leg.ticker, e)
            self.storage.save_order(f"failed-{coid}", coid, intent.intent_id, leg.ticker, leg.book_side, leg.price, leg.count, self.tif,
                                    "error", raw={"error": str(e), "status": e.status, "body": e.body})
            return LegResult(leg, None, Decimal("0"), Decimal(leg.count), None, None, error=str(e))
        status = "executed" if ack.remaining_count == 0 else ("partial" if ack.fill_count > 0 else "unfilled")
        self.storage.save_order(ack.order_id, ack.client_order_id or coid, intent.intent_id, leg.ticker, leg.book_side, leg.price, leg.count,
                                self.tif, status, ack.fill_count, ack.remaining_count, ack.average_fill_price, ack.average_fee_paid, ack.raw)
        if self.tif != "immediate_or_cancel" and ack.remaining_count > 0:
            # never leave a resting order behind without management: cancel the remainder
            try:
                await self.client.cancel_order(ack.order_id)
                self.storage.update_order(ack.order_id, status="canceled")
            except ApiError as e:
                log.error("cancel of remainder failed for %s: %s", ack.order_id, e)
        return LegResult(leg, ack.order_id, ack.fill_count, ack.remaining_count, ack.average_fill_price, ack.average_fee_paid)

    async def _unwind(self, intent: Intent, filled: list[LegResult]) -> bool:
        """Close what filled with reduce-only IOC limits a few cents through the touch."""
        ok = True
        for r in filled:
            leg = r.leg
            book = await self.client.orderbook(leg.ticker)
            res = await self.close_position_leg(leg.ticker, leg.outcome_side, int(r.filled), book, intent.intent_id, reason="unwind partial basket")
            ok = ok and res is not None and res.remaining == 0
        return ok

    # ---- closing / flattening -------------------------------------------------------------------
    async def close_position_leg(self, ticker: str, outcome_side: str, count: int, book: OrderBook | None, intent_id: str | None,
                                 reason: str, through_cents: int = 3) -> LegResult | None:
        """Sell the side we hold: YES -> ask at (best yes bid - through), NO -> bid at (best yes ask + through)."""
        if count <= 0:
            return None
        if outcome_side == "yes":
            ref = book.best_yes_bid if book else None
            price = (ref - Decimal(through_cents) / 100) if ref is not None else Decimal("0.01")
            side = "ask"
        else:
            ref = book.best_yes_ask if book else None
            price = (ref + Decimal(through_cents) / 100) if ref is not None else Decimal("0.99")
            side = "bid"
        price = min(max(price, Decimal("0.01")), Decimal("0.99"))
        leg = Leg(ticker, side, price, count, reduce_only=True, reason=reason)
        coid = f"close-{intent_id or ticker}-{int(time.time())}"
        try:
            ack = await self.client.create_order(ticker=ticker, book_side=side, price=price, count=count, client_order_id=coid,
                                                 time_in_force="immediate_or_cancel", self_trade_prevention_type=self.stp, reduce_only=True)
        except ApiError as e:
            log.error("close order failed for %s: %s", ticker, e)
            self.storage.log_decision("executor", "close", False, f"close failed: {e}", market_ticker=ticker, book_side=side, price=price, count=count)
            return LegResult(leg, None, Decimal("0"), Decimal(count), None, None, error=str(e))
        self.storage.save_order(ack.order_id, coid, intent_id, ticker, side, price, count, "immediate_or_cancel",
                                "executed" if ack.remaining_count == 0 else "partial", ack.fill_count, ack.remaining_count,
                                ack.average_fill_price, ack.average_fee_paid, ack.raw)
        self.storage.log_decision("executor", "close", ack.remaining_count == 0, f"{reason}: filled {ack.fill_count}/{count}",
                                  market_ticker=ticker, book_side=side, price=price, count=count)
        return LegResult(leg, ack.order_id, ack.fill_count, ack.remaining_count, ack.average_fill_price, ack.average_fee_paid)

    async def flatten_all(self, positions: list[Position], reason: str) -> list[LegResult]:
        """Cancel every resting order and close every open position. Used by loss halts."""
        out: list[LegResult] = []
        if self.mode == OBSERVE:
            self.storage.log_decision("executor", "flatten", False, f"OBSERVE mode: would flatten {len(positions)} positions ({reason})")
            return out
        try:
            await self.client.cancel_all_orders()
        except ApiError as e:
            log.error("cancel_all failed: %s", e)
        for p in positions:
            if p.position == 0:
                continue
            book = None
            try:
                book = await self.client.orderbook(p.ticker)
            except ApiError as e:
                log.error("orderbook fetch failed for %s during flatten: %s", p.ticker, e)
            r = await self.close_position_leg(p.ticker, "yes" if p.position > 0 else "no", int(abs(p.position)), book,
                                              self.storage.intent_for_market(p.ticker), reason)
            if r:
                out.append(r)
                if r.remaining == 0:
                    self.storage.close_intents_for_market(p.ticker)
        if self.alerts:
            self.alerts.queue("critical", f"flattened {len(out)} positions: {reason}")
        return out

    def _log(self, intent: Intent, stage: str, accepted: bool, reason: str) -> None:
        for leg in intent.legs:
            self.storage.log_decision(intent.strategy, stage, accepted, reason, event_ticker=intent.event_ticker, market_ticker=leg.ticker,
                                      book_side=leg.book_side, price=leg.price, count=leg.count, model_prob=leg.model_prob,
                                      fee_cents=leg.fee_cents_per_contract, edge_net_cents=intent.expected_edge_cents,
                                      details={"intent_id": intent.intent_id, "kind": intent.kind, "mode": self.mode})
