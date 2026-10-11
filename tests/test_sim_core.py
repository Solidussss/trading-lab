import json
import unittest
from dataclasses import dataclass
from datetime import date

from tradinglab.engine.risk import RiskParams
from tradinglab.marketdata.bars import Bar, BarSeries
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.costs import ZERO_COST
from tradinglab.sim.core import Controls, PortfolioState, SimulationCore
from tradinglab.strategies.base import Action, PositionState, Signal, StrategySpec

DAYS = trading_days(date(2026, 3, 2), date(2026, 3, 31))


def series(overrides=None, sym="SPY"):
    overrides = overrides or {}
    out = []
    for d in DAYS:
        o, h, lo, c, div, split = overrides.get(d, (100, 101, 99, 100, 0.0, 1.0))
        out.append(Bar(d, o, h, lo, c, 1e6, div, split))
    return BarSeries(sym, out)


@dataclass
class Scripted:
    script: dict
    spec: StrategySpec = StrategySpec("scripted", "0", {})
    warmup_bars: int = 0

    def evaluate(self, history, position: PositionState):
        sig = self.script.get(history[-1].day)
        if sig is not None and not (sig.action == Action.ENTER_LONG and position.in_position):
            return sig
        return Signal(Action.HOLD if position.in_position else Action.NO_TRADE, "idle",
                      position.stop_price)


def run(strategy, data, days=DAYS, risk=None, mode="auto", controls=None, state=None):
    risk = risk or RiskParams(stop_gap_buffer_pct=0.0)
    core = SimulationCore(strategy, list(data), risk, ZERO_COST, drawdown_resume=mode)
    state = state or PortfolioState.initial(10_000.0)
    results = []
    for d in days:
        bars = {s: data[s].bar_on(d) for s in data}
        c = (controls or {}).get(d, Controls())
        results.append(core.step(state, d, bars, lambda s, x: data[s].history_through(x), c))
    return core, state, results


class CorporateActionTests(unittest.TestCase):
    def test_dividend_credited_to_holder_and_trade(self):
        d0, d5, d8, d9 = DAYS[0], DAYS[5], DAYS[8], DAYS[9]
        data = {"SPY": series({d5: (100, 101, 99, 100, 1.50, 1.0)})}
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0),
                          d8: Signal(Action.EXIT, "done")})
        _, state, results = run(strat, data)
        shares = 8  # $50 / (101 - 95)
        trades = [t for r in results for t in r.trades]
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].dividends, 1.50 * shares)
        self.assertAlmostEqual(state.dividends_received, 1.50 * shares)
        self.assertAlmostEqual(state.cash, 10_000 + 1.50 * shares)  # flat prices, no costs
        self.assertTrue(any(x.outcome == "corporate_action" for x in results[5].decisions))
        self.assertEqual(results[9].day, d9)

    def test_no_dividend_when_flat(self):
        data = {"SPY": series({DAYS[3]: (100, 101, 99, 100, 2.0, 1.0)})}
        _, state, _ = run(Scripted({}), data)
        self.assertEqual(state.dividends_received, 0.0)

    def test_split_rescales_position_and_strategy_view(self):
        d0, d4 = DAYS[0], DAYS[4]
        over = {d: (50, 50.5, 49.5, 50, 0.0, 2.0 if d == d4 else 1.0) for d in DAYS if d >= d4}
        data = {"SPY": series(over)}
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0)})
        _, state, results = run(strat, data, days=DAYS[:8])
        p = state.positions["SPY"]
        self.assertEqual(p.shares, 16)  # 8 shares before a 2:1 split
        self.assertAlmostEqual(p.stop, 47.5)
        self.assertAlmostEqual(p.entry_price, 50.0)
        # equity unchanged by the split itself (flat prices, zero costs)
        self.assertAlmostEqual(results[4].equity.equity, 10_000.0)
        # strategy sees a continuous, split-adjusted history ending at the raw price
        hist = data["SPY"].history_through(DAYS[6])
        self.assertTrue(all(abs(b.close - 50) < 1e-9 for b in hist))
        # ...and never a split that happens later (no look-ahead)
        self.assertEqual(data["SPY"].history_through(DAYS[3])[-1].close, 100)


class StateAndControlsTests(unittest.TestCase):
    def test_state_json_round_trip_mid_trade(self):
        strat = Scripted({DAYS[0]: Signal(Action.ENTER_LONG, "go", stop_price=95.0),
                          DAYS[2]: Signal(Action.EXIT, "x")})
        data = {"SPY": series()}
        _, state, _ = run(strat, data, days=DAYS[:3])  # exit is pending at this point
        blob = json.dumps(state.to_dict())
        restored = PortfolioState.from_dict(json.loads(blob))
        self.assertEqual(restored, state)
        self.assertEqual(len(restored.pending), 1)

    def test_reprocessing_a_session_is_refused(self):
        core, state, _ = run(Scripted({}), {"SPY": series()}, days=DAYS[:2])
        with self.assertRaises(ValueError):
            core.step(state, DAYS[1], {"SPY": None}, lambda s, d: [], Controls())

    def test_pause_blocks_entries_but_exits_and_stops_still_run(self):
        d0, d1, d6, d8 = DAYS[0], DAYS[1], DAYS[6], DAYS[8]
        data = {"SPY": series({d6: (99, 99.5, 94, 96, 0.0, 1.0)})}
        strat = Scripted({d0: Signal(Action.ENTER_LONG, "go", stop_price=95.0),
                          d8: Signal(Action.ENTER_LONG, "again", stop_price=95.0)})
        paused = {d: Controls(paused=True) for d in DAYS if d >= d1}
        _, state, results = run(strat, data, controls=paused)
        trades = [t for r in results for t in r.trades]
        self.assertEqual([t.exit_reason for t in trades], ["stop_hit"])  # stop ran while paused
        rejected = [x for r in results for x in r.decisions
                    if x.day == d8 and x.action == "enter_long"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].outcome, "rejected")
        self.assertIn("emergency_pause", rejected[0].reason)
        self.assertEqual(state.positions, {})

    def test_manual_drawdown_latch_until_resume(self):
        d0, d2 = DAYS[0], DAYS[2]
        data = {"SPY": series({d2: (80, 81, 79, 80, 0.0, 1.0)})}

        class EnterWhenFlat(Scripted):
            def evaluate(self, history, position):
                if position.in_position:
                    return Signal(Action.HOLD, "hold", position.stop_price)
                return Signal(Action.ENTER_LONG, "go", stop_price=history[-1].close * 0.95)

        risk = RiskParams(stop_gap_buffer_pct=0.0, peak_drawdown_halt_pct=0.01,
                          drawdown_halt_cooloff_days=3)
        core, state, results = run(EnterWhenFlat({}), data, days=DAYS[:12], risk=risk,
                                   mode="manual")
        self.assertTrue(state.drawdown_halt_latched)
        entries_after = [t for r in results for t in r.trades if t.entry_day > d2]
        self.assertEqual(entries_after, [])  # no auto-resume in manual mode
        SimulationCore.resume_after_drawdown(state)
        self.assertFalse(state.drawdown_halt_latched)
        _, state, more = run(EnterWhenFlat({}), data, days=DAYS[12:16], risk=risk,
                             mode="manual", state=state)
        self.assertTrue(any(x.outcome == "order_queued" for r in more for x in r.decisions))


if __name__ == "__main__":
    unittest.main()
