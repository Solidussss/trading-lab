"""Indicators computed strictly from the bars passed in (no future data by construction).

Each function takes a history ending at "today" and returns today's value (or None if
there is not enough history). They are deliberately simple and O(n) per call; with three
symbols of daily data this is fast enough and keeps the no-look-ahead property obvious.
"""

from __future__ import annotations

from collections.abc import Sequence

from tradinglab.marketdata.bars import Bar


def sma(history: Sequence[Bar], period: int, offset: int = 0) -> float | None:
    """Simple moving average of closes ending `offset` bars before the last bar."""
    end = len(history) - offset
    if period <= 0 or end < period:
        return None
    window = history[end - period : end]
    return sum(b.close for b in window) / period


def true_range(cur: Bar, prev_close: float) -> float:
    return max(cur.high - cur.low, abs(cur.high - prev_close), abs(cur.low - prev_close))


def atr(history: Sequence[Bar], period: int) -> float | None:
    """Average true range (simple average of the last `period` true ranges)."""
    if period <= 0 or len(history) < period + 1:
        return None
    trs = [true_range(history[i], history[i - 1].close)
           for i in range(len(history) - period, len(history))]
    return sum(trs) / period


def rate_of_change(history: Sequence[Bar], period: int) -> float | None:
    if len(history) < period + 1:
        return None
    return history[-1].close / history[-1 - period].close - 1.0
