"""Day-trading strategies (long-only, flat by the close). Each is a small, self-contained plug-in.

A strategy only answers one question after each 5-minute bar closes: "buy now (with this stop and
optional target)", "sell now", or nothing. Fills, sizing, costs, limits and the end-of-day exit
are handled by the core, identically for every strategy. To add one: subclass
IntradayStrategy, implement on_bar(), and add it to REGISTRY.

Strategy ideas are standard, published ones; parameters are round numbers chosen up front,
not tuned to the data (tuning happens only on the training window, and is recorded).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from tradinglab.marketdata.calendar import NY
from tradinglab.marketdata.intraday import IBar


@dataclass(frozen=True)
class Enter:
    stop: float
    reason: str
    target: float | None = None
    diagnostics: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Exit:
    reason: str
    diagnostics: Mapping[str, float] = field(default_factory=dict)


Signal = Enter | Exit


class RunningStats:
    """Today's running sums for one symbol, updated once per bar by the core (O(1) per bar)."""

    __slots__ = ("vwaps", "w", "wtp", "wtp2", "ranges", "n")

    def __init__(self) -> None:
        self.vwaps: list[float] = []
        self.w = self.wtp = self.wtp2 = self.ranges = 0.0
        self.n = 0

    def add(self, b: IBar) -> None:
        tp = (b.high + b.low + b.close) / 3
        w = b.volume if b.volume > 0 else 1.0  # a zero-volume bar still counts, lightly
        self.w += w
        self.wtp += w * tp
        self.wtp2 += w * tp * tp
        self.ranges += b.high - b.low
        self.n += 1
        self.vwaps.append(self.wtp / self.w)


@dataclass(frozen=True)
class StrategyContext:
    symbol: str
    day: date
    bars: Sequence[IBar]  # today's bars so far, the last one just closed
    prev_close: float | None  # yesterday's official close
    position_entry: float | None  # entry price when holding this symbol, else None
    entries_today: int
    open_utc: datetime
    close_utc: datetime
    stats: RunningStats | None = None

    def _stats(self) -> RunningStats:
        if self.stats is not None:
            return self.stats
        st = RunningStats()
        for b in self.bars:
            st.add(b)
        object.__setattr__(self, "stats", st)
        return st

    @property
    def bar(self) -> IBar:
        return self.bars[-1]

    def minutes_since_open(self) -> float:
        return (self.bar.end - self.open_utc).total_seconds() / 60

    def vwap_series(self) -> Sequence[float]:
        """Volume-weighted average price of the day so far, after each bar (IEX volume)."""
        return self._stats().vwaps

    def vwap_sigma(self, vwap: float) -> float:
        """Volume-weighted standard deviation of typical price around VWAP (VWAP bands)."""
        st = self._stats()
        var = st.wtp2 / st.w - 2 * vwap * st.wtp / st.w + vwap * vwap
        return math.sqrt(max(var, 0.0))

    def avg_range(self) -> float:
        st = self._stats()
        return st.ranges / st.n


class IntradayStrategy:
    id: ClassVar[str] = ""
    name: ClassVar[str] = ""
    summary: ClassVar[str] = ""  # one plain-English sentence for the dashboard
    rules: ClassVar[tuple[str, ...]] = ()  # plain-English rules for "how it works"
    max_entries_per_symbol: ClassVar[int] = 1
    defaults: ClassVar[dict[str, float]] = {}

    def __init__(self, **params: float) -> None:
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"unknown parameters for {self.id}: {sorted(unknown)}")
        self.params: dict[str, float] = {**self.defaults, **params}

    @property
    def label(self) -> str:
        return f"{self.id}(" + ",".join(f"{k}={v:g}" for k, v in sorted(self.params.items())) + ")"

    def on_bar(self, ctx: StrategyContext) -> Signal | None:  # pragma: no cover - interface
        raise NotImplementedError


class OpeningRangeBreakout(IntradayStrategy):
    id = "orb"
    name = "Opening breakout"
    summary = ("Buys when a fund breaks above its high from the first 30 minutes, betting the "
               "move keeps going.")
    rules = ("Notes the highest and lowest price of the first 30 minutes.",
             "Buys if a 5-minute bar later closes above that high.",
             "Safety stop at the first 30 minutes' low.",
             "Sells at the close, or earlier if the stop is hit. At most one trade per fund a day.")
    defaults = {"range_minutes": 30}

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        if ctx.position_entry is not None or ctx.entries_today:
            return None
        n = int(self.params["range_minutes"] // 5)
        range_end = ctx.open_utc + timedelta(minutes=n * 5)
        if ctx.bar.ts < range_end or len(ctx.bars) < 2:  # by the clock, not by bar count
            return None
        rng = [b for b in ctx.bars if b.ts < range_end]
        if len(rng) < n - 1:  # need (nearly) the whole opening range
            return None
        hi, lo = max(b.high for b in rng), min(b.low for b in rng)
        prev = ctx.bars[-2]
        if ctx.bar.close > hi and prev.close <= hi:
            return Enter(lo, "breakout_above_opening_range",
                         diagnostics={"range_high": hi, "range_low": lo, "close": ctx.bar.close})
        return None


class GapFill(IntradayStrategy):
    id = "gap"
    name = "Gap-down bounce"
    summary = ("When a fund opens noticeably lower than yesterday's close, buys and aims for it "
               "to climb back to that close.")
    rules = ("Looks at the first 5 minutes of the day.",
             "Buys if the fund opened between 0.25% and 1.5% below yesterday's close.",
             "Sells when it gets back to yesterday's close (the target).",
             "Safety stop the same distance below the open as the gap.",
             "If neither happens, sells at 1 pm, or at the close on short days.")
    defaults = {"min_gap": 0.0025, "max_gap": 0.015, "exit_hour": 13}

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        p = self.params
        if ctx.position_entry is not None:
            if ctx.bar.end.astimezone(NY).hour >= int(p["exit_hour"]):
                return Exit("time_exit")
            return None
        if (len(ctx.bars) != 1 or ctx.bars[0].ts != ctx.open_utc or ctx.entries_today
                or not ctx.prev_close):  # only on the real opening bar
            return None
        day_open = ctx.bars[0].open
        gap = day_open / ctx.prev_close - 1
        if not (-p["max_gap"] <= gap <= -p["min_gap"]):
            return None
        stop = day_open - (ctx.prev_close - day_open)
        return Enter(stop, "gap_down_open", target=ctx.prev_close,
                     diagnostics={"gap": gap, "prev_close": ctx.prev_close, "open": day_open})


class VwapDip(IntradayStrategy):
    id = "vwap_dip"
    name = "Dip below the day's average"
    summary = ("Buys sharp drops well below the day's average price, and sells when the price "
               "bounces back to that average.")
    rules = ("Tracks the day's average price, weighted by how much traded (called VWAP).",
             "After 10:30 am, buys when the price closes unusually far below it (2 standard "
             "deviations) and the bar closes off its low.",
             "Sells once the price closes back at the day's average.",
             "Safety stop the same distance further down. Up to two trades per fund a day.")
    max_entries_per_symbol = 2
    defaults = {"bands": 2.0, "start_minutes": 60}

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        if len(ctx.bars) < 6:
            return None
        vwap = ctx.vwap_series()[-1]
        b = ctx.bar
        if ctx.position_entry is not None:
            return Exit("back_at_average", {"vwap": vwap}) if b.close >= vwap else None
        if ctx.minutes_since_open() < self.params["start_minutes"]:
            return None
        sigma = ctx.vwap_sigma(vwap)
        if sigma <= 0:
            return None
        lower = vwap - self.params["bands"] * sigma
        closed_off_low = b.close > b.low + 0.5 * (b.high - b.low)
        if b.close < lower and closed_off_low:
            dist = vwap - b.close
            return Enter(b.close - dist, "far_below_average",
                         diagnostics={"vwap": vwap, "sigma": sigma, "close": b.close})
        return None


class VwapTrend(IntradayStrategy):
    id = "vwap_trend"
    name = "Ride the day's trend"
    summary = ("On days a fund is climbing, buys when it dips back to the day's average price "
               "and bounces.")
    rules = ("After 10 am, only on days the price is above where it opened and the day's "
             "average price (VWAP) is rising.",
             "Buys when a 5-minute bar dips to the average but closes above it.",
             "Sells if the price closes clearly below the average (the trend broke), or at "
             "the close.",
             "Safety stop just under the average. Up to two trades per fund a day.")
    max_entries_per_symbol = 2
    defaults = {"start_minutes": 30, "slope_bars": 6, "buffer_ranges": 0.5}

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        k = int(self.params["slope_bars"])
        if len(ctx.bars) <= k:
            return None
        vw = ctx.vwap_series()
        vwap = vw[-1]
        b = ctx.bar
        buf = self.params["buffer_ranges"] * ctx.avg_range()
        if ctx.position_entry is not None:
            return Exit("trend_broke", {"vwap": vwap}) if b.close < vwap - buf else None
        if ctx.minutes_since_open() < self.params["start_minutes"]:
            return None
        rising = vwap > vw[-1 - k]
        up_day = b.close > ctx.bars[0].open
        touched = b.low <= vwap * 1.0005 and b.close > vwap
        if rising and up_day and touched:
            return Enter(min(b.low, vwap) - buf, "bounce_off_rising_average",
                         diagnostics={"vwap": vwap, "close": b.close})
        return None


REGISTRY: dict[str, type[IntradayStrategy]] = {
    cls.id: cls for cls in (OpeningRangeBreakout, GapFill, VwapDip, VwapTrend)
}


def create(strategy_id: str, **params: Any) -> IntradayStrategy:
    return REGISTRY[strategy_id](**params)
