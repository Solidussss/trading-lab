"""Where the live engine gets daily bars from (provider-agnostic)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol

from tradinglab.marketdata.bars import BarSeries


class BarSource(Protocol):
    def history(self, symbols: Sequence[str], through: date) -> dict[str, BarSeries]:
        """Validated daily bars for each symbol, ending no later than `through`."""
        ...


class InMemoryBarSource:
    """Serves bars from preloaded series, never past `through` (tests, replays)."""

    def __init__(self, data: Mapping[str, BarSeries]) -> None:
        self._data = dict(data)

    def history(self, symbols: Sequence[str], through: date) -> dict[str, BarSeries]:
        out = {}
        for s in symbols:
            series = self._data.get(s)
            out[s] = BarSeries(s, [b for b in series.bars if b.day <= through] if series else [])
        return out
