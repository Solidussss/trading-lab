"""Known-answer, fill-model, look-ahead, and invariant tests for the backtester."""

import math
import unittest
from dataclasses import dataclass, replace
from datetime import date

from tradinglab.engine.risk import RiskParams
from tradinglab.marketdata.bars import Bar, BarSeries
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.backtest import BacktestConfig, Backtester
from tradinglab.research.costs import ZERO_COST, CostModel
from tradinglab.research.synthetic import synthetic_series
from tradinglab.strategies.base import Action, PositionState, Signal, StrategySpec
from tradinglab.strategies.trend_following import TrendFollowing

DAYS = trading_days(date(2026, 3, 2), date(2026, 3, 31))  # 22 sessions


def flat_series(symbol="SPY", price=100.0, days=DAYS, overrides=None):
    overrides = overrides or {}
    bars = []
    for d in days:
        o, h, lo, c = overrides.get(d, (price, price + 1, price - 1, price))
        bars.append(Bar(d, o, h, lo, c, 1e6))
    return BarSeries(symbol, bars)


@dataclass
class Scripted:
    """Emits pre-scripted signals by date; records what history it was shown."""

    script: dict
    spec: StrategySpec = StrategySpec("scripted", "0", {})
    seen_last_days: list = None

    def __post_init__(self):
        self.seen_last_days = []

    @property
    def warmup_bars(self):
        return 0

    def evaluate(self, history, position: PositionState):
        today = history[-1].day
        self.seen_last_days.append(today)
        sig = self.script.get(today)
        if sig is not None:
            if sig.action == Action.ENTER_LONG and position.in_position:
                return Signal(Action.HOLD, "already_in", position.stop_price)
            if sig.action == Action.EXIT and not position.in_position:
                return Signal(Action.NO_TRADE, "flat")
            return sig
        return Signal(Action.HOLD if position.in_position else Action.NO_TRADE, "scripted_idle",
                      position.stop_price)


def cfg(**kw):
    base = dict(start=DAYS[0], end=DAYS[-1], initial_capital=10_000.0, costs=ZERO_COST,
                risk=RiskParams(stop_gap_buffer_pct=0.0))
    base.update(kw)
    return BacktestConfig(**base)


class FillModelTests(unittest.TestCase):
    def test_entry_at_next_open_and_exit_at_next_open_zero_cost(self):
        d0, d1, d5, d6 = DAYS[0], DAYS[1], DAYS[5], DAYS[6]
        series = flat_series(overrides={d1: (101, 102, 100.5, 101.5), d6: (110, 111, 109, 110)})
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0),
                          d5: Signal(Action.EXIT, "done")})
        res = Backtester(strat, {"SPY": series}, cfg()).run()
        # Sized at the worst fill, the on-open limit 100*1.01=101: $50/(101-95) -> 8 shares
        # (exposure cap would allow 3000/101 = 29).
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual((t.entry_day, t.exit_day, t.shares), (d1, d6, 8))
        self.assertEqual(t.entry_price, 101)  # next day's OPEN, not the signal day's close
        self.assertEqual(t.exit_price, 110)
        self.assertAlmostEqual(t.pnl, 72.0)
        self.assertAlmostEqual(res.equity_curve[-1].equity, 10_072.0)

    def test_gap_up_above_limit_does_not_fill(self):
        # Auditor's probe: a +30% gap used to push exposure to 39%. Now the limit blocks it.
        d0, d1 = DAYS[0], DAYS[1]
        series = flat_series(overrides={d1: (130, 131, 129, 130)})
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=99.0)})
        res = Backtester(strat, {"SPY": series}, cfg()).run()
        self.assertEqual(res.trades, [])
        self.assertTrue(any(x.day == d1 and x.outcome == "order_cancelled"
                            and "above limit" in x.reason for x in res.decisions))
        self.assertLessEqual(max(p.exposure for p in res.equity_curve), 0.30)

    def test_small_gap_within_limit_fills_and_respects_cap(self):
        d0, d1 = DAYS[0], DAYS[1]
        series = flat_series(overrides={d1: (100.9, 101.5, 100.5, 101)})
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=99.9)})
        res = Backtester(strat, {"SPY": series}, cfg()).run()
        self.assertEqual(res.trades[0].entry_price, 100.9)
        first = next(p for p in res.equity_curve if p.day == d1)
        self.assertLessEqual(first.exposure, 0.30 + 1e-9)

    def test_costs_applied_on_both_sides(self):
        d0, d1, d5, d6 = DAYS[0], DAYS[1], DAYS[5], DAYS[6]
        costs = CostModel(half_spread_bps=2, slippage_bps=3, stop_extra_bps=5,
                          sec_fee_rate=0.0, finra_taf_per_share=0.0)
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0),
                          d5: Signal(Action.EXIT, "done")})
        res = Backtester(strat, {"SPY": flat_series()}, cfg(costs=costs)).run()
        t = res.trades[0]
        self.assertAlmostEqual(t.entry_price, 100 * 1.0005)
        self.assertAlmostEqual(t.exit_price, 100 * 0.9995)
        self.assertLess(t.pnl, 0)  # flat prices + costs = loss
        self.assertAlmostEqual(t.pnl, (100 * 0.9995 - 100 * 1.0005) * t.shares)

    def test_stop_hit_intraday_fills_at_stop(self):
        d0, d3 = DAYS[0], DAYS[3]
        series = flat_series(overrides={d3: (99, 99.5, 94, 96)})
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0)})
        res = Backtester(strat, {"SPY": series}, cfg()).run()
        t = res.trades[0]
        self.assertEqual((t.exit_day, t.exit_reason, t.exit_price), (d3, "stop_hit", 95.0))
        self.assertAlmostEqual(t.r_multiple, -1.0)

    def test_gap_through_stop_fills_at_open_worse_than_stop(self):
        d0, d3 = DAYS[0], DAYS[3]
        series = flat_series(overrides={d3: (90, 91, 89, 90)})
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0)})
        res = Backtester(strat, {"SPY": series}, cfg()).run()
        t = res.trades[0]
        self.assertEqual((t.exit_reason, t.exit_price), ("stop_gap_at_open", 90.0))
        self.assertLess(t.r_multiple, -1.0)  # gap risk is realised, not hidden

    def test_entry_gapping_below_stop_is_stopped_at_open(self):
        d0, d1 = DAYS[0], DAYS[1]
        series = flat_series(overrides={d1: (94, 96, 93, 95)})
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0)})
        res = Backtester(strat, {"SPY": series}, cfg()).run()
        t = res.trades[0]
        self.assertEqual((t.entry_day, t.exit_day, t.exit_reason), (d1, d1, "stop_gap_at_open"))

    def test_open_position_closed_at_end(self):
        strat = Scripted({DAYS[0]: Signal(Action.ENTER_LONG, "go", stop_price=95.0)})
        res = Backtester(strat, {"SPY": flat_series()}, cfg()).run()
        self.assertEqual(res.trades[-1].exit_reason, "end_of_backtest")
        self.assertEqual(res.equity_curve[-1].open_positions, 0)


class RiskInvariantTests(unittest.TestCase):
    def test_max_positions_and_no_margin(self):
        syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        data = {s: flat_series(s) for s in syms}

        class AlwaysEnter(Scripted):
            def evaluate(self, history, position):
                if position.in_position:
                    return Signal(Action.HOLD, "hold", position.stop_price)
                return Signal(Action.ENTER_LONG, "go", stop_price=history[-1].close * 0.99)

        res = Backtester(AlwaysEnter({}), data, cfg(), syms).run()
        self.assertLessEqual(max(p.open_positions for p in res.equity_curve), 3)
        self.assertTrue(all(p.cash >= -1e-9 for p in res.equity_curve))
        rejected = [x for x in res.decisions if x.outcome == "rejected"]
        self.assertTrue(any("max_open_positions" in x.reason or "max_exposure" in x.reason
                            for x in rejected))
        # every queued entry sized within 30% exposure at decision time
        self.assertLessEqual(max(p.exposure for p in res.equity_curve), 0.30 + 1e-9)

    def test_halt_blocks_new_entries_after_drawdown(self):
        d0, d2, d3, d4 = DAYS[0], DAYS[2], DAYS[3], DAYS[4]
        # Big gap down through stop on d2 (loss), then a halt-level drawdown is simulated via
        # a tiny halt threshold.
        series = flat_series(overrides={d2: (80, 81, 79, 80)})
        risk = RiskParams(stop_gap_buffer_pct=0.0, peak_drawdown_halt_pct=0.005,
                          intraday_loss_halt_pct=0.005)
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0),
                          d3: Signal(Action.ENTER_LONG, "again", stop_price=75.0),
                          d4: Signal(Action.ENTER_LONG, "again2", stop_price=75.0)})
        res = Backtester(strat, {"SPY": series}, cfg(risk=risk)).run()
        rej = [x for x in res.decisions if x.day in (d3, d4) and x.action == "enter_long"]
        self.assertTrue(rej)
        self.assertTrue(all(x.outcome == "rejected" and "halt" in x.reason for x in rej))

    def test_drawdown_halt_is_lifted_after_cooloff(self):
        # One losing trade breaches a 1% drawdown halt; without a reset the strategy would
        # sit in cash forever (auditor finding). With cool-off it resumes later.
        days = trading_days(date(2026, 1, 2), date(2026, 4, 30))
        d_loss = days[2]
        series = flat_series(days=days, overrides={d_loss: (80, 81, 79, 80)})
        risk = RiskParams(stop_gap_buffer_pct=0.0, peak_drawdown_halt_pct=0.01,
                          drawdown_halt_cooloff_days=10)

        class EnterWhenFlat(Scripted):
            def evaluate(self, history, position):
                if position.in_position:
                    return Signal(Action.HOLD, "hold", position.stop_price)
                return Signal(Action.ENTER_LONG, "go", stop_price=history[-1].close * 0.95)

        res = Backtester(EnterWhenFlat({}), {"SPY": series},
                         cfg(start=days[0], end=days[-1], risk=risk)).run()
        resets = [x for x in res.decisions if x.outcome == "halt_reset"]
        self.assertEqual(len(resets), 1)
        reset_day = resets[0].day
        self.assertTrue(any(x.outcome == "rejected" and "peak_drawdown" in x.reason
                            for x in res.decisions if x.day < reset_day))
        self.assertTrue(any(t.entry_day > reset_day for t in res.trades))

    def test_data_gap_cancels_pending_order(self):
        d0, d1 = DAYS[0], DAYS[1]
        full = flat_series()
        gapped = BarSeries("SPY", [b for b in full.bars if b.day != d1])
        self.assertIn("missing_session", {i.kind for i in gapped.issues})
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0)})
        res = Backtester(strat, {"SPY": gapped}, cfg()).run()
        self.assertTrue(any(x.day == d1 and x.outcome == "order_cancelled" for x in res.decisions))
        self.assertEqual(res.trades, [])


class LookAheadTests(unittest.TestCase):
    def setUp(self):
        self.syms = ["SPY", "QQQ", "IWM"]
        self.data = {s: synthetic_series(s, date(2016, 1, 1), date(2022, 12, 30), seed=3)
                     for s in self.syms}
        self.cfg = BacktestConfig(start=date(2017, 1, 3), end=date(2022, 12, 30))

    def test_strategy_only_sees_bars_through_today(self):
        strat = Scripted({})
        Backtester(strat, {"SPY": self.data["SPY"]}, replace(self.cfg, end=date(2017, 6, 30))).run()
        expected = [d for d in trading_days(self.cfg.start, date(2017, 6, 30))]
        self.assertEqual(strat.seen_last_days, expected)

    def test_future_data_cannot_change_past_decisions(self):
        cutoff = date(2020, 6, 30)
        base = Backtester(TrendFollowing.create(), self.data, self.cfg, self.syms).run()

        def scramble(series):
            bars = [b if b.day <= cutoff else Bar(b.day, b.open * 3, b.high * 3, b.low * 0.2,
                                                  b.close * 0.5, b.volume) for b in series.bars]
            bars = [Bar(b.day, b.open, max(b.open, b.high, b.close), min(b.open, b.low, b.close),
                        b.close, b.volume) for b in bars]
            return BarSeries(series.symbol, bars)

        altered = {s: scramble(v) for s, v in self.data.items()}
        alt = Backtester(TrendFollowing.create(), altered, self.cfg, self.syms).run()

        before = lambda res: [x for x in res.decisions if x.day <= cutoff]  # noqa: E731
        self.assertEqual(before(base), before(alt))
        eq_b = [p for p in base.equity_curve if p.day <= cutoff]
        eq_a = [p for p in alt.equity_curve if p.day <= cutoff]
        self.assertEqual(eq_b, eq_a)
        self.assertNotEqual(base.equity_curve[-1], alt.equity_curve[-1])  # sanity: data changed

    def test_deterministic(self):
        a = Backtester(TrendFollowing.create(), self.data, self.cfg, self.syms).run()
        b = Backtester(TrendFollowing.create(), self.data, self.cfg, self.syms).run()
        self.assertEqual(a.trades, b.trades)
        self.assertEqual(a.equity_curve, b.equity_curve)

    def test_accounting_identity(self):
        res = Backtester(TrendFollowing.create(), self.data, self.cfg, self.syms).run()
        final = res.equity_curve[-1].equity
        self.assertAlmostEqual(final - self.cfg.initial_capital, sum(t.pnl for t in res.trades),
                               places=6)
        self.assertTrue(all(math.isfinite(p.equity) for p in res.equity_curve))


if __name__ == "__main__":
    unittest.main()
