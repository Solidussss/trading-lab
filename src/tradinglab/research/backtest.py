"""Research backtester: a loop over trading days that drives the shared simulation core.

The fill model, risk rules, corporate actions and decision logic all live in
tradinglab.sim.core so that backtests and the live paper engine behave identically.
This module only adds what is specific to a historical run: a fixed date range, automatic
drawdown-halt resume (modelling your manual resume), and liquidation on the final day.

No-look-ahead guarantees:
  * Strategies only receive BarSeries.history_through(D).
  * Orders created at D are only filled with D+1 prices.
  * Sizing uses information available at close(D).
  * Tests mutate post-D data and assert every decision through D is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date

from tradinglab.engine.risk import RiskParams
from tradinglab.marketdata.bars import BarSeries, DataIssue
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.costs import CostModel
from tradinglab.sim.core import (  # re-exported for research code
    Controls,
    Decision,
    EquityPoint,
    Fill,
    PortfolioState,
    SimulationCore,
    Trade,
)
from tradinglab.strategies.base import Strategy

__all__ = ["BacktestConfig", "BacktestResult", "Backtester", "Decision", "EquityPoint",
           "Fill", "Trade"]


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    initial_capital: float = 10_000.0
    risk: RiskParams = field(default_factory=RiskParams)
    costs: CostModel = field(default_factory=CostModel)


@dataclass
class BacktestResult:
    strategy_label: str
    config: BacktestConfig
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    fills: list[Fill]
    decisions: list[Decision]
    data_issues: list[DataIssue]
    dividends: float = 0.0

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy_label,
            "config": {
                "start": self.config.start.isoformat(), "end": self.config.end.isoformat(),
                "initial_capital": self.config.initial_capital,
                "risk": asdict(self.config.risk), "costs": asdict(self.config.costs),
            },
            "equity_curve": [{**asdict(p), "day": p.day.isoformat()} for p in self.equity_curve],
            "trades": [{**asdict(t), "entry_day": t.entry_day.isoformat(),
                        "exit_day": t.exit_day.isoformat()} for t in self.trades],
        }


class Backtester:
    def __init__(self, strategy: Strategy, data: Mapping[str, BarSeries],
                 config: BacktestConfig, symbols: Sequence[str] | None = None) -> None:
        if config.end < config.start:
            raise ValueError("end before start")
        self.strategy = strategy
        self.data = data
        self.cfg = config
        self.symbols = list(symbols or data.keys())
        missing = [s for s in self.symbols if s not in data]
        if missing:
            raise ValueError(f"no data for {missing}")

    def run(self) -> BacktestResult:
        cfg = self.cfg
        days = trading_days(cfg.start, cfg.end)
        if not days:
            raise ValueError("no trading days in range")
        core = SimulationCore(self.strategy, self.symbols, cfg.risk, cfg.costs,
                              drawdown_resume="auto")
        state = PortfolioState.initial(cfg.initial_capital)
        for s in self.symbols:  # seed marks from data before the start
            hist = self.data[s].history_through(days[0])
            if hist:
                state.last_close[s] = hist[-1].close

        def history(sym: str, d: date):
            return self.data[sym].history_through(d)

        curve: list[EquityPoint] = []
        fills: list[Fill] = []
        trades: list[Trade] = []
        decisions: list[Decision] = []
        dividends = 0.0
        for k, day in enumerate(days):
            last = k == len(days) - 1
            bars = {s: self.data[s].bar_on(day) for s in self.symbols}
            res = core.step(state, day, bars, history,
                            Controls(allow_new_orders=not last))
            if last:
                end = core.liquidate_all(state, day, "end_of_backtest")
                res.fills += end.fills
                res.trades += end.trades
                res.equity = EquityPoint(day, state.cash, state.cash, 0.0, 0,
                                         res.equity.halted if res.equity else False)
            curve.append(res.equity)
            fills += res.fills
            trades += res.trades
            decisions += res.decisions
            dividends += res.dividends

        issues = [i for s in self.symbols for i in self.data[s].issues
                  if i.day is None or cfg.start <= i.day <= cfg.end]
        return BacktestResult(self.strategy.spec.label, cfg, curve, trades, fills, decisions,
                              issues, dividends)
