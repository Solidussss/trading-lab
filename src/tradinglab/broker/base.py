"""Brokerage integration interface - FUTURE USE ONLY, deliberately unimplemented.

Trading Lab V1 trades exclusively in its own simulator (tradinglab.sim). No module in this
repository connects to a brokerage, and the egress guard, configuration checks and static
tests all enforce that (see docs/SAFETY.md).

This file defines the shape a brokerage adapter would need if one is ever added, so the
rest of the system stays modular:

  * The simulator already produces orders with deterministic ids (make_client_order_id),
    so a future adapter could mirror them idempotently.
  * Fills would flow back as Order updates and be reconciled against the simulated ledger.

Adding an implementation is a deliberate project of its own: it requires removing the
brokerage denylist entry for that venue, new safety tests, and your explicit approval.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"  # closes/reduces a long position only


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"  # protective sell stop


class TimeInForce(StrEnum):
    DAY = "day"
    OPG = "opg"  # on-open auction
    GTC = "gtc"


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
                        OrderStatus.REJECTED}


_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


def make_client_order_id(strategy_version: str, symbol: str, session_date: date,
                         action: str) -> str:
    """Deterministic, idempotent order id: same inputs always produce the same id."""
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
    stop_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0 or self.qty != self.qty.to_integral_value():
            raise ValueError("qty must be a positive whole number of shares.")
        if not _CLIENT_ID_RE.match(self.client_order_id):
            raise ValueError("client_order_id has an invalid format.")
        if (self.order_type == OrderType.LIMIT) != (self.limit_price is not None):
            raise ValueError("limit_price is required for, and only for, limit orders.")
        if (self.order_type == OrderType.STOP) != (self.stop_price is not None):
            raise ValueError("stop_price is required for, and only for, stop orders.")
        for price in (self.limit_price, self.stop_price):
            if price is not None and (price <= 0 or price != price.quantize(Decimal("0.01"))):
                raise ValueError("prices must be positive and in whole cents.")
        if self.order_type == OrderType.STOP and self.side != Side.SELL:
            raise ValueError("stop orders are protective sells only (long-only system).")
        if self.time_in_force == TimeInForce.OPG and self.order_type == OrderType.STOP:
            raise ValueError("on-open orders must be market or limit.")


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


class Broker(Protocol):
    """Shape of a future brokerage adapter. No implementation exists in V1."""

    def submit_order(self, request: OrderRequest) -> Order: ...

    def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...

    def cancel_order(self, order_id: str) -> None: ...
