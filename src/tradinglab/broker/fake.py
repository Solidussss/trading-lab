"""Deterministic in-memory paper broker for tests and offline dry runs.

Supports injecting failure modes the live engine must survive: rejections, partial fills,
timeouts, and duplicate client order ids.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tradinglab.broker.base import (
    Account,
    MarketClock,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    Side,
)


class BrokerTimeoutError(TimeoutError):
    pass


class BrokerRejectedError(RuntimeError):
    pass


class FakePaperBroker:
    is_paper = True

    def __init__(self, cash: Decimal = Decimal("10000"), *, prices: dict[str, Decimal] | None = None):
        self.cash = cash
        self.last_equity = cash
        self.prices: dict[str, Decimal] = dict(prices or {})
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}  # by client_order_id
        self._ids = itertools.count(1)
        self.trading_days: list[date] = []
        self.clock_open = True
        # failure injection
        self.reject_next: str | None = None
        self.timeout_next = False
        self.timeout_after_accept = False  # order is accepted, but the response is lost
        self.partial_fill_ratio: Decimal | None = None
        self.submit_calls = 0

    # -- reads ---------------------------------------------------------------------------

    def get_account(self) -> Account:
        mv = sum((p.market_value for p in self.positions.values()), Decimal(0))
        equity = self.cash + mv
        return Account("fake-paper", equity, self.cash, self.cash, self.last_equity, False, True)

    def get_clock(self) -> MarketClock:
        now = datetime.now(UTC)
        return MarketClock(now, self.clock_open, now + timedelta(hours=1), now + timedelta(hours=7))

    def get_trading_days(self, start: date, end: date) -> list[date]:
        return [d for d in self.trading_days if start <= d <= end]

    def list_positions(self) -> list[Position]:
        return list(self.positions.values())

    def list_open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if not o.status.is_terminal]

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        return self.orders.get(client_order_id)

    # -- writes --------------------------------------------------------------------------

    def submit_order(self, request: OrderRequest) -> Order:
        self.submit_calls += 1
        if self.timeout_next:
            self.timeout_next = False
            raise BrokerTimeoutError("simulated timeout before acceptance")
        if request.client_order_id in self.orders:
            raise BrokerRejectedError("client_order_id must be unique")
        if self.reject_next:
            reason, self.reject_next = self.reject_next, None
            order = self._new(request, OrderStatus.REJECTED)
            self.orders[request.client_order_id] = order
            raise BrokerRejectedError(reason)

        price = self.prices[request.symbol]
        qty = request.qty
        if self.partial_fill_ratio is not None:
            qty = (request.qty * self.partial_fill_ratio).to_integral_value()
        if request.side == Side.SELL:
            held = self.positions.get(request.symbol)
            if held is None or held.qty < qty:
                raise BrokerRejectedError("insufficient qty (paper accounts here are long-only)")
        status = OrderStatus.FILLED if qty == request.qty else OrderStatus.PARTIALLY_FILLED
        order = replace(
            self._new(request, status), filled_qty=qty, filled_avg_price=price,
            filled_at=datetime.now(UTC),
        )
        self._apply_fill(request.symbol, request.side, qty, price)
        self.orders[request.client_order_id] = order
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise BrokerTimeoutError("simulated timeout after acceptance")
        return order

    def cancel_order(self, order_id: str) -> None:
        for cid, o in self.orders.items():
            if o.id == order_id and not o.status.is_terminal:
                self.orders[cid] = replace(o, status=OrderStatus.CANCELED)

    # -- helpers -------------------------------------------------------------------------

    def _new(self, r: OrderRequest, status: OrderStatus) -> Order:
        return Order(
            id=f"fake-{next(self._ids)}", client_order_id=r.client_order_id, symbol=r.symbol,
            side=r.side, qty=r.qty, filled_qty=Decimal(0), filled_avg_price=None, status=status,
            submitted_at=datetime.now(UTC),
        )

    def _apply_fill(self, symbol: str, side: Side, qty: Decimal, price: Decimal) -> None:
        if qty == 0:
            return
        pos = self.positions.get(symbol)
        if side == Side.BUY:
            self.cash -= qty * price
            if pos:
                new_qty = pos.qty + qty
                avg = (pos.avg_entry_price * pos.qty + price * qty) / new_qty
                self.positions[symbol] = Position(symbol, new_qty, avg, new_qty * price)
            else:
                self.positions[symbol] = Position(symbol, qty, price, qty * price)
        else:
            assert pos is not None
            self.cash += qty * price
            remaining = pos.qty - qty
            if remaining == 0:
                del self.positions[symbol]
            else:
                self.positions[symbol] = Position(
                    symbol, remaining, pos.avg_entry_price, remaining * price
                )
