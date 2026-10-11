"""Performance metrics, benchmarks, attribution, and drawdown analysis."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from tradinglab.marketdata.bars import BarSeries
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.backtest import BacktestResult, EquityPoint, Trade
from tradinglab.research.costs import CostModel

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Metrics:
    start: date
    end: date
    days: int
    total_return: float
    cagr: float
    ann_vol: float
    sharpe: float
    sortino: float
    max_drawdown: float
    max_dd_days: int
    calmar: float
    trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float
    avg_r: float
    avg_exposure: float
    total_costs: float
    halted_days: int


def daily_returns(curve: Sequence[EquityPoint]) -> list[float]:
    return [curve[i].equity / curve[i - 1].equity - 1.0 for i in range(1, len(curve))]


def drawdown_series(equity: Sequence[float]) -> list[float]:
    peak, out = -math.inf, []
    for e in equity:
        peak = max(peak, e)
        out.append(e / peak - 1.0 if peak > 0 else 0.0)
    return out


def max_drawdown(equity: Sequence[float]) -> tuple[float, int]:
    """(max drawdown as a positive fraction, longest underwater stretch in trading days)."""
    dd = drawdown_series(equity)
    worst = -min(dd) if dd else 0.0
    longest = cur = 0
    for x in dd:
        cur = cur + 1 if x < 0 else 0
        longest = max(longest, cur)
    return worst, longest


def compute_metrics(result: BacktestResult, risk_free_rate: float = 0.0) -> Metrics:
    curve = result.equity_curve
    eq = [p.equity for p in curve]
    rets = daily_returns(curve)
    n = len(curve)
    total = eq[-1] / result.config.initial_capital - 1.0 if eq else 0.0
    years = max(n / TRADING_DAYS_PER_YEAR, 1e-9)
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    rf_d = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = [r - rf_d for r in rets]
    vol = statistics.pstdev(rets) * math.sqrt(TRADING_DAYS_PER_YEAR) if len(rets) > 1 else 0.0
    mean_ex = statistics.fmean(excess) if excess else 0.0
    sd = statistics.pstdev(excess) if len(excess) > 1 else 0.0
    sharpe = mean_ex / sd * math.sqrt(TRADING_DAYS_PER_YEAR) if sd > 0 else 0.0
    downside = [min(r, 0.0) for r in excess]
    dsd = math.sqrt(statistics.fmean([d * d for d in downside])) if downside else 0.0
    sortino = mean_ex / dsd * math.sqrt(TRADING_DAYS_PER_YEAR) if dsd > 0 else 0.0
    mdd, mdd_days = max_drawdown(eq)

    t = result.trades
    wins = [x.pnl for x in t if x.pnl > 0]
    losses = [x.pnl for x in t if x.pnl <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    rs = [x.r_multiple for x in t if math.isfinite(x.r_multiple)]
    return Metrics(
        start=curve[0].day, end=curve[-1].day, days=n, total_return=total, cagr=cagr,
        ann_vol=vol, sharpe=sharpe, sortino=sortino, max_drawdown=mdd, max_dd_days=mdd_days,
        calmar=cagr / mdd if mdd > 0 else 0.0, trades=len(t),
        win_rate=len(wins) / len(t) if t else 0.0,
        avg_win=statistics.fmean(wins) if wins else 0.0,
        avg_loss=statistics.fmean(losses) if losses else 0.0,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else (math.inf if wins else 0.0),
        expectancy=statistics.fmean([x.pnl for x in t]) if t else 0.0,
        avg_r=statistics.fmean(rs) if rs else 0.0,
        avg_exposure=statistics.fmean([p.exposure for p in curve]) if curve else 0.0,
        total_costs=sum(x.costs for x in t),
        halted_days=sum(1 for p in curve if p.halted),
    )


# --- benchmarks ---------------------------------------------------------------------------


def buy_and_hold(series: BarSeries, start: date, end: date, capital: float,
                 fraction: float = 1.0, costs: CostModel | None = None) -> list[EquityPoint]:
    """Buy `fraction` of capital at the first open in range, hold, mark at each close.
    Cash dividends are credited (not reinvested) and splits rescale shares.

    fraction=1.0 is the classic benchmark; fraction equal to the strategy's exposure cap
    (0.30) is the fair comparison for a strategy that can never be more than 30% invested.
    """
    costs = costs or CostModel()
    days = [d for d in trading_days(start, end) if series.bar_on(d) is not None]
    if not days:
        return []
    first = series.bar_on(days[0])
    assert first is not None
    px = costs.buy_price(first.open)
    shares = math.floor(capital * fraction / px)
    cash = capital - shares * px
    out = []
    for i, d in enumerate(days):
        bar = series.bar_on(d)
        assert bar is not None
        if i > 0:  # same corporate-action treatment as the simulator
            if bar.split != 1.0:
                shares = math.floor(shares * bar.split + 1e-9)
            cash += shares * bar.dividend
        mv = shares * bar.close
        eq = cash + mv
        out.append(EquityPoint(d, eq, cash, mv / eq, 1 if shares else 0, False))
    return out


@dataclass(frozen=True)
class BenchmarkStats:
    name: str
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float


def benchmark_stats(name: str, curve: Sequence[EquityPoint]) -> BenchmarkStats:
    eq = [p.equity for p in curve]
    rets = daily_returns(curve)
    total = eq[-1] / eq[0] - 1.0
    years = max(len(eq) / TRADING_DAYS_PER_YEAR, 1e-9)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = statistics.fmean(rets) / sd * math.sqrt(TRADING_DAYS_PER_YEAR) if sd > 0 else 0.0
    return BenchmarkStats(name, total, (1 + total) ** (1 / years) - 1, sharpe,
                          max_drawdown(eq)[0])


# --- attribution & loss analysis ----------------------------------------------------------


def attribution(trades: Sequence[Trade]) -> dict[str, dict[str, float]]:
    """P&L split by symbol, by exit reason, and by year."""
    out: dict[str, dict[str, float]] = {"by_symbol": defaultdict(float),
                                         "by_exit_reason": defaultdict(float),
                                         "by_year": defaultdict(float)}
    for t in trades:
        out["by_symbol"][t.symbol] += t.pnl
        out["by_exit_reason"][t.exit_reason.split(":")[0]] += t.pnl
        out["by_year"][str(t.exit_day.year)] += t.pnl
    return {k: dict(sorted(v.items())) for k, v in out.items()}


def worst_trades(trades: Sequence[Trade], n: int = 5) -> list[Trade]:
    return sorted(trades, key=lambda t: t.pnl)[:n]


@dataclass(frozen=True)
class DrawdownEpisode:
    peak_day: date
    trough_day: date
    recovery_day: date | None
    depth: float
    length_days: int


def drawdown_episodes(curve: Sequence[EquityPoint], min_depth: float = 0.02) -> list[DrawdownEpisode]:
    episodes: list[DrawdownEpisode] = []
    peak_eq, peak_i = curve[0].equity, 0
    trough_eq, trough_i = peak_eq, 0
    in_dd = False
    for i, p in enumerate(curve):
        if p.equity >= peak_eq:
            if in_dd and 1 - trough_eq / peak_eq >= min_depth:
                episodes.append(DrawdownEpisode(curve[peak_i].day, curve[trough_i].day, p.day,
                                                1 - trough_eq / peak_eq, i - peak_i))
            peak_eq, peak_i, trough_eq, trough_i, in_dd = p.equity, i, p.equity, i, False
        else:
            in_dd = True
            if p.equity < trough_eq:
                trough_eq, trough_i = p.equity, i
    if in_dd and 1 - trough_eq / peak_eq >= min_depth:
        episodes.append(DrawdownEpisode(curve[peak_i].day, curve[trough_i].day, None,
                                        1 - trough_eq / peak_eq, len(curve) - 1 - peak_i))
    return sorted(episodes, key=lambda e: -e.depth)
