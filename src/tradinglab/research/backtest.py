"""Event-driven daily backtester with next-open execution and resting protective stops.

Timeline for each trading day D (this is also exactly how the live engine will run):

  open(D)   pending orders decided at close(D-1) fill at D's open, with spread+slippage.
            Exits fill before entries. An entry whose open is already at/below its stop
            is stopped out immediately at the open (gap risk is realised, not skipped).
  intraday  resting stops: if open(D) <= stop -> filled at the open (gap through stop);
            elif low(D) <= stop -> filled at the stop. Both pay stop slippage.
  close(D)  mark to market, update peak equity and halt state.
  after     strategy sees bars through D only, emits signals; the risk engine sizes
            entries using close(D) as the estimated entry price; orders queue for D+1.

No-look-ahead guarantees:
  * Strategies only receive BarSeries.history_through(D).
  * Orders created at D are only filled with D+1 prices.
  * Sizing uses information available at close(D).
  * Tests mutate post-D data and assert every decision through D is unchanged.

Known simplifications (all conservative or neutral; see docs/RESEARCH.md):
  * The intraday-loss halt is evaluated on closing equity (daily bars have no intraday path).
  * When a bar is missing for a held symbol, the position is marked at its last close and
    any pending order for it is cancelled (day orders expire).
  * Positions still open at the end are closed at the final close with exit costs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date

from tradinglab.engine.risk import RiskParams, evaluate_halts, size_long_position
from tradinglab.marketdata.bars import BarSeries, DataIssue
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.costs import CostModel
from tradinglab.strategies.base import Action, PositionState, Strategy


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    initial_capital: float = 10_000.0
    risk: RiskParams = field(default_factory=RiskParams)
    costs: CostModel = field(default_factory=CostModel)


@dataclass
class _Pos:
    symbol: str
    shares: int
    entry_day: date
    entry_price: float  # fill price incl. costs
    entry_ref: float  # open price before costs
    stop: float
    initial_stop: float
    highest_close: float
    bars_held: int = 0
    entry_fees: float = 0.0
    entry_reason: str = ""


@dataclass(frozen=True)
class _Pending:
    symbol: str
    action: Action
    shares: int
    stop: float | None
    decided_on: date
    reason: str
    limit: float | None = None  # entries: on-open limit price


@dataclass(frozen=True)
class Fill:
    day: date
    symbol: str
    side: str
    shares: int
    ref_price: float
    fill_price: float
    fees: float
    slippage_cost: float
    reason: str


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_day: date
    exit_day: date
    shares: int
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    r_multiple: float
    bars_held: int
    costs: float
    entry_reason: str
    exit_reason: str


@dataclass(frozen=True)
class Decision:
    day: date
    symbol: str
    action: str  # strategy action
    outcome: str  # order_queued | rejected | none | data_gap | order_cancelled
    reason: str
    shares: int = 0
    stop: float | None = None
    diagnostics: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class EquityPoint:
    day: date
    equity: float
    cash: float
    exposure: float  # market value of positions / equity
    open_positions: int
    halted: bool


@dataclass
class BacktestResult:
    strategy_label: str
    config: BacktestConfig
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    fills: list[Fill]
    decisions: list[Decision]
    data_issues: list[DataIssue]

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_label,
            "config": {
                "start": self.config.start.isoformat(),
                "end": self.config.end.isoformat(),
                "initial_capital": self.config.initial_capital,
                "risk": asdict(self.config.risk),
                "costs": asdict(self.config.costs),
            },
            "equity_curve": [
                {**asdict(p), "day": p.day.isoformat()} for p in self.equity_curve
            ],
            "trades": [
                {**asdict(t), "entry_day": t.entry_day.isoformat(),
                 "exit_day": t.exit_day.isoformat()} for t in self.trades
            ],
        }


class Backtester:
    def __init__(self, strategy: Strategy, data: Mapping[str, BarSeries],
                 config: BacktestConfig, symbols: Sequence[str] | None = None) -> None:
        if config.end < config.start:
            raise ValueError("end before start")
        self.strategy = strategy
        self.data = data
        self.cfg = config
        self.symbols = list(symbols or data.keys())  # order = deterministic entry priority
        missing = [s for s in self.symbols if s not in data]
        if missing:
            raise ValueError(f"no data for {missing}")

    def run(self) -> BacktestResult:
        cfg, costs = self.cfg, self.cfg.costs
        cash = cfg.initial_capital
        positions: dict[str, _Pos] = {}
        pending: list[_Pending] = []
        last_close: dict[str, float] = {}
        fills: list[Fill] = []
        trades: list[Trade] = []
        decisions: list[Decision] = []
        curve: list[EquityPoint] = []
        peak = prev_equity = cfg.initial_capital
        dd_halt_since: int | None = None

        days = trading_days(cfg.start, cfg.end)
        if not days:
            raise ValueError("no trading days in range")

        # seed last_close with data before start (for marking positions on gap days)
        for s in self.symbols:
            hist = self.data[s].history_through(days[0])
            if hist:
                last_close[s] = hist[-1].close

        def close_position(p: _Pos, day: date, ref: float, fill_px: float, reason: str) -> None:
            nonlocal cash
            fees = costs.fees(sell=True, shares=p.shares, price=fill_px)
            cash += p.shares * fill_px - fees
            fills.append(Fill(day, p.symbol, "sell", p.shares, ref, fill_px, fees,
                              (ref - fill_px) * p.shares, reason))
            pnl = (fill_px - p.entry_price) * p.shares - fees - p.entry_fees
            init_risk = (p.entry_price - p.initial_stop) * p.shares
            trades.append(Trade(
                p.symbol, p.entry_day, day, p.shares, p.entry_price, fill_px, pnl,
                fill_px / p.entry_price - 1.0,
                pnl / init_risk if init_risk > 0 else float("nan"),
                p.bars_held, fees + p.entry_fees + (p.entry_price - p.entry_ref) * p.shares
                + (ref - fill_px) * p.shares,
                p.entry_reason, reason,
            ))
            del positions[p.symbol]

        for k, day in enumerate(days):
            is_last = k == len(days) - 1
            todays = {s: self.data[s].bar_on(day) for s in self.symbols}

            # ---- 1. open: execute yesterday's decisions (exits first) -----------------
            for order in sorted(pending, key=lambda o: o.action != Action.EXIT):
                bar = todays[order.symbol]
                if bar is None:
                    decisions.append(Decision(day, order.symbol, order.action.value,
                                              "order_cancelled", "data_gap_at_open"))
                    continue
                if order.action == Action.EXIT:
                    p = positions.get(order.symbol)
                    if p is not None:
                        close_position(p, day, bar.open, costs.sell_price(bar.open),
                                       order.reason)
                    continue
                # entry: market-on-open LIMIT order; no fill if the open gaps above it
                assert order.limit is not None
                if bar.open > order.limit:
                    decisions.append(Decision(day, order.symbol, order.action.value,
                                              "order_cancelled",
                                              f"open {bar.open:.2f} above limit {order.limit:.2f}"))
                    continue
                fill_px = min(costs.buy_price(bar.open), order.limit)
                fees = costs.fees(sell=False, shares=order.shares, price=fill_px)
                shares = order.shares
                if shares * fill_px + fees > cash:  # gap up: never borrow
                    shares = int(max(cash - fees, 0) // fill_px)
                if shares <= 0 or order.symbol in positions:
                    decisions.append(Decision(day, order.symbol, order.action.value,
                                              "order_cancelled", "insufficient_cash_at_open"))
                    continue
                fees = costs.fees(sell=False, shares=shares, price=fill_px)
                cash -= shares * fill_px + fees
                assert order.stop is not None
                fills.append(Fill(day, order.symbol, "buy", shares, bar.open, fill_px, fees,
                                  (fill_px - bar.open) * shares, order.reason))
                positions[order.symbol] = _Pos(
                    order.symbol, shares, day, fill_px, bar.open, order.stop, order.stop,
                    highest_close=bar.open, entry_fees=fees, entry_reason=order.reason,
                )
            pending = []

            # ---- 2. intraday: resting protective stops ---------------------------------
            for sym in list(positions):
                p = positions[sym]
                bar = todays[sym]
                if bar is None:
                    continue
                if bar.open <= p.stop:
                    close_position(p, day, bar.open, costs.sell_price(bar.open, stop=True),
                                   "stop_gap_at_open")
                elif bar.low <= p.stop:
                    close_position(p, day, p.stop, costs.sell_price(p.stop, stop=True),
                                   "stop_hit")

            # ---- 3. close: mark to market --------------------------------------------
            for sym, bar in todays.items():
                if bar is not None:
                    last_close[sym] = bar.close
            mv = sum(p.shares * last_close[p.symbol] for p in positions.values())
            equity = cash + mv
            halts = evaluate_halts(equity=equity, prior_close_equity=prev_equity,
                                   peak_equity=peak, params=cfg.risk)
            if halts.peak_drawdown_halt:
                if dd_halt_since is None:
                    dd_halt_since = k
                elif k - dd_halt_since >= cfg.risk.drawdown_halt_cooloff_days:
                    # models the manual resume: the halted drawdown becomes the new baseline
                    decisions.append(Decision(day, "*", "none", "halt_reset",
                                              f"drawdown halt lifted after "
                                              f"{k - dd_halt_since} sessions"))
                    peak, dd_halt_since = equity, None
                    halts = evaluate_halts(equity=equity, prior_close_equity=prev_equity,
                                           peak_equity=peak, params=cfg.risk)
            else:
                dd_halt_since = None
            peak = max(peak, equity)

            # ---- 4. after close: strategy decisions for tomorrow -----------------------
            reserved_cash = 0.0
            pending_exposure = 0.0
            pending_entries = 0
            for sym in self.symbols:
                bar = todays[sym]
                p = positions.get(sym)
                if bar is None:
                    decisions.append(Decision(day, sym, "none", "data_gap", "no_bar_today"))
                    continue
                if p is not None:
                    p.bars_held += 1
                    p.highest_close = max(p.highest_close, bar.close)
                state = PositionState(
                    in_position=p is not None,
                    entry_price=p.entry_price if p else None,
                    stop_price=p.stop if p else None,
                    bars_held=p.bars_held if p else 0,
                    highest_close=p.highest_close if p else None,
                )
                sig = self.strategy.evaluate(self.data[sym].history_through(day), state)

                if is_last:
                    decisions.append(Decision(day, sym, sig.action.value, "none",
                                              "final_day_no_new_orders", diagnostics=sig.diagnostics))
                    continue

                if sig.action == Action.HOLD and p is not None:
                    if sig.stop_price is not None and sig.stop_price > p.stop:
                        p.stop = sig.stop_price  # stops only ratchet up
                    decisions.append(Decision(day, sym, sig.action.value, "none", sig.reason,
                                              stop=p.stop, diagnostics=sig.diagnostics))
                elif sig.action == Action.EXIT and p is not None:
                    pending.append(_Pending(sym, Action.EXIT, p.shares, None, day, sig.reason))
                    decisions.append(Decision(day, sym, sig.action.value, "order_queued",
                                              sig.reason, shares=p.shares,
                                              diagnostics=sig.diagnostics))
                elif sig.action == Action.ENTER_LONG and p is None:
                    if halts.entries_blocked:
                        decisions.append(Decision(day, sym, sig.action.value, "rejected",
                                                  ";".join(halts.reasons()),
                                                  diagnostics=sig.diagnostics))
                        continue
                    assert sig.stop_price is not None
                    exposure = mv + pending_exposure
                    limit = bar.close * (1 + cfg.risk.entry_limit_pct)
                    sizing = size_long_position(  # sized at the worst possible fill: the limit
                        equity=equity, cash=cash - reserved_cash,
                        entry_price=limit, stop_price=sig.stop_price,
                        current_exposure_usd=exposure,
                        open_positions=len(positions) + pending_entries, params=cfg.risk,
                    )
                    if sizing.shares <= 0:
                        decisions.append(Decision(day, sym, sig.action.value, "rejected",
                                                  f"sizing:{sizing.binding_constraint}",
                                                  stop=sig.stop_price,
                                                  diagnostics=sig.diagnostics))
                        continue
                    est = sizing.shares * limit
                    reserved_cash += est
                    pending_exposure += est
                    pending_entries += 1
                    pending.append(_Pending(sym, Action.ENTER_LONG, sizing.shares,
                                            sig.stop_price, day, sig.reason, limit))
                    decisions.append(Decision(day, sym, sig.action.value, "order_queued",
                                              f"{sig.reason}; sized_by={sizing.binding_constraint}",
                                              shares=sizing.shares, stop=sig.stop_price,
                                              diagnostics=sig.diagnostics))
                else:
                    decisions.append(Decision(day, sym, sig.action.value, "none", sig.reason,
                                              diagnostics=sig.diagnostics))

            # ---- 5. final day: liquidate at the close ----------------------------------
            if is_last:
                for sym in list(positions):
                    ref = last_close[sym]
                    close_position(positions[sym], day, ref, costs.sell_price(ref),
                                   "end_of_backtest")
                mv = 0.0
                equity = cash

            curve.append(EquityPoint(day, equity, cash, mv / equity if equity > 0 else 0.0,
                                     len(positions), halts.entries_blocked))
            prev_equity = equity
            if not math.isfinite(equity):
                raise FloatingPointError(f"equity became non-finite on {day}")

        issues = [i for s in self.symbols for i in self.data[s].issues
                  if i.day is None or cfg.start <= i.day <= cfg.end]
        return BacktestResult(self.strategy.spec.label, cfg, curve, trades, fills, decisions,
                              issues)
