"""Strategy interface and versioned specifications.

A strategy is a pure function of (bar history up to and including today, current position
state, parameters) -> Signal. It never sees future bars: the engine hands it
`BarSeries.history_through(today)`, and tests verify that perturbing later bars cannot
change any earlier signal.

Every strategy run is identified by a StrategySpec whose hash covers the name, version,
and full parameter set, so results are always traceable to exact settings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from tradinglab.marketdata.bars import Bar


class Action(StrEnum):
    ENTER_LONG = "enter_long"
    EXIT = "exit"
    HOLD = "hold"  # keep the current position
    NO_TRADE = "no_trade"  # flat and staying flat


@dataclass(frozen=True)
class PositionState:
    in_position: bool
    entry_price: float | None = None
    stop_price: float | None = None
    bars_held: int = 0
    highest_close: float | None = None


@dataclass(frozen=True)
class Signal:
    action: Action
    reason: str
    stop_price: float | None = None  # required for ENTER_LONG; updated (trailing) for HOLD
    diagnostics: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategySpec:
    name: str
    version: str
    params: Mapping[str, Any]

    @property
    def canonical_params(self) -> dict[str, Any]:
        """Parameters with whole-number floats as ints, so fast=50 and fast=50.0 (identical
        behaviour) share one hash and one test-window evaluation."""
        out: dict[str, Any] = {}
        for k, v in self.params.items():
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            out[k] = v
        return out

    @property
    def spec_hash(self) -> str:
        payload = json.dumps(
            {"name": self.name, "version": self.version, "params": self.canonical_params},
            sort_keys=True, separators=(",", ":"), default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}#{self.spec_hash}"


class Strategy(Protocol):
    spec: StrategySpec

    @property
    def warmup_bars(self) -> int: ...

    def evaluate(self, history: Sequence[Bar], position: PositionState) -> Signal: ...
