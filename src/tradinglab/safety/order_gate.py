"""The single choke point every order must pass through before reaching a broker.

The gate never raises for an ordinary "no" - it returns a GateDecision with reasons, which
the engine logs as a rejected-trade decision. It raises only for safety violations that
indicate a bug or tampering (a non-paper broker).

Checks, in order:
  1. Broker reports itself as paper (else LiveTradingForbiddenError).
  2. Emergency pause is off (exits are still allowed while paused only if explicitly
     permitted by allow_exits_when_paused - default True, so risk can be reduced).
  3. ORDER_SUBMISSION_ENABLED is true (else the order becomes a "would submit" dry run).
  4. Symbol is on the configured instrument list.
  5. Long-only: a sell may not exceed the held quantity.
  6. Idempotency: no existing order with the same client_order_id.
  7. Entries only: no entry while a halt is active.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from tradinglab.broker.base import Broker, OrderRequest, Side
from tradinglab.config import Settings
from tradinglab.safety.endpoints import LiveTradingForbiddenError


class GateOutcome(StrEnum):
    SUBMIT = "submit"  # passed every check; may be sent to the paper broker
    DRY_RUN = "dry_run"  # passed every check but order submission is disabled
    REJECT = "reject"


@dataclass(frozen=True)
class GateDecision:
    outcome: GateOutcome
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def may_submit(self) -> bool:
        return self.outcome == GateOutcome.SUBMIT


class PauseState(Protocol):
    def is_paused(self) -> bool: ...


class InMemoryPause:
    """Process-local pause switch (tests / dry runs). Production uses the database."""

    def __init__(self, paused: bool = False) -> None:
        self._paused = paused

    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False


class OrderGate:
    def __init__(
        self,
        settings: Settings,
        broker: Broker,
        pause: PauseState,
        *,
        allow_exits_when_paused: bool = True,
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._pause = pause
        self._allow_exits_when_paused = allow_exits_when_paused

    def check(
        self,
        request: OrderRequest,
        *,
        held_qty: Decimal,
        entries_halted: bool = False,
        halt_reasons: tuple[str, ...] = (),
    ) -> GateDecision:
        if getattr(self._broker, "is_paper", False) is not True:
            raise LiveTradingForbiddenError("Order gate refused: broker is not a paper broker.")

        is_exit = request.side == Side.SELL
        reasons: list[str] = []

        if self._pause.is_paused() and not (is_exit and self._allow_exits_when_paused):
            reasons.append("emergency_pause_active")

        if request.symbol not in self._settings.symbols:
            reasons.append(f"symbol_not_allowed:{request.symbol}")

        if is_exit:
            if held_qty <= 0:
                reasons.append("no_position_to_sell(long_only)")
            elif request.qty > held_qty:
                reasons.append(f"sell_exceeds_position(long_only):{request.qty}>{held_qty}")
        elif entries_halted:
            reasons.extend(halt_reasons or ("entries_halted",))

        if self._broker.get_order_by_client_id(request.client_order_id) is not None:
            reasons.append("duplicate_client_order_id")

        if reasons:
            return GateDecision(GateOutcome.REJECT, tuple(reasons))
        if not self._settings.order_submission_enabled:
            return GateDecision(GateOutcome.DRY_RUN, ("order_submission_disabled",))
        return GateDecision(GateOutcome.SUBMIT)
