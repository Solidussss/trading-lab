"""Trend following v1: moving-average regime filter with an ATR trailing stop.

Rules (evaluated on the completed daily bar; orders execute at the next session's open):
  Entry   flat AND close > SMA(slow) AND SMA(fast) > SMA(slow)
  Stop    initial = close - atr_mult * ATR(atr_period); trails up as
          highest_close_since_entry - atr_mult * ATR, never down
  Exit    SMA(fast) < SMA(slow), OR close < SMA(slow)
          (the protective stop is a resting order handled by the execution layer)

Deliberately few parameters (fast, slow, atr_period, atr_mult) to limit overfitting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tradinglab.marketdata.bars import Bar
from tradinglab.strategies.base import Action, PositionState, Signal, StrategySpec
from tradinglab.strategies.indicators import atr, sma

NAME = "trend_following"
VERSION = "1.0.0"
DEFAULT_PARAMS = {"fast": 50, "slow": 200, "atr_period": 20, "atr_mult": 3.0}


@dataclass
class TrendFollowing:
    spec: StrategySpec

    @classmethod
    def create(cls, **overrides: float) -> TrendFollowing:
        params = {**DEFAULT_PARAMS, **overrides}
        if not (1 < params["fast"] < params["slow"]):
            raise ValueError("require 1 < fast < slow")
        if params["atr_mult"] <= 0 or params["atr_period"] < 2:
            raise ValueError("invalid ATR parameters")
        return cls(StrategySpec(NAME, VERSION, params))

    @property
    def warmup_bars(self) -> int:
        p = self.spec.params
        return int(max(p["slow"], p["atr_period"] + 1))

    def evaluate(self, history: Sequence[Bar], position: PositionState) -> Signal:
        p = self.spec.params
        if len(history) < self.warmup_bars:
            action = Action.HOLD if position.in_position else Action.NO_TRADE
            return Signal(action, "warmup", stop_price=position.stop_price)

        close = history[-1].close
        fast = sma(history, int(p["fast"]))
        slow = sma(history, int(p["slow"]))
        a = atr(history, int(p["atr_period"]))
        assert fast is not None and slow is not None and a is not None
        diag = {"close": close, "sma_fast": fast, "sma_slow": slow, "atr": a}

        if not position.in_position:
            if close > slow and fast > slow:
                stop = close - float(p["atr_mult"]) * a
                if stop <= 0:
                    return Signal(Action.NO_TRADE, "stop_non_positive", diagnostics=diag)
                return Signal(Action.ENTER_LONG, "uptrend: close>slow & fast>slow", stop, diag)
            why = "close<=sma_slow" if close <= slow else "sma_fast<=sma_slow"
            return Signal(Action.NO_TRADE, f"no_uptrend: {why}", diagnostics=diag)

        if fast < slow:
            return Signal(Action.EXIT, "trend_end: sma_fast<sma_slow", diagnostics=diag)
        if close < slow:
            return Signal(Action.EXIT, "trend_end: close<sma_slow", diagnostics=diag)

        highest = max(position.highest_close or close, close)
        trail = highest - float(p["atr_mult"]) * a
        new_stop = max(position.stop_price or 0.0, trail)
        return Signal(Action.HOLD, "trend_intact", new_stop, diag)
