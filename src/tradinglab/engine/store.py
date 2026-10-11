"""Persistence interface for the live simulation, plus an in-memory implementation.

Phase 4 adds PostgreSQL behind the same interface. Contract:

  * commit_session() is ATOMIC: the new portfolio state and every record the session
    produced (decisions, fills, trades, equity point, the bars used) are saved together or
    not at all, so a crash mid-run can never leave a half-processed session.
  * State is stored as JSON (PortfolioState.to_dict()); the in-memory store round-trips it
    on every commit so persistence bugs surface in tests, not in production.
  * run_lock() prevents two engine runs from processing sessions concurrently (Postgres
    uses an advisory lock).
"""

from __future__ import annotations

import contextlib
import copy
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from tradinglab.marketdata.bars import Bar
from tradinglab.sim.core import Decision, EquityPoint, Fill, PortfolioState, StepResult, Trade


class EngineBusyError(RuntimeError):
    """Another engine run holds the lock."""


@dataclass(frozen=True)
class EngineEvent:
    at: datetime
    session: date | None
    kind: str  # initialized|processed|waiting_for_data|data_gap_accepted|data_error|paused|...
    detail: str = ""


class Store(Protocol):
    def load_state(self) -> PortfolioState | None: ...
    def save_state(self, state: PortfolioState) -> None: ...
    def commit_session(self, state: PortfolioState, result: StepResult,
                       bars_used: Mapping[str, Bar | None]) -> None: ...
    def add_event(self, event: EngineEvent) -> None: ...
    def events(self) -> list[EngineEvent]: ...
    def recent_events(self, n: int | None = 50) -> list[EngineEvent]: ...
    def decisions(self) -> list[Decision]: ...
    def recent_decisions(self, n: int | None = 100) -> list[Decision]: ...
    def fills(self) -> list[Fill]: ...
    def trades(self) -> list[Trade]: ...
    def equity_curve(self) -> list[EquityPoint]: ...
    def bars_used(self, session: date) -> dict[str, Bar | None]: ...
    def get_flag(self, name: str, default: bool = False) -> bool: ...
    def set_flag(self, name: str, value: bool, at: datetime | None = None) -> None: ...
    def flag_at(self, name: str, when: datetime, default: bool = False) -> bool: ...
    def run_lock(self) -> contextlib.AbstractContextManager[None]: ...
    def save_prices(self, symbol: str, bars: list[Bar]) -> None: ...
    def price_history(self, symbol: str, n: int = 320) -> list[Bar]: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._state: dict[str, Any] | None = None
        self._decisions: list[Decision] = []
        self._fills: list[Fill] = []
        self._trades: list[Trade] = []
        self._curve: list[EquityPoint] = []
        self._bars: dict[date, dict[str, Bar | None]] = {}
        self._events: list[EngineEvent] = []
        self._flags: dict[str, list[tuple[datetime, bool]]] = {}  # name -> history
        self._prices: dict[str, dict[date, Bar]] = {}  # display-only price history
        self._lock = threading.Lock()
        self.fail_next_commit = False  # failure injection for atomicity tests

    def load_state(self) -> PortfolioState | None:
        return PortfolioState.from_dict(copy.deepcopy(self._state)) if self._state else None

    def save_state(self, state: PortfolioState) -> None:
        self._state = copy.deepcopy(state.to_dict())

    def commit_session(self, state, result, bars_used):
        payload = copy.deepcopy(state.to_dict())  # serialise first: fail before mutating
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise OSError("simulated database failure during commit")
        if result.day in self._bars:
            raise ValueError(f"session {result.day} already committed")
        self._state = payload
        self._decisions += result.decisions
        self._fills += result.fills
        self._trades += result.trades
        if result.equity is not None:
            self._curve.append(result.equity)
        self._bars[result.day] = dict(bars_used)

    def add_event(self, event):
        self._events.append(event)

    def events(self):
        return list(self._events)

    def recent_events(self, n=50):
        return list(self._events[-n:]) if n else list(self._events)

    def decisions(self):
        return list(self._decisions)

    def recent_decisions(self, n=100):
        return list(self._decisions[-n:]) if n else list(self._decisions)

    def fills(self):
        return list(self._fills)

    def trades(self):
        return list(self._trades)

    def equity_curve(self):
        return list(self._curve)

    def bars_used(self, session):
        return dict(self._bars.get(session, {}))

    def get_flag(self, name, default=False):
        hist = self._flags.get(name)
        return hist[-1][1] if hist else default

    def set_flag(self, name, value, at=None):
        from datetime import UTC

        self._flags.setdefault(name, []).append((at or datetime.now(UTC), bool(value)))

    def flag_at(self, name, when, default=False):
        """Value the flag had at `when` (history-aware, for catch-up runs)."""
        value = default
        for t, v in self._flags.get(name, []):
            if t <= when:
                value = v
        return value

    def save_prices(self, symbol, bars):
        self._prices.setdefault(symbol, {}).update({b.day: b for b in bars})

    def price_history(self, symbol, n=320):
        bars = self._prices.get(symbol, {})
        return [bars[d] for d in sorted(bars)][-n:]

    @contextlib.contextmanager
    def run_lock(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise EngineBusyError("another engine run is in progress")
        try:
            yield
        finally:
            self._lock.release()
