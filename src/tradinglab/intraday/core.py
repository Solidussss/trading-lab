"""Intraday simulation core: one trading day, 5-minute bar by 5-minute bar, for one bot.

The same `IntradayCore.run_day()` is used by the live bots, the dashboard's "today so far" view
and the historical tests, so all three give identical results for identical bars.

Timeline and fills (no look-ahead):
  * After bar t closes, the strategy sees bars up to and including t and may signal.
  * A signal becomes a market order filled at the OPEN of that symbol's next bar (plus costs).
    If the next bar is missing or more than 10 minutes later, the order is cancelled.
  * Stops and targets rest from the fill onward and are checked against each bar's low/high.
    A bar that opens through a stop fills at the open (gap). If one bar touches both the stop
    and the target, the stop is assumed to have happened first (the pessimistic choice).
  * No new entries in the last 30 minutes. Every position is sold at the close of the day's
    final bar, so the bot never holds overnight.
  * Each 5-minute step is processed for all funds together (fills, then prices, then limits,
    then decisions), so the order of the fund list cannot change a result, and a limit that
    trips is re-checked when a pending order would fill.
  * Missing bars are simply gaps (an order waiting on a missing bar is cancelled); nothing about
    the rest of the day is used to decide whether to trade, so "today so far" during the day is
    always the beginning of the final result.

Risk (per bot, same limits as the daily account):
  * Size = risk budget / (entry - stop), where the budget is 0.5% of the day's opening equity,
    capped so total invested <= 30% of equity, by available cash, and by 3 open positions.
  * Daily loss halt: once marked-to-market equity is 3% below the day's start, no new entries
    for the rest of the day. Peak halt: 10% below the best end-of-day equity latches until the
    owner resumes it. Existing positions are always still managed and closed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from tradinglab.engine.risk import RiskParams
from tradinglab.intraday.strategies import (
    Enter,
    Exit,
    IntradayStrategy,
    RunningStats,
    StrategyContext,
)
from tradinglab.marketdata.calendar import session
from tradinglab.marketdata.intraday import BAR, IBar
from tradinglab.research.costs import CostModel

ENTRY_CUTOFF = timedelta(minutes=30)
MAX_FILL_DELAY = timedelta(minutes=10)
GAP_BUFFER = 0.001
LIMIT_TRADE_THROUGH = 0.01  # a resting buy fills only when the price trades a cent below it

# Intraday costs: market orders on SPY/QQQ/IWM at the next 5-minute open. Real spreads are a
# fraction of a basis point; 2 bp per side plus a further 3 bp on stop fills, plus a
# per-share commission (Interactive Brokers fixed pricing: $0.005/share, $1 minimum), keeps
# the results on the cautious side without assuming a broker nobody can get.
INTRADAY_COSTS = CostModel(half_spread_bps=0.5, slippage_bps=1.5, stop_extra_bps=3.0,
                           commission_per_share=0.005, commission_min=1.0)


@dataclass
class BotState:
    """Everything carried from one day to the next. Bots are flat overnight, so it is cash."""

    cash: float
    initial_capital: float
    peak_equity: float
    start_day: date | None = None
    last_day: date | None = None
    days: int = 0
    halt_latched: bool = False
    latched_days: int = 0
    config_fingerprint: str = ""

    @classmethod
    def initial(cls, capital: float, start_day: date | None = None) -> BotState:
        return cls(capital, capital, capital, start_day)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("start_day", "last_day"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> BotState:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        for k in ("start_day", "last_day"):
            if known.get(k):
                known[k] = date.fromisoformat(known[k])
        return cls(**known)


@dataclass(frozen=True)
class IFill:
    ts: datetime
    symbol: str
    side: str  # buy | sell
    shares: int
    ref_price: float
    fill_price: float
    fees: float
    reason: str


@dataclass(frozen=True)
class ITrade:
    day: date
    symbol: str
    strategy: str
    entry_ts: datetime
    exit_ts: datetime
    shares: int
    entry_price: float
    exit_price: float
    pnl: float  # after every cost and fee
    costs: float
    return_pct: float
    r_multiple: float
    entry_reason: str
    exit_reason: str


@dataclass(frozen=True)
class ISignal:
    """A logged decision: every signal, every order, every rejection."""

    ts: datetime
    symbol: str
    action: str  # enter | exit | skip
    outcome: str  # ordered | filled | rejected | cancelled | excluded
    reason: str
    diagnostics: Mapping[str, float] = field(default_factory=dict)


@dataclass
class DayResult:
    day: date
    start_equity: float
    end_equity: float
    final: bool
    fills: list[IFill] = field(default_factory=list)
    trades: list[ITrade] = field(default_factory=list)
    signals: list[ISignal] = field(default_factory=list)
    open_positions: dict[str, dict[str, float]] = field(default_factory=dict)  # final=False only
    halted: bool = False
    excluded: dict[str, str] = field(default_factory=dict)  # symbol -> why it sat out

    @property
    def pnl(self) -> float:
        return self.end_equity - self.start_equity


@dataclass
class _Pos:
    symbol: str
    shares: int
    entry_ts: datetime
    entry_price: float  # fill incl. costs
    entry_ref: float  # the bar open before costs
    entry_fees: float
    stop: float
    target: float | None
    initial_risk: float
    reason: str
    by_limit: bool = False


@dataclass
class _Order:
    symbol: str
    kind: str  # enter | exit
    placed: datetime
    reason: str
    stop: float | None = None
    target: float | None = None
    diagnostics: Mapping[str, float] = field(default_factory=dict)
    limit: float | None = None  # resting buy limit (else: market order at the next open)
    cancel_below: float | None = None


class IntradayCore:
    def __init__(self, strategy: IntradayStrategy, symbols: Sequence[str], risk: RiskParams,
                 costs: CostModel = INTRADAY_COSTS, *, auto_resume_days: int | None = None) -> None:
        """auto_resume_days: historical tests only. Live, a peak halt waits for the owner; a
        test models that decision as a reset of the peak after this many halted days."""
        self.strategy = strategy
        self.symbols = list(symbols)
        self.risk = risk
        self.costs = costs
        self.auto_resume_days = auto_resume_days

    # ------------------------------------------------------------------------------------

    def run_day(self, state: BotState, day: date, bars: Mapping[str, Sequence[IBar]],
                prev_close: Mapping[str, float | None], *, final: bool = True,
                paused: bool | Callable[[datetime], bool] = False) -> DayResult:
        """Simulate `day`. With final=True, `state` is advanced (positions closed at the close);
        with final=False (today, still trading) nothing is mutated and open positions are
        marked at their latest price."""
        if final and state.halt_latched and self.auto_resume_days is not None:
            state.latched_days += 1
            if state.latched_days > self.auto_resume_days:
                state.halt_latched, state.latched_days, state.peak_equity = False, 0, state.cash
        is_paused = paused if callable(paused) else (lambda _ts: bool(paused))
        sess = session(day)
        cutoff = sess.close_utc - ENTRY_CUTOFF
        start_equity = state.cash
        res = DayResult(day, start_equity, start_equity, final)
        cash = state.cash
        peak = state.peak_equity
        latched = state.halt_latched
        budget = start_equity * self.risk.risk_per_trade_pct

        usable: dict[str, list[IBar]] = {}
        for sym in self.symbols:
            sb = sorted((b for b in bars.get(sym, ()) if b.day == day), key=lambda b: b.ts)
            if not sb:  # decided without peeking: only a symbol with no data at all sits out
                if final:
                    res.excluded[sym] = "no price data was available for it"
                    res.signals.append(ISignal(sess.open_utc, sym, "skip", "excluded",
                                               res.excluded[sym]))
                continue
            usable[sym] = sb

        positions: dict[str, _Pos] = {}
        orders: dict[str, _Order] = {}
        entries: dict[str, int] = dict.fromkeys(usable, 0)
        last_px: dict[str, float] = {}
        seen: dict[str, list[IBar]] = {s: [] for s in usable}
        stats: dict[str, RunningStats] = {s: RunningStats() for s in usable}
        day_halted = False
        timeline = sorted({b.ts for sb in usable.values() for b in sb})
        by_ts = {s: {b.ts: b for b in sb} for s, sb in usable.items()}
        last_slot = sess.close_utc - BAR  # the session's final 5-minute bar

        def equity() -> float:
            return cash + sum(p.shares * last_px.get(p.symbol, p.entry_price)
                              for p in positions.values())

        def close_pos(p: _Pos, ts: datetime, ref: float, reason: str, *, stop: bool) -> None:
            nonlocal cash
            px = self.costs.sell_price(ref, stop=stop)
            fees = self.costs.fees(sell=True, shares=p.shares, price=px)
            cash += p.shares * px - fees
            res.fills.append(IFill(ts, p.symbol, "sell", p.shares, ref, px, fees, reason))
            pnl = (px - p.entry_price) * p.shares - fees - p.entry_fees
            cost = p.shares * ((p.entry_price - p.entry_ref) + (ref - px)) + fees + p.entry_fees
            res.trades.append(ITrade(
                day, p.symbol, self.strategy.id, p.entry_ts, ts, p.shares, p.entry_price, px,
                pnl, cost, pnl / (p.entry_price * p.shares),
                pnl / p.initial_risk if p.initial_risk > 0 else math.nan, p.reason, reason))
            del positions[p.symbol]

        # Each 5-minute timestamp is processed in phases, across ALL symbols at once, so the
        # order of the symbol list can never change a result:
        #   A) fills at the open for every fund, then resting stops/targets for every fund
        #   B) mark every symbol at its close, then check the bot's limits
        #   C) end-of-day exit, then strategy decisions on the bar that just closed
        for ts in timeline:
            now_bars = {s: by_ts[s][ts] for s in self.symbols if s in by_ts and ts in by_ts[s]}
            # A1) orders fill at this bar's open: exits first, then entries in alphabetical
            #     order (a fixed rule, independent of the fund list), all before any price
            #     movement inside the bar is known
            pending = [(sym, orders.pop(sym)) for sym in sorted(now_bars) if sym in orders]
            for sym, order in sorted(pending, key=lambda x: (x[1].kind != "exit", x[0])):
                bar = now_bars[sym]
                if order.kind == "enter" and order.limit is not None:
                    self._resting_limit(res, positions, orders, order, bar, ts, budget, cash,
                                        equity, "paused_by_owner" if is_paused(ts) else
                                        "daily_loss_halt" if day_halted else
                                        "peak_drawdown_halt" if latched else None)
                    if sym in positions and positions[sym].entry_ts == ts:
                        p = positions[sym]
                        cash -= p.entry_price * p.shares + p.entry_fees
                        entries[sym] += 1
                elif ts - order.placed > MAX_FILL_DELAY:
                    res.signals.append(ISignal(ts, sym, order.kind, "cancelled",
                                               "no price bar right after the signal"))
                elif order.kind == "exit" and sym in positions:
                    close_pos(positions[sym], ts, bar.open, order.reason, stop=False)
                elif order.kind == "enter":
                    why = ("paused_by_owner" if is_paused(ts) else "daily_loss_halt"
                           if day_halted else "peak_drawdown_halt" if latched else None)
                    if why:  # limits are re-checked at the moment of the fill too
                        res.signals.append(ISignal(ts, sym, "enter", "cancelled",
                                                   f"{order.reason}; {why}"))
                    else:
                        self._try_enter(res, positions, order, bar, ts, budget, cash, equity)
                        if sym in positions:
                            p = positions[sym]
                            cash -= p.entry_price * p.shares + p.entry_fees
                            entries[sym] += 1
            # A2) then resting stops and targets during the bar
            for sym, bar in now_bars.items():
                p = positions.get(sym)
                if p is not None:
                    if bar.open <= p.stop:
                        close_pos(p, ts, bar.open, "stop_gap", stop=True)
                    elif bar.low <= p.stop:
                        close_pos(p, ts, p.stop, "stop_hit", stop=True)
                    elif p.by_limit and p.entry_ts == ts:
                        pass  # filled on a dip inside this bar: its high may have come before
                    elif p.target is not None and bar.open >= p.target:
                        close_pos(p, ts, bar.open, "target_hit", stop=False)
                    elif p.target is not None and bar.high >= p.target:
                        close_pos(p, ts, p.target, "target_hit", stop=False)
            for sym, bar in now_bars.items():  # B
                last_px[sym] = bar.close
                seen[sym].append(bar)
                stats[sym].add(bar)
            eq = equity()
            if not day_halted and eq <= start_equity * (1 - self.risk.intraday_loss_halt_pct):
                day_halted = True
                res.signals.append(ISignal(ts, "*", "skip", "rejected", "daily_loss_halt",
                                           {"equity": eq, "start": start_equity}))
            if not latched and eq <= peak * (1 - self.risk.peak_drawdown_halt_pct):
                latched = True
                res.signals.append(ISignal(ts, "*", "skip", "rejected", "peak_drawdown_halt",
                                           {"equity": eq, "peak": peak}))
            for sym, bar in now_bars.items():  # C
                if final and ts == last_slot:
                    orders.pop(sym, None)
                    if sym in positions:
                        close_pos(positions[sym], ts, bar.close, "end_of_day", stop=False)
                    continue
                rest = orders.get(sym)
                if rest is not None and rest.limit is not None:  # resting order upkeep
                    gone = ("gap_failed" if rest.cancel_below is not None
                            and bar.close < rest.cancel_below else
                            "too_close_to_the_close" if bar.end >= cutoff else None)
                    if gone:
                        del orders[sym]
                        res.signals.append(ISignal(ts, sym, "enter", "cancelled",
                                                   f"{rest.reason}; {gone}"))
                ctx = StrategyContext(sym, day, seen[sym], prev_close.get(sym),
                                      positions[sym].entry_price if sym in positions else None,
                                      entries[sym], sess.open_utc, sess.close_utc, stats[sym])
                sig = self.strategy.on_bar(ctx)
                if sig is None:
                    continue
                if isinstance(sig, Exit):
                    if sym in positions:
                        orders[sym] = _Order(sym, "exit", ts, sig.reason)
                        res.signals.append(ISignal(ts, sym, "exit", "ordered", sig.reason,
                                                   sig.diagnostics))
                    continue
                if not isinstance(sig, Enter) or sym in positions or sym in orders:
                    continue
                why = None
                if bar.end > cutoff:
                    why = "too_close_to_the_close"
                elif entries[sym] >= self.strategy.max_entries_per_symbol:
                    why = "max_entries_today"
                elif is_paused(bar.end):
                    why = "paused_by_owner"
                elif day_halted:
                    why = "daily_loss_halt"
                elif latched:
                    why = "peak_drawdown_halt"
                elif len(positions) + sum(o.kind == "enter" for o in orders.values()) \
                        >= self.risk.max_open_positions:
                    why = "max_open_positions"
                elif not (sig.stop < (sig.limit if sig.limit is not None else bar.close)):
                    why = "stop_not_below_price"
                if why:
                    res.signals.append(ISignal(ts, sym, "enter", "rejected", f"{sig.reason}; {why}",
                                               sig.diagnostics))
                    continue
                orders[sym] = _Order(sym, "enter", ts, sig.reason, sig.stop, sig.target,
                                     sig.diagnostics, sig.limit, sig.cancel_below)
                res.signals.append(ISignal(ts, sym, "enter", "ordered", sig.reason,
                                           {**sig.diagnostics, "stop": sig.stop,
                                            **({"target": sig.target} if sig.target else {}),
                                            **({"limit": sig.limit} if sig.limit else {})}))

        if final:  # a symbol missing the final bar is sold at its last known price
            for sym in list(positions):
                close_pos(positions[sym], seen[sym][-1].ts, last_px[sym], "end_of_day",
                          stop=False)
            res.end_equity = cash
            state.cash = cash
            state.peak_equity = max(peak, cash)
            if not state.halt_latched and cash <= peak * (1 - self.risk.peak_drawdown_halt_pct):
                latched = True
            state.halt_latched = latched
            state.last_day = day
            state.days += 1
        else:
            res.end_equity = equity()
            res.open_positions = {s: {"shares": p.shares, "entry": p.entry_price, "stop": p.stop,
                                      "target": p.target or 0.0, "last": last_px.get(s, p.entry_price)}
                                  for s, p in positions.items()}
        res.halted = day_halted or latched
        return res

    # ------------------------------------------------------------------------------------

    def _resting_limit(self, res: DayResult, positions: dict[str, _Pos],
                       orders: dict[str, _Order], order: _Order, bar: IBar, ts: datetime,
                       budget: float, cash: float, equity, blocked: str | None) -> None:
        """A resting buy limit during this bar. It fills at the open if the bar opens at or
        below the limit, otherwise at the limit if the bar trades at least a cent through it
        (a touch alone may not fill in a real queue). Unfilled, it keeps resting."""
        limit = float(order.limit)
        if blocked:  # the bot's limits apply to resting orders too
            res.signals.append(ISignal(ts, order.symbol, "enter", "cancelled",
                                       f"{order.reason}; {blocked}"))
            return
        if bar.open <= limit:
            ref = bar.open
        elif bar.low <= limit - LIMIT_TRADE_THROUGH:
            ref = limit
        else:
            orders[order.symbol] = order  # not reached yet: keep waiting
            return
        self._try_enter(res, positions, order, bar, ts, budget, cash, equity, ref=ref)
        if order.symbol in positions:
            positions[order.symbol].by_limit = ref == limit

    def _try_enter(self, res: DayResult, positions: dict[str, _Pos], order: _Order, bar: IBar,
                   ts: datetime, budget: float, cash: float, equity, ref: float | None = None
                   ) -> None:
        """Fill an entry at `ref` (default: this bar's open). `equity()` marks positions at the
        PREVIOUS bar's closes here (phase A runs before phase B), so sizing uses no later prices."""
        sym, stop = order.symbol, float(order.stop or 0.0)
        ref = bar.open if ref is None else ref
        if bar.open <= stop:
            res.signals.append(ISignal(ts, sym, "enter", "cancelled",
                                       f"{order.reason}; opened_below_stop"))
            return
        if order.target is not None and ref >= order.target:
            res.signals.append(ISignal(ts, sym, "enter", "cancelled",
                                       f"{order.reason}; opened_above_target"))
            return
        px = self.costs.buy_price(ref)
        per_share_risk = px - stop + px * GAP_BUFFER  # sized as if the stop could slip a bit
        eq = equity()
        invested = sum(p.entry_price * p.shares for p in positions.values())
        room = max(0.0, eq * self.risk.max_exposure_pct - invested)
        limits = {"risk_budget": budget / per_share_risk, "max_exposure": room / px,
                  "cash": max(cash - 5.0, 0.0) / px}
        binding = min(limits, key=limits.get)
        shares = math.floor(limits[binding])
        while shares >= 1 and shares * px + self.costs.fees(sell=False, shares=shares,
                                                              price=px) > cash:
            shares -= 1  # never spend more cash than there is, fees included
        if shares < 1:
            why = {"max_exposure": "max_exposure_reached", "cash": "not_enough_cash"}.get(
                binding, "position_too_small")
            res.signals.append(ISignal(ts, sym, "enter", "rejected", f"{order.reason}; {why}"))
            return
        fees = self.costs.fees(sell=False, shares=shares, price=px)
        positions[sym] = _Pos(sym, shares, ts, px, ref, fees, stop, order.target,
                              (px - stop) * shares, order.reason)  # 1R = loss if the stop fills
        res.fills.append(IFill(ts, sym, "buy", shares, ref, px, fees, order.reason))
        res.signals.append(ISignal(ts, sym, "enter", "filled", order.reason,
                                   {"shares": shares, "price": px, "stop": stop}))
