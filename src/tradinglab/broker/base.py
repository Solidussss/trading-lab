"""Broker-agnostic domain types and the Broker protocol.

The engine depends only on this interface. Two implementations exist:
  * AlpacaPaperBroker - wraps alpaca-py, paper endpoint only (tradinglab.broker.alpaca_paper)
  * FakePaperBroker   - deterministic in-memory simulator for tests and dry runs

Long-only is enforced here at the type level: there is no "sell short" side, and the
order gate rejects sells larger than the held quantity.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tradinglab.safety.order_gate import GateDecision


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"  # closes/reduces a long position only


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    DAY = "day"
    OPG = "opg"  # market/limit-on-open auction


class OrderStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PENDING_NEW = "pending_new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }


_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


def make_client_order_id(
    strategy_version: str, symbol: str, session_date: date, action: str
) -> str:
    """Deterministic, idempotent client order id.

    The same strategy version, symbol, session and action always produce the same id, so
    a retried or duplicated evaluation run cannot place a second order: the broker rejects
    duplicate client_order_ids, and the engine checks for an existing one before sending.
    """
    raw = f"{strategy_version}|{symbol}|{session_date.isoformat()}|{action}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"tl-{session_date:%Y%m%d}-{digest}"


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: Decimal
    client_order_id: str
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0 or self.qty != self.qty.to_integral_value():
            raise ValueError("qty must be a positive whole number of shares.")
        if not _CLIENT_ID_RE.match(self.client_order_id):
            raise ValueError("client_order_id has an invalid format.")
        if (self.order_type == OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit_price is required for, and only for, limit orders.")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive.")


@dataclass(frozen=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    status: OrderStatus
    submitted_at: datetime | None = None
    filled_at: datetime | None = None


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class Account:
    account_id: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    last_equity: Decimal  # equity at previous close (for intraday loss checks)
    trading_blocked: bool
    is_paper: bool = field(default=True)


@dataclass(frozen=True)
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@runtime_checkable
class Broker(Protocol):
    """Everything the engine is allowed to ask of a broker."""

    is_paper: bool

    def get_account(self) -> Account: ...

    def get_clock(self) -> MarketClock: ...

    def get_trading_days(self, start: date, end: date) -> list[date]: ...

    def list_positions(self) -> list[Position]: ...

    def list_open_orders(self) -> list[Order]: ...

    def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...

    def submit_order(self, request: OrderRequest, decision: GateDecision) -> Order:
        """Must call safety.order_gate.require_gate_approval(request, decision) first."""
        ...

    def cancel_order(self, order_id: str) -> None: ...
