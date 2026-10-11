"""Time-ordered splits, the locked test set, parameter search, and walk-forward evaluation.

Rules enforced in code (not just convention):
  * Splits are contiguous and ordered: train < validation < test.
  * Parameter search refuses any window that overlaps the test window (LeakageError).
  * The test window is evaluated through TestVault, which allows exactly one evaluation per
    strategy spec hash and records it in an append-only ledger. Re-testing a tweaked
    strategy requires a new version, which is visible in the ledger.
  * Walk-forward chooses parameters on each training window only, then records the
    out-of-sample result of the next window. It never sees the test window.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tradinglab.marketdata.bars import BarSeries, data_fingerprint
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.backtest import BacktestConfig, Backtester, BacktestResult, EquityPoint
from tradinglab.research.metrics import Metrics, compute_metrics
from tradinglab.strategies.base import Strategy


class LeakageError(RuntimeError):
    """An operation would let the test window influence research decisions."""


@dataclass(frozen=True)
class Window:
    name: str
    start: date
    end: date

    def overlaps(self, other: Window) -> bool:
        return self.start <= other.end and other.start <= self.end


@dataclass(frozen=True)
class Splits:
    train: Window
    validation: Window
    test: Window


def time_splits(start: date, end: date, train_frac: float = 0.6, val_frac: float = 0.2) -> Splits:
    days = trading_days(start, end)
    if len(days) < 30:
        raise ValueError("range too short to split")
    if not (0 < train_frac < 1 and 0 < val_frac < 1 and train_frac + val_frac < 1):
        raise ValueError("invalid split fractions")
    i_tr = int(len(days) * train_frac)
    i_va = int(len(days) * (train_frac + val_frac))
    return Splits(
        Window("train", days[0], days[i_tr - 1]),
        Window("validation", days[i_tr], days[i_va - 1]),
        Window("test", days[i_va], days[-1]),
    )


StrategyFactory = Callable[..., Strategy]


@dataclass(frozen=True)
class SearchResult:
    params: Mapping[str, Any]
    label: str
    metrics: Metrics
    score: float


def objective(m: Metrics, min_trades: int = 5) -> float:
    """Sharpe, heavily penalised when there are too few trades to mean anything."""
    if m.trades < min_trades or not math.isfinite(m.sharpe):
        return -1e9 + m.trades
    return m.sharpe


def run_window(factory: StrategyFactory, params: Mapping[str, Any],
               data: Mapping[str, BarSeries], window: Window, base: BacktestConfig,
               symbols: Sequence[str] | None = None,
               initial_capital: float | None = None) -> BacktestResult:
    cfg = replace(base, start=window.start, end=window.end,
                  initial_capital=initial_capital or base.initial_capital)
    return Backtester(factory(**params), data, cfg, symbols).run()


def grid_search(factory: StrategyFactory, grid: Mapping[str, Sequence[Any]],
                data: Mapping[str, BarSeries], window: Window, base: BacktestConfig, *,
                forbidden: Window | None, symbols: Sequence[str] | None = None,
                min_trades: int = 5) -> list[SearchResult]:
    """Evaluate every parameter combination on `window`; best first. Never on test data."""
    if forbidden is not None and window.overlaps(forbidden):
        raise LeakageError(f"parameter search window {window} overlaps {forbidden.name} window")
    keys = sorted(grid)
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo, strict=True))
        try:
            strat = factory(**params)
        except ValueError:
            continue  # invalid combination (e.g. fast >= slow)
        res = Backtester(strat, data, replace(base, start=window.start, end=window.end),
                         symbols).run()
        m = compute_metrics(res)
        results.append(SearchResult(params, strat.spec.label, m, objective(m, min_trades)))
    # deterministic ordering: score desc, then parameter tuple asc
    return sorted(results, key=lambda r: (-r.score, tuple(sorted(r.params.items()))))


@dataclass(frozen=True)
class WalkForwardSegment:
    train: Window
    oos: Window
    chosen_params: Mapping[str, Any]
    train_score: float
    oos_metrics: Metrics


@dataclass
class WalkForwardResult:
    segments: list[WalkForwardSegment]
    oos_curve: list[EquityPoint] = field(default_factory=list)

    @property
    def param_stability(self) -> float:
        """Share of segments that picked the most common parameter set (1.0 = stable)."""
        if not self.segments:
            return 0.0
        keys = [json.dumps(dict(s.chosen_params), sort_keys=True) for s in self.segments]
        return max(keys.count(k) for k in set(keys)) / len(keys)


def walk_forward(factory: StrategyFactory, grid: Mapping[str, Sequence[Any]],
                 data: Mapping[str, BarSeries], start: date, end: date, base: BacktestConfig,
                 *, train_days: int, oos_days: int, forbidden: Window | None,
                 symbols: Sequence[str] | None = None, min_trades: int = 5) -> WalkForwardResult:
    """Rolling walk-forward: optimise on [t, t+train), trade [t+train, t+train+oos), step oos.

    Each out-of-sample segment starts flat with the previous segment's ending equity, so the
    stitched curve is a genuine sequence of out-of-sample decisions.
    """
    if forbidden is not None and Window("wf", start, end).overlaps(forbidden):
        raise LeakageError("walk-forward range overlaps the test window")
    days = trading_days(start, end)
    segments: list[WalkForwardSegment] = []
    curve: list[EquityPoint] = []
    capital = base.initial_capital
    i = 0
    while i + train_days + oos_days <= len(days):
        train = Window("train", days[i], days[i + train_days - 1])
        oos = Window("oos", days[i + train_days], days[i + train_days + oos_days - 1])
        ranked = grid_search(factory, grid, data, train, base, forbidden=forbidden,
                             symbols=symbols, min_trades=min_trades)
        if not ranked:
            break
        best = ranked[0]
        res = run_window(factory, best.params, data, oos, base, symbols, initial_capital=capital)
        m = compute_metrics(res)
        segments.append(WalkForwardSegment(train, oos, best.params, best.score, m))
        curve.extend(res.equity_curve)
        capital = res.equity_curve[-1].equity
        i += oos_days
    return WalkForwardResult(segments, curve)


class TestVault:
    """Gatekeeper for the untouched test window. One evaluation per strategy spec, logged."""

    __test__ = False  # not a pytest test class

    def __init__(self, ledger_path: Path, test_window: Window) -> None:
        self.ledger_path = ledger_path.resolve()
        self.window = test_window

    def _ledger(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text().splitlines() if line]

    def already_evaluated(self, spec_hash: str) -> bool:
        """True if this spec was ever evaluated on a window overlapping this test window
        (regardless of data revisions - tweaking data is not a licence to re-test)."""
        for e in self._ledger():
            if e["spec_hash"] != spec_hash:
                continue
            past = Window("past", date.fromisoformat(e["window"][0]),
                          date.fromisoformat(e["window"][1]))
            if past.overlaps(self.window):
                return True
        return False

    def evaluate(self, strategy: Strategy, data: Mapping[str, BarSeries], base: BacktestConfig,
                 *, reason: str, symbols: Sequence[str] | None = None) -> BacktestResult:
        if not reason.strip():
            raise ValueError("a reason is required to use the test window")
        h = strategy.spec.spec_hash
        if self.already_evaluated(h):
            raise LeakageError(
                f"{strategy.spec.label} was already evaluated on the test window. "
                "Re-testing invites overfitting; create a new strategy version instead."
            )
        res = Backtester(strategy, data, replace(base, start=self.window.start,
                                                 end=self.window.end), symbols).run()
        m = compute_metrics(res)
        entry = {
            "at": datetime.now(UTC).isoformat(), "spec_hash": h, "label": strategy.spec.label,
            "params": dict(strategy.spec.params), "window": [self.window.start.isoformat(),
                                                             self.window.end.isoformat()],
            "data_sha256": data_fingerprint(dict(data)),
            "reason": reason, "sharpe": m.sharpe, "total_return": m.total_return,
            "max_drawdown": m.max_drawdown, "trades": m.trades,
        }
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        return res
