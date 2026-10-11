"""The simulation core: one trading session at a time, from persisted state.

Both the research backtester and the live paper engine call `SimulationCore.step()`, so a
backtest and the live simulation make identical decisions and fills from identical data.

Timeline for session D (all prices are RAW, as traded; strategies see split-adjusted views):

  0. corporate actions  splits rescale positions/pending orders; cash dividends going ex on
                        D are credited for shares held at the previous close.
  1. open               orders decided at close(D-1) execute at open(D) with spread +
                        slippage. Exits before entries. Entries are on-open LIMIT orders at
                        close(D-1) x (1 + entry_limit_pct): no fill if the open is above it.
                        An entry opening at/below its stop is stopped out at the open.
  2. intraday stops     open <= stop -> fill at the open (gap); low <= stop -> fill at stop.
  3. close              mark to market; update peak equity and halts.
  4. decisions          the strategy sees bars through D only; entries are sized at their
                        worst possible fill (the limit); orders queue for D+1.

State is a plain, JSON-serialisable PortfolioState so the live engine can persist it after
every session and resume exactly after a restart.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from tradinglab.broker.base import make_client_order_id
from tradinglab.engine.risk import RiskParams, evaluate_halts, size_long_position
from tradinglab.marketdata.bars import Bar
from tradinglab.research.costs import CostModel
from tradinglab.strategies.base import Action, PositionState, Strategy

# --- records produced by a step -----------------------------------------------------------


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
    order_id: str = ""


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_day: date
    exit_day: date
    shares: int
    entry_price: float
    exit_price: float
    pnl: float  # includes fees, slippage and dividends received while held
    return_pct: float
    r_multiple: float
    bars_held: int
    costs: float
    entry_reason: str
    exit_reason: str
    dividends: float = 0.0
    strategy: str = ""


@dataclass(frozen=True)
class Decision:
    day: date
    symbol: str
    action: str  # strategy action, or "none" for engine events
    outcome: str  # order_queued|rejected|none|data_gap|order_cancelled|halt_reset|corporate_action
    reason: str
    shares: int = 0
    stop: float | None = None
    diagnostics: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class EquityPoint:
    day: date
    equity: float
    cash: float
    exposure: float
    open_positions: int
    halted: bool


@dataclass
class StepResult:
    day: date
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    equity: EquityPoint | None = None
    dividends: float = 0.0


@dataclass(frozen=True)
class Controls:
    paused: bool = False  # emergency pause: no new entries; exits and stops still run
    allow_new_orders: bool = True  # False on a backtest's final day


# --- persisted state ----------------------------------------------------------------------


@dataclass
class SimPosition:
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
    dividends: float = 0.0
    entry_reason: str = ""
    strategy: str = ""


@dataclass
class PendingOrder:
    order_id: str
    symbol: str
    action: str  # enter_long | exit
    shares: int
    decided_on: date
    reason: str
    stop: float | None = None
    limit: float | None = None
    strategy: str = ""


@dataclass
class PortfolioState:
    cash: float
    initial_capital: float
    positions: dict[str, SimPosition] = field(default_factory=dict)
    pending: list[PendingOrder] = field(default_factory=list)
    last_close: dict[str, float] = field(default_factory=dict)
    peak_equity: float = 0.0
    prev_equity: float = 0.0
    session_index: int = 0
    last_session: date | None = None
    dd_halt_since: int | None = None
    drawdown_halt_latched: bool = False
    realized_pnl: float = 0.0
    dividends_received: float = 0.0
    config_fingerprint: str = ""  # symbols + strategy spec + risk + costs it was run with

    @classmethod
    def initial(cls, capital: float) -> PortfolioState:
        return cls(cash=capital, initial_capital=capital, peak_equity=capital,
                   prev_equity=capital)

    def equity(self) -> float:
        return self.cash + sum(p.shares * self.last_close.get(p.symbol, p.entry_ref)
                               for p in self.positions.values())

    # JSON round trip (dates as ISO strings) ---------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        def enc(o: Any) -> Any:
            if isinstance(o, date):
                return o.isoformat()
            if isinstance(o, dict):
                return {k: enc(v) for k, v in o.items()}
            if isinstance(o, list):
                return [enc(v) for v in o]
            return o

        return enc(asdict(self))

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PortfolioState:
        def day(v: str | None) -> date | None:
            return date.fromisoformat(v) if v else None

        positions = {}
        for k, p in d["positions"].items():
            positions[k] = SimPosition(**{**p, "entry_day": day(p["entry_day"])})
        pending = [PendingOrder(**{**o, "decided_on": day(o["decided_on"])})
                   for o in d["pending"]]
        known = {f for f in cls.__dataclass_fields__}
        rest = {k: v for k, v in d.items()
                if k in known and k not in ("positions", "pending", "last_session")}
        return cls(positions=positions, pending=pending, last_session=day(d.get("last_session")),
                   **rest)


HistoryFn = Callable[[str, date], Sequence[Bar]]


class SimulationCore:
    def __init__(self, strategy: Strategy, symbols: Sequence[str], risk: RiskParams,
                 costs: CostModel, *, drawdown_resume: str = "auto") -> None:
        if drawdown_resume not in ("auto", "manual"):
            raise ValueError("drawdown_resume must be 'auto' (backtests) or 'manual' (live)")
        self.strategy = strategy
        self.symbols = list(symbols)  # order = deterministic entry priority
        self.risk = risk
        self.costs = costs
        self.drawdown_resume = drawdown_resume

    # ------------------------------------------------------------------------------------

    def step(self, state: PortfolioState, day: date, bars: Mapping[str, Bar | None],
             history: HistoryFn, controls: Controls | None = None) -> StepResult:
        controls = controls or Controls()
        if state.last_session is not None and day <= state.last_session:
            raise ValueError(f"session {day} already processed (last {state.last_session})")
        res = StepResult(day)
        costs, label = self.costs, self.strategy.spec.label
        # Held symbols are always managed (stops, marks, exits) even if they were removed
        # from the configured list; they just never receive new entries.
        symbols = self.symbols + sorted(s for s in state.positions if s not in self.symbols)
        todays = {s: bars.get(s) for s in symbols}

        # ---- 0. corporate actions -----------------------------------------------------
        for sym, bar in todays.items():
            if bar is None:
                continue
            p = state.positions.get(sym)
            if bar.split != 1.0:
                self._apply_split(state, sym, bar, res)
            if bar.dividend > 0 and p is not None and sym in state.positions:
                p = state.positions[sym]
                amount = p.shares * bar.dividend
                state.cash += amount
                p.dividends += amount
                state.dividends_received += amount
                res.dividends += amount
                res.decisions.append(Decision(day, sym, "none", "corporate_action",
                                              f"dividend {bar.dividend:.4f}/sh credited "
                                              f"${amount:.2f}"))

        # ---- 1. open: execute yesterday's decisions (exits first) ---------------------
        for order in sorted(state.pending, key=lambda o: o.action != Action.EXIT.value):
            bar = todays.get(order.symbol)
            if bar is None:
                res.decisions.append(Decision(day, order.symbol, order.action,
                                              "order_cancelled", "data_gap_at_open"))
                continue
            if order.action == Action.EXIT.value:
                p = state.positions.get(order.symbol)
                if p is not None:
                    self._close(state, p, day, bar.open, costs.sell_price(bar.open),
                                order.reason, res, order.order_id)
                continue
            assert order.limit is not None and order.stop is not None
            if bar.open > order.limit:
                res.decisions.append(Decision(day, order.symbol, order.action,
                                              "order_cancelled",
                                              f"open {bar.open:.2f} above limit {order.limit:.2f}"))
                continue
            fill_px = min(costs.buy_price(bar.open), order.limit)
            shares = order.shares
            fees = costs.fees(sell=False, shares=shares, price=fill_px)
            if shares * fill_px + fees > state.cash:  # never borrow
                shares = int(max(state.cash - fees, 0) // fill_px)
            if shares <= 0 or order.symbol in state.positions:
                res.decisions.append(Decision(day, order.symbol, order.action,
                                              "order_cancelled", "insufficient_cash_at_open"))
                continue
            fees = costs.fees(sell=False, shares=shares, price=fill_px)
            state.cash -= shares * fill_px + fees
            res.fills.append(Fill(day, order.symbol, "buy", shares, bar.open, fill_px, fees,
                                  (fill_px - bar.open) * shares, order.reason, order.order_id))
            state.positions[order.symbol] = SimPosition(
                order.symbol, shares, day, fill_px, bar.open, order.stop, order.stop,
                highest_close=bar.open, entry_fees=fees, entry_reason=order.reason,
                strategy=order.strategy,
            )
        state.pending = []

        # ---- 2. intraday: resting protective stops -------------------------------------
        for sym in list(state.positions):
            p, bar = state.positions[sym], todays.get(sym)
            if bar is None:
                continue
            if bar.open <= p.stop:
                self._close(state, p, day, bar.open, costs.sell_price(bar.open, stop=True),
                            "stop_gap_at_open", res)
            elif bar.low <= p.stop:
                self._close(state, p, day, p.stop, costs.sell_price(p.stop, stop=True),
                            "stop_hit", res)

        # ---- 3. close: mark to market, halts --------------------------------------------
        for sym, bar in todays.items():
            if bar is not None:
                state.last_close[sym] = bar.close
        mv = sum(p.shares * state.last_close.get(p.symbol, p.entry_ref)
                 for p in state.positions.values())
        equity = state.cash + mv
        halts = evaluate_halts(equity=equity, prior_close_equity=state.prev_equity,
                               peak_equity=state.peak_equity, params=self.risk)
        k = state.session_index
        if halts.peak_drawdown_halt:
            if self.drawdown_resume == "manual":
                state.drawdown_halt_latched = True
            elif state.dd_halt_since is None:
                state.dd_halt_since = k
            elif k - state.dd_halt_since >= self.risk.drawdown_halt_cooloff_days:
                res.decisions.append(Decision(day, "*", "none", "halt_reset",
                                              f"drawdown halt lifted after "
                                              f"{k - state.dd_halt_since} sessions"))
                state.peak_equity, state.dd_halt_since = equity, None
                halts = evaluate_halts(equity=equity, prior_close_equity=state.prev_equity,
                                       peak_equity=state.peak_equity, params=self.risk)
        else:
            state.dd_halt_since = None
        if not state.drawdown_halt_latched:
            state.peak_equity = max(state.peak_equity, equity)
        block_reasons = halts.reasons()
        if state.drawdown_halt_latched and not any("peak" in r for r in block_reasons):
            block_reasons.append("drawdown_halt_latched (manual resume required)")
        if controls.paused:
            block_reasons.append("emergency_pause")
        entries_blocked = bool(block_reasons)

        # ---- 4. after close: decisions for the next session -----------------------------
        reserved_cash = pending_exposure = 0.0
        pending_entries = 0
        for sym in symbols:
            bar, p = todays[sym], state.positions.get(sym)
            if bar is None:
                res.decisions.append(Decision(day, sym, "none", "data_gap", "no_bar_today"))
                continue
            if sym not in self.symbols and p is None:
                continue
            if p is not None:
                p.bars_held += 1
                p.highest_close = max(p.highest_close, bar.close)
            pstate = PositionState(
                in_position=p is not None,
                entry_price=p.entry_price if p else None,
                stop_price=p.stop if p else None,
                bars_held=p.bars_held if p else 0,
                highest_close=p.highest_close if p else None,
            )
            sig = self.strategy.evaluate(history(sym, day), pstate)

            if not controls.allow_new_orders:
                res.decisions.append(Decision(day, sym, sig.action.value, "none",
                                              "no_new_orders", diagnostics=sig.diagnostics))
                continue
            if sig.action == Action.HOLD and p is not None:
                if sig.stop_price is not None and sig.stop_price > p.stop:
                    p.stop = sig.stop_price  # stops only ratchet up
                res.decisions.append(Decision(day, sym, sig.action.value, "none", sig.reason,
                                              stop=p.stop, diagnostics=sig.diagnostics))
            elif sig.action == Action.EXIT and p is not None:
                oid = make_client_order_id(label, sym, day, "exit")
                state.pending.append(PendingOrder(oid, sym, Action.EXIT.value, p.shares, day,
                                                  sig.reason, strategy=label))
                res.decisions.append(Decision(day, sym, sig.action.value, "order_queued",
                                              sig.reason, shares=p.shares,
                                              diagnostics=sig.diagnostics))
            elif sig.action == Action.ENTER_LONG and p is None:
                if sym not in self.symbols:
                    res.decisions.append(Decision(day, sym, sig.action.value, "rejected",
                                                  "symbol_not_configured"))
                    continue
                if entries_blocked:
                    res.decisions.append(Decision(day, sym, sig.action.value, "rejected",
                                                  ";".join(block_reasons),
                                                  diagnostics=sig.diagnostics))
                    continue
                assert sig.stop_price is not None
                limit = bar.close * (1 + self.risk.entry_limit_pct)
                sizing = size_long_position(  # sized at the worst possible fill: the limit
                    equity=equity, cash=state.cash - reserved_cash, entry_price=limit,
                    stop_price=sig.stop_price, current_exposure_usd=mv + pending_exposure,
                    open_positions=len(state.positions) + pending_entries, params=self.risk,
                )
                if sizing.shares <= 0:
                    res.decisions.append(Decision(day, sym, sig.action.value, "rejected",
                                                  f"sizing:{sizing.binding_constraint}",
                                                  stop=sig.stop_price,
                                                  diagnostics=sig.diagnostics))
                    continue
                est = sizing.shares * limit
                reserved_cash += est
                pending_exposure += est
                pending_entries += 1
                oid = make_client_order_id(label, sym, day, "entry")
                state.pending.append(PendingOrder(oid, sym, Action.ENTER_LONG.value,
                                                  sizing.shares, day, sig.reason,
                                                  stop=sig.stop_price, limit=limit,
                                                  strategy=label))
                res.decisions.append(Decision(
                    day, sym, sig.action.value, "order_queued",
                    f"{sig.reason}; sized_by={sizing.binding_constraint}",
                    shares=sizing.shares, stop=sig.stop_price, diagnostics=sig.diagnostics))
            else:
                res.decisions.append(Decision(day, sym, sig.action.value, "none", sig.reason,
                                              diagnostics=sig.diagnostics))

        if not math.isfinite(equity):
            raise FloatingPointError(f"equity became non-finite on {day}")
        res.equity = EquityPoint(day, equity, state.cash, mv / equity if equity > 0 else 0.0,
                                 len(state.positions), entries_blocked)
        state.prev_equity = equity
        state.session_index += 1
        state.last_session = day
        return res

    # ------------------------------------------------------------------------------------

    def liquidate_all(self, state: PortfolioState, day: date, reason: str) -> StepResult:
        """Close every position at its last close with exit costs (end of a backtest)."""
        res = StepResult(day)
        for sym in list(state.positions):
            ref = state.last_close[sym]
            self._close(state, state.positions[sym], day, ref, self.costs.sell_price(ref),
                        reason, res)
        state.pending = []
        res.equity = EquityPoint(day, state.cash, state.cash, 0.0, 0, False)
        state.prev_equity = state.cash
        return res

    @staticmethod
    def skip_session(state: PortfolioState, day: date, reason: str) -> StepResult:
        """Record a session the market did not trade (e.g. an unscheduled closure).
        No fills, no stops, no decisions; pending orders carry over to the next session."""
        if state.last_session is not None and day <= state.last_session:
            raise ValueError(f"session {day} already processed")
        res = StepResult(day)
        res.decisions.append(Decision(day, "*", "none", "session_skipped", reason))
        state.last_session = day
        return res

    @staticmethod
    def resume_after_drawdown(state: PortfolioState) -> None:
        """Manual resume (live): the current equity becomes the new drawdown baseline."""
        state.drawdown_halt_latched = False
        state.dd_halt_since = None
        state.peak_equity = state.prev_equity

    # ------------------------------------------------------------------------------------

    def _close(self, state: PortfolioState, p: SimPosition, day: date, ref: float,
               fill_px: float, reason: str, res: StepResult, order_id: str = "") -> None:
        fees = self.costs.fees(sell=True, shares=p.shares, price=fill_px)
        state.cash += p.shares * fill_px - fees
        res.fills.append(Fill(day, p.symbol, "sell", p.shares, ref, fill_px, fees,
                              (ref - fill_px) * p.shares, reason, order_id))
        pnl = (fill_px - p.entry_price) * p.shares - fees - p.entry_fees + p.dividends
        init_risk = (p.entry_price - p.initial_stop) * p.shares
        res.trades.append(Trade(
            p.symbol, p.entry_day, day, p.shares, p.entry_price, fill_px, pnl,
            fill_px / p.entry_price - 1.0,
            pnl / init_risk if init_risk > 0 else float("nan"),
            p.bars_held,
            fees + p.entry_fees + (p.entry_price - p.entry_ref) * p.shares
            + (ref - fill_px) * p.shares,
            p.entry_reason, reason, p.dividends, p.strategy,
        ))
        state.realized_pnl += pnl
        del state.positions[p.symbol]

    def _apply_split(self, state: PortfolioState, sym: str, bar: Bar, res: StepResult) -> None:
        f = bar.split
        p = state.positions.get(sym)
        if p is not None and math.floor(p.shares * f + 1e-9) <= 0:
            # A reverse split leaves less than one share: paid out entirely as cash in lieu,
            # at the pre-split equivalent of today's open. Recorded as a normal trade.
            ref = bar.open * f
            self._close(state, p, bar.day, ref, ref, "split_cash_in_lieu", res)
            p = None
        if p is not None:
            new_shares = math.floor(p.shares * f + 1e-9)
            cash_in_lieu = (p.shares * f - new_shares) * bar.open
            state.cash += cash_in_lieu
            p.dividends += cash_in_lieu  # income while held: counted in the trade's P&L
            p.shares = new_shares
            for attr in ("entry_price", "entry_ref", "stop", "initial_stop", "highest_close"):
                setattr(p, attr, getattr(p, attr) / f)
        if sym in state.last_close:
            state.last_close[sym] /= f
        for o in state.pending:
            if o.symbol == sym:
                o.shares = math.floor(o.shares * f + 1e-9)
                o.stop = o.stop / f if o.stop else o.stop
                o.limit = o.limit / f if o.limit else o.limit
        res.decisions.append(Decision(bar.day, sym, "none", "corporate_action",
                                      f"split {f:g}:1 applied"))
