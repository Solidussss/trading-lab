"""Alpaca PAPER broker adapter.

This is the only module that constructs an alpaca-py TradingClient. Safety properties:

  1. The network guard is installed before any client exists.
  2. TradingClient is always constructed with paper=True AND an explicit
     url_override=https://paper-api.alpaca.markets (validated against the allowlist), so
     neither a library default nor an environment variable can change the target.
  3. After construction, the client's actual base URL is read back and re-validated.
     If it cannot be read or is not the paper host, construction fails (fail closed).
  4. Credentials come only from Settings (paper-prefixed env vars, validated).
  5. Every order passes through the OrderGate (tradinglab.safety.order_gate) before
     reaching submit_order; this adapter does not decide whether to trade.

alpaca-py is imported lazily so the rest of the system (backtests, tests, dashboard)
does not depend on it being importable.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tradinglab.broker.base import (
    Account,
    MarketClock,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from tradinglab.config import Settings
from tradinglab.safety.endpoints import (
    PAPER_TRADING_URL,
    EndpointNotAllowedError,
    LiveTradingForbiddenError,
    validate_trading_url,
)
from tradinglab.safety.network_guard import install_network_guard

log = logging.getLogger(__name__)

# Attribute names alpaca-py's RESTClient has used for the resolved base URL.
_BASE_URL_ATTRS = ("_base_url", "base_url")


def build_paper_trading_client(settings: Settings, *, client_cls: Any | None = None) -> Any:
    """Construct a verified paper-only alpaca-py TradingClient.

    `client_cls` exists for tests (inject a fake); production passes nothing.
    """
    install_network_guard()

    if settings.trading_mode != "paper":
        raise LiveTradingForbiddenError("Settings are not in paper mode.")
    url = validate_trading_url(settings.trading_base_url)
    if url != PAPER_TRADING_URL:
        raise LiveTradingForbiddenError(f"Unexpected trading URL {url!r}.")

    if client_cls is None:
        from alpaca.trading.client import TradingClient as client_cls  # noqa: N813

    client = client_cls(
        api_key=settings.alpaca_api_key.get(),
        secret_key=settings.alpaca_secret_key.get(),
        paper=True,
        url_override=url,
    )
    verify_client_is_paper(client)
    return client


def verify_client_is_paper(client: Any) -> None:
    """Read the client's effective base URL back and refuse anything but the paper host."""
    resolved = None
    for attr in _BASE_URL_ATTRS:
        value = getattr(client, attr, None)
        if value:
            # alpaca-py's BaseURL is a (str, Enum); str() of a member is its name, not URL.
            resolved = str(getattr(value, "value", value))
            break
    if resolved is None:
        raise EndpointNotAllowedError(
            "Cannot determine the trading client's base URL; refusing to continue."
        )
    validate_trading_url(resolved.rstrip("/"))
    if getattr(client, "_use_raw_data", False) not in (False, None):
        raise EndpointNotAllowedError("raw_data mode is not supported.")


class AlpacaPaperBroker:
    """Broker implementation backed by the Alpaca paper trading API."""

    is_paper = True

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self._client = client if client is not None else build_paper_trading_client(settings)
        verify_client_is_paper(self._client)

    # -- reads ---------------------------------------------------------------------------

    def get_account(self) -> Account:
        a = self._client.get_account()
        return Account(
            account_id=str(a.id),
            equity=Decimal(str(a.equity)),
            cash=Decimal(str(a.cash)),
            buying_power=Decimal(str(a.buying_power)),
            last_equity=Decimal(str(a.last_equity)),
            trading_blocked=bool(a.trading_blocked or a.account_blocked),
            is_paper=True,
        )

    def get_clock(self) -> MarketClock:
        c = self._client.get_clock()
        return MarketClock(
            timestamp=c.timestamp, is_open=bool(c.is_open), next_open=c.next_open,
            next_close=c.next_close,
        )

    def get_trading_days(self, start: date, end: date) -> list[date]:
        from alpaca.trading.requests import GetCalendarRequest

        days = self._client.get_calendar(GetCalendarRequest(start=start, end=end))
        return [d.date for d in days]

    def list_positions(self) -> list[Position]:
        return [
            Position(
                symbol=p.symbol,
                qty=Decimal(str(p.qty)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                market_value=Decimal(str(p.market_value)),
            )
            for p in self._client.get_all_positions()
        ]

    def list_open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [_to_order(o) for o in orders]

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        try:
            o = self._client.get_order_by_client_id(client_order_id)
        except Exception as exc:  # alpaca-py raises APIError on 404
            if _is_not_found(exc):
                return None
            raise
        return _to_order(o)

    # -- writes --------------------------------------------------------------------------

    def submit_order(self, request: OrderRequest) -> Order:
        verify_client_is_paper(self._client)  # re-check immediately before any write

        from alpaca.trading.enums import OrderSide
        from alpaca.trading.enums import TimeInForce as ATif
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        common: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": float(request.qty),
            "side": OrderSide.BUY if request.side == Side.BUY else OrderSide.SELL,
            "time_in_force": ATif(request.time_in_force.value),
            "client_order_id": request.client_order_id,
        }
        if request.order_type == OrderType.LIMIT:
            req = LimitOrderRequest(limit_price=float(request.limit_price), **common)
        else:
            req = MarketOrderRequest(**common)
        log.info("Submitting PAPER order %s %s x%s (%s)", request.side, request.symbol,
                 request.qty, request.client_order_id)
        return _to_order(self._client.submit_order(req))

    def cancel_order(self, order_id: str) -> None:
        verify_client_is_paper(self._client)
        self._client.cancel_order_by_id(order_id)


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return code == 404 or "not found" in str(exc).lower()


def _to_order(o: Any) -> Order:
    status_raw = getattr(o.status, "value", o.status)
    try:
        status = OrderStatus(str(status_raw))
    except ValueError:
        status = OrderStatus.UNKNOWN
    side_raw = str(getattr(o.side, "value", o.side))
    return Order(
        id=str(o.id),
        client_order_id=str(o.client_order_id),
        symbol=o.symbol,
        side=Side(side_raw),
        qty=Decimal(str(o.qty or 0)),
        filled_qty=Decimal(str(o.filled_qty or 0)),
        filled_avg_price=Decimal(str(o.filled_avg_price)) if o.filled_avg_price else None,
        status=status,
        submitted_at=_dt(getattr(o, "submitted_at", None)),
        filled_at=_dt(getattr(o, "filled_at", None)),
    )


def _dt(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None
