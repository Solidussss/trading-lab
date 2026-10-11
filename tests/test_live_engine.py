"""Live simulation engine: incremental sessions, catch-up, idempotency, data waits,
atomic commits, controls, and exact parity with the research backtester."""

import unittest
from datetime import date, datetime, timedelta

from tests.helpers import settings
from tradinglab.engine.live import SETTLE_MINUTES, LiveSimulationEngine
from tradinglab.engine.store import InMemoryStore
from tradinglab.marketdata.bars import BarSeries
from tradinglab.marketdata.calendar import session, trading_days
from tradinglab.marketdata.source import InMemoryBarSource
from tradinglab.research.backtest import BacktestConfig, Backtester
from tradinglab.research.synthetic import synthetic_series
from tradinglab.strategies.trend_following import TrendFollowing

SYMS = ["SPY", "QQQ", "IWM"]
DATA = {s: synthetic_series(s, date(2016, 1, 1), date(2021, 12, 31), seed=21) for s in SYMS}


def evening(d: date) -> datetime:
    """A moment after session d's data has settled."""
    return session(d).close_utc + timedelta(minutes=SETTLE_MINUTES + 5)


class FlakySource:
    """Serves data only up to a moving 'published through' date; can fail on demand."""

    def __init__(self, data, published_through=None):
        self.inner = InMemoryBarSource(data)
        self.data = data
        self.published_through = published_through
        self.fail = False
        self.calls = 0
        self.drop: set[tuple[str, date]] = set()

    def history(self, symbols, through):
        self.calls += 1
        if self.fail:
            raise ConnectionError("provider down")
        cap = min(through, self.published_through or through)
        out = self.inner.history(symbols, cap)
        if self.drop:
            out = {s: BarSeries(s, [b for b in v.bars if (s, b.day) not in self.drop])
                   for s, v in out.items()}
        return out


def engine(source=None, store=None):
    store = store or InMemoryStore()
    source = source or FlakySource(DATA)
    return LiveSimulationEngine(settings(), store, source, TrendFollowing.create()), store, source


class LifecycleTests(unittest.TestCase):
    def test_first_run_initialises_then_processes_that_session(self):
        eng, store, _ = engine()
        d = date(2019, 3, 15)
        rep = eng.run(evening(d))
        self.assertEqual((rep.status, rep.processed), ("processed", [d]))
        self.assertEqual(store.events()[0].kind, "initialized")
        state = store.load_state()
        self.assertEqual((state.last_session, state.initial_capital), (d, 10_000.0))

    def test_before_settle_targets_previous_session(self):
        eng, store, _ = engine()
        d = date(2019, 3, 15)
        eng.run(session(d).close_utc + timedelta(minutes=30))  # data not yet settled
        self.assertEqual(store.load_state().last_session, date(2019, 3, 14))

    def test_rerun_is_idempotent(self):
        eng, store, source = engine()
        d = date(2019, 3, 15)
        eng.run(evening(d))
        n_dec = len(store.decisions())
        rep = eng.run(evening(d) + timedelta(hours=1))
        self.assertEqual(rep.status, "up_to_date")
        self.assertEqual(len(store.decisions()), n_dec)

    def test_catch_up_after_missed_days(self):
        eng, store, source = engine()
        eng.run(evening(date(2019, 3, 1)))
        calls = source.calls
        rep = eng.run(evening(date(2019, 3, 15)))  # job didn't run for two weeks
        self.assertEqual(rep.processed, trading_days(date(2019, 3, 4), date(2019, 3, 15)))
        self.assertEqual(source.calls, calls + 1)  # one fetch for the whole catch-up

    def test_busy_lock(self):
        eng, store, _ = engine()
        with store.run_lock():
            self.assertEqual(eng.run(evening(date(2019, 3, 15))).status, "busy")


class DataHandlingTests(unittest.TestCase):
    def test_waits_for_late_data_then_resumes(self):
        source = FlakySource(DATA, published_through=date(2019, 3, 14))
        eng, store, _ = engine(source)
        eng.run(evening(date(2019, 3, 13)))
        rep = eng.run(evening(date(2019, 3, 15)))  # provider is a day behind
        self.assertEqual(rep.status, "waiting_for_data")
        self.assertEqual(rep.processed, [date(2019, 3, 14)])
        self.assertEqual(store.events()[-1].kind, "waiting_for_data")
        source.published_through = None
        rep = eng.run(evening(date(2019, 3, 15)) + timedelta(hours=2))
        self.assertEqual(rep.processed, [date(2019, 3, 15)])

    def test_persistent_gap_is_accepted_and_recorded(self):
        source = FlakySource(DATA)
        source.drop = {("QQQ", date(2019, 3, 12))}
        eng, store, _ = engine(source)
        eng.run(evening(date(2019, 3, 8)))
        rep = eng.run(evening(date(2019, 3, 14)))  # gap is 2 sessions old by now
        self.assertIn(date(2019, 3, 12), rep.processed)
        kinds = [e.kind for e in store.events()]
        self.assertIn("data_gap_accepted", kinds)
        self.assertTrue(any(x.outcome == "data_gap" and x.symbol == "QQQ"
                            for x in store.decisions()))

    def test_provider_failure_changes_nothing(self):
        source = FlakySource(DATA)
        eng, store, _ = engine(source)
        eng.run(evening(date(2019, 3, 13)))
        before = store.load_state()
        source.fail = True
        rep = eng.run(evening(date(2019, 3, 15)))
        self.assertEqual(rep.status, "data_error")
        self.assertEqual(store.load_state(), before)
        self.assertEqual(store.events()[-1].kind, "data_error")

    def test_commit_failure_is_atomic_and_recoverable(self):
        eng, store, _ = engine()
        eng.run(evening(date(2019, 3, 13)))
        before_state, before_dec = store.load_state(), len(store.decisions())
        store.fail_next_commit = True
        with self.assertRaises(OSError):
            eng.run(evening(date(2019, 3, 15)))
        self.assertEqual(store.load_state(), before_state)
        self.assertEqual(len(store.decisions()), before_dec)
        self.assertEqual(store.events()[-1].kind, "run_error")
        rep = eng.run(evening(date(2019, 3, 15)))  # next run recovers cleanly
        self.assertEqual(rep.processed, [date(2019, 3, 14), date(2019, 3, 15)])

    def test_bars_used_are_recorded_for_audit(self):
        eng, store, _ = engine()
        d = date(2019, 3, 15)
        eng.run(evening(d))
        used = store.bars_used(d)
        self.assertEqual(used["SPY"], DATA["SPY"].bar_on(d))


class ControlsTests(unittest.TestCase):
    def test_pause_flag_reaches_the_core(self):
        eng, store, _ = engine()
        eng.run(evening(date(2018, 1, 2)))
        eng.pause()
        eng.run(evening(date(2018, 12, 31)))
        queued_entries = [x for x in store.decisions()
                          if x.action == "enter_long" and x.outcome == "order_queued"
                          and x.day > date(2018, 1, 2)]
        self.assertEqual(queued_entries, [])
        self.assertTrue(any("emergency_pause" in x.reason for x in store.decisions()))
        eng.resume()
        self.assertFalse(store.get_flag("paused"))


class ParityTests(unittest.TestCase):
    def test_live_engine_matches_backtester_exactly(self):
        """Running the live engine one evening at a time over 2019-2020 must produce the
        same trades and equity as a single backtest over the same dates."""
        start, end = date(2019, 1, 2), date(2020, 12, 30)
        eng, store, _ = engine()
        for d in trading_days(start, end):
            eng.run(evening(d))
        bt = Backtester(TrendFollowing.create(), DATA, BacktestConfig(start=start, end=end),
                        SYMS).run()
        live_trades = store.trades()
        bt_trades = [t for t in bt.trades if t.exit_reason != "end_of_backtest"]
        self.assertGreater(len(bt_trades), 5)
        self.assertEqual(live_trades, bt_trades[:len(live_trades)])
        self.assertEqual(len(live_trades), len(bt_trades))
        live_eq = [round(p.equity, 6) for p in store.equity_curve()][:-1]
        bt_eq = [round(p.equity, 6) for p in bt.equity_curve][:-1]  # bt liquidates on last day
        self.assertEqual(live_eq, bt_eq)


if __name__ == "__main__":
    unittest.main()
