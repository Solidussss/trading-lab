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

from tradinglab.marketdata.calendar import NY, session
from tradinglab.marketdata.intraday import IBar


@dataclass(frozen=True)
class Enter:
    """Buy. With no `limit`: a market order at the next bar's open. With `limit`: a resting buy
    order at that price, filled when a later bar trades down to it, and cancelled if a bar
    closes below `cancel_below` or the entry window ends."""

    stop: float
    reason: str
    target: float | None = None
    diagnostics: Mapping[str, float] = field(default_factory=dict)
    limit: float | None = None
    cancel_below: float | None = None


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
    history: Sequence[Sequence[IBar]] = ()  # earlier sessions' bars, oldest first (if asked for)

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
    history_days: ClassVar[int] = 0  # earlier sessions of 5-minute bars this strategy needs
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


class FairValueGap(IntradayStrategy):
    """Bullish fair value gap (FVG), as defined in discretionary day trading (often "ICT").

    Pattern: three consecutive 5-minute candles 1, 2, 3 where candle 1's high is below candle 3's
    low. Candle 2 is the "displacement": a large up candle. The range between candle 1's high
    and candle 3's low is the gap. The trade: a resting buy order at the gap's midpoint (its
    "consequent encroachment"), waiting for the price to come back.
      * Displacement: candle 2 closes up and its range is at least 1.5x the day's average.
      * Gap at least 0.05% of the price, formed after the first 15 minutes, with candle 3
        closing above the day's average price (trade in the day's direction - a stand-in for the
        "higher timeframe bias" discretionary traders use).
      * Stop just below candle 1's low; target 2x the risk; sold by the close.
      * The order is cancelled if a candle closes below the gap (the gap failed), or when the
        entry window ends 30 minutes before the close.
    The order sits from the moment candle 3 closes, so it can only fill on later prices.
    """

    id = "fvg"
    name = "Fair value gap"
    summary = ("After a sharp move up leaves a gap between candles, waits with a buy order in the "
               "middle of the gap for the price to come back, betting the move resumes.")
    rules = ("Looks for three 5-minute candles where the third one's low is above the first "
             "one's high, with a big up candle in the middle. The price range in between is the "
             "fair value gap.",
             "Only counts gaps after the first 15 minutes, and only when the price is above the "
             "day's average price.",
             "Places a buy order at the middle of the gap and waits for the price to come back "
             "to it.",
             "Cancels the order if a candle closes below the gap. Safety stop just below the "
             "first candle's low. Target twice the amount risked. Sells at the close if neither "
             "is hit. Up to two trades per fund a day.")
    max_entries_per_symbol = 2
    defaults = {"min_gap": 0.0005, "displacement": 1.5, "entry_level": 0.5, "reward_risk": 2.0,
                "stop_buffer": 0.1, "start_minutes": 15}

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        if ctx.position_entry is not None or len(ctx.bars) < 4:
            return None
        p = self.params
        a, b, c = ctx.bars[-3], ctx.bars[-2], ctx.bars[-1]  # the gap is decided as c closes
        bottom, top = a.high, c.low
        avg = ctx.avg_range()
        if not (top > bottom and a.ts >= ctx.open_utc + timedelta(minutes=p["start_minutes"])
                and b.close > b.open and (b.high - b.low) >= p["displacement"] * avg
                and (top - bottom) >= p["min_gap"] * c.close
                and c.close > ctx.vwap_series()[-1]):
            return None
        entry = bottom + p["entry_level"] * (top - bottom)
        stop = a.low - p["stop_buffer"] * avg
        return Enter(stop, "fair_value_gap_order", target=entry + p["reward_risk"] * (entry - stop),
                     limit=entry, cancel_below=bottom,
                     diagnostics={"gap_bottom": bottom, "gap_top": top, "entry": entry})


class NoiseBreakout(IntradayStrategy):
    """Intraday momentum past the "noise area", after Zarattini, Aziz and Barbon (2024), "Beat
    the Market: An effective intraday momentum strategy for the S&P 500 ETF (SPY)".

    For each time of day, the noise is the average distance the fund was from that day's open
    at that time over the last 14 sessions. The upper edge is max(today's open, yesterday's
    close) x (1 + noise). Checked only every 30 minutes, from 10 am: buy when the price closes
    above both the upper edge and VWAP; sell at a later check when it closes back below either
    one (the paper's trailing stop). Long-only here, and sized by the shared risk rules instead
    of the paper's volatility targeting and leverage. Parameters are the paper's, not tuned.
    """

    id = "noise"
    name = "Breakout past normal noise"
    summary = ("Learns how far each fund normally drifts from its open at each time of day, and "
               "buys only when today's climb is clearly bigger than that normal noise.")
    rules = ("For every time of day, works out how far the fund usually is from its open, "
             "averaged over the last 14 trading days. Anything inside that is 'noise'.",
             "Checks every 30 minutes from 10 am. Buys when the price closes above the top of "
             "the noise (measured from the higher of today's open and yesterday's close) and "
             "above the day's average price (VWAP).",
             "At each later check, sells if the price has dropped back below either line.",
             "Sells at the close at the latest. Emergency stop 5% down, which also sizes each "
             "fund to about a tenth of the account, so all three can be held at once. Up to two "
             "trades per fund a day.")
    max_entries_per_symbol = 2
    history_days = 14
    defaults = {"lookback": 14, "min_days": 10, "start_minutes": 30, "every_minutes": 30,
                "disaster_stop": 0.05}

    def __init__(self, **params: float) -> None:
        super().__init__(**params)
        self._noise: dict[tuple[str, date], dict[int, float]] = {}

    def noise(self, ctx: StrategyContext) -> dict[int, float]:
        """Average |price / day's open - 1| by minutes after the open, over earlier sessions."""
        key = (ctx.symbol, ctx.day)
        if key not in self._noise:
            if len(self._noise) > 16:
                self._noise.clear()
            sums: dict[int, float] = {}
            counts: dict[int, int] = {}
            for bars in list(ctx.history)[-int(self.params["lookback"]):]:
                if not bars:
                    continue
                opened = session(bars[0].day).open_utc
                if bars[0].ts != opened or bars[0].open <= 0:
                    continue  # no real opening bar: that day's distances would be wrong
                for b in bars:
                    k = round((b.end - opened).total_seconds() / 60)
                    sums[k] = sums.get(k, 0.0) + abs(b.close / bars[0].open - 1)
                    counts[k] = counts.get(k, 0) + 1
            self._noise[key] = {k: sums[k] / counts[k] for k in sums
                                if counts[k] >= self.params["min_days"]}
        return self._noise[key]

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        p = self.params
        k = round(ctx.minutes_since_open())
        if k < p["start_minutes"] or k % int(p["every_minutes"]):
            return None
        if not ctx.prev_close or ctx.bars[0].ts != ctx.open_utc:
            return None
        sigma = self.noise(ctx).get(k)
        if not sigma:
            return None
        b = ctx.bar
        upper = max(ctx.bars[0].open, ctx.prev_close) * (1 + sigma)
        vwap = ctx.vwap_series()[-1]
        line = max(upper, vwap)
        diag = {"noise": sigma, "upper": upper, "vwap": vwap, "close": b.close}
        if ctx.position_entry is not None:
            return Exit("back_inside_noise", diag) if b.close < line else None
        if b.close > line:
            return Enter(b.close * (1 - p["disaster_stop"]), "broke_above_noise", diagnostics=diag)
        return None


class LateDayMomentum(IntradayStrategy):
    """Market intraday momentum, after Gao, Han, Li and Zhou (2018), "Market intraday
    momentum", Journal of Financial Economics 129(2). The return from yesterday's close to
    10 am predicts the return of the last half hour. Long-only: buy for the last 30 minutes
    when the first-half-hour return is positive. Parameters are the paper's, not tuned."""

    id = "late_momentum"
    name = "Last half-hour momentum"
    summary = ("When a fund is up from yesterday's close by 10 am, buys it for the last half "
               "hour of the day, when that early move tends to continue.")
    rules = ("At 10 am, notes whether the fund is above or below yesterday's close.",
             "If it was above, buys at 3:30 pm (30 minutes before the close).",
             "Sells at the close. One trade per fund a day, so costs stay low.",
             "Emergency stop 5% down, which also sizes each fund to about a tenth of the "
             "account, so all three can be held at once.")
    defaults = {"first_minutes": 30, "last_minutes": 30, "disaster_stop": 0.05}

    def on_bar(self, ctx: StrategyContext) -> Signal | None:
        p = self.params
        if ctx.position_entry is not None or ctx.entries_today or not ctx.prev_close:
            return None
        if ctx.bar.end != ctx.close_utc - timedelta(minutes=p["last_minutes"]):
            return None
        mark = ctx.open_utc + timedelta(minutes=p["first_minutes"])
        early = [b for b in ctx.bars if b.end == mark]
        if not early:
            return None
        first = early[0].close / ctx.prev_close - 1
        if first <= 0:
            return None
        return Enter(ctx.bar.close * (1 - p["disaster_stop"]), "up_by_10am",
                     diagnostics={"first_half_hour": first, "close": ctx.bar.close})


REGISTRY: dict[str, type[IntradayStrategy]] = {
    cls.id: cls for cls in (OpeningRangeBreakout, GapFill, VwapDip, VwapTrend, FairValueGap,
                             NoiseBreakout, LateDayMomentum)
}


def create(strategy_id: str, **params: Any) -> IntradayStrategy:
    return REGISTRY[strategy_id](**params)
