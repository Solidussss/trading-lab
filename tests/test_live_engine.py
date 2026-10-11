"""Live simulation engine: incremental sessions, catch-up, idempotency, data waits,
atomic commits, controls, and exact parity with the research backtester."""

import unittest
from decimal import Decimal
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
        self.assertEqual((state.last_session, state.initial_capital), (d, 1_000_000.0))

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

    def test_single_symbol_gap_accepted_once_provider_has_moved_on(self):
        source = FlakySource(DATA)
        source.drop = {("QQQ", date(2019, 3, 12))}
        eng, store, _ = engine(source)
        eng.run(evening(date(2019, 3, 8)))
        rep = eng.run(evening(date(2019, 3, 14)))  # QQQ has bars after 3/12
        self.assertIn(date(2019, 3, 12), rep.processed)
        kinds = [e.kind for e in store.events()]
        self.assertIn("data_gap_accepted", kinds)
        self.assertTrue(any(x.outcome == "data_gap" and x.symbol == "QQQ"
                            for x in store.decisions()))

    def test_stalled_provider_never_commits_empty_sessions(self):
        # Auditor probe p9: provider two+ sessions behind must not produce empty sessions.
        source = FlakySource(DATA, published_through=date(2019, 3, 11))
        eng, store, _ = engine(source)
        eng.run(evening(date(2019, 3, 8)))
        for d in trading_days(date(2019, 3, 12), date(2019, 3, 19)):
            rep = eng.run(evening(d))
        self.assertEqual(store.load_state().last_session, date(2019, 3, 11))
        self.assertEqual(rep.status, "waiting_for_data")
        self.assertIn("stalled", store.events()[-1].detail)
        self.assertFalse(any(x.outcome == "data_gap" for x in store.decisions()))
        source.published_through = None  # provider catches up: real bars are used
        rep = eng.run(evening(date(2019, 3, 19)) + timedelta(hours=1))
        self.assertEqual(rep.processed, trading_days(date(2019, 3, 12), date(2019, 3, 19)))
        self.assertEqual(store.bars_used(date(2019, 3, 12))["SPY"],
                         DATA["SPY"].bar_on(date(2019, 3, 12)))

    def test_all_symbols_missing_with_later_data_is_a_market_closure(self):
        source = FlakySource(DATA)
        closed = date(2019, 3, 13)
        source.drop = {(s, closed) for s in SYMS}
        eng, store, _ = engine(source)
        eng.run(evening(date(2019, 3, 11)))
        rep = eng.run(evening(date(2019, 3, 15)))
        self.assertIn(closed, rep.processed)
        self.assertIn("session_skipped", [e.kind for e in store.events()])
        self.assertFalse(any(x.day == closed and x.outcome == "data_gap"
                             for x in store.decisions()))
        self.assertEqual(store.load_state().last_session, date(2019, 3, 15))

    def test_rejected_provider_row_blocks_until_acknowledged(self):
        from tradinglab.marketdata.bars import Bar

        bad_day = date(2019, 3, 13)
        data = dict(DATA)
        b = DATA["SPY"].bar_on(bad_day)
        data["SPY"] = BarSeries("SPY", [x if x.day != bad_day else
                                        Bar(b.day, b.open, b.open * 0.5, b.low, b.close, b.volume)
                                        for x in DATA["SPY"].bars])
        eng, store, _ = engine(FlakySource(data))
        eng.run(evening(date(2019, 3, 11)))
        rep = eng.run(evening(date(2019, 3, 15)))
        self.assertEqual(rep.status, "blocked")
        self.assertEqual(store.load_state().last_session, date(2019, 3, 12))
        eng.acknowledge_anomaly("SPY", bad_day)
        rep = eng.run(evening(date(2019, 3, 15)) + timedelta(hours=1))
        self.assertEqual(rep.processed[-1], date(2019, 3, 15))

    def test_unrecorded_split_like_jump_blocks(self):
        from tradinglab.marketdata.bars import Bar

        jump = date(2019, 3, 13)
        bars = [x if x.day < jump else Bar(x.day, x.open / 2, x.high / 2, x.low / 2,
                                            x.close / 2, x.volume * 2)
                for x in DATA["IWM"].bars]  # a 2:1 split whose splitFactor row is missing
        data = {**DATA, "IWM": BarSeries("IWM", bars)}
        eng, store, _ = engine(FlakySource(data))
        eng.run(evening(date(2019, 3, 11)))
        rep = eng.run(evening(date(2019, 3, 15)))
        self.assertEqual(rep.status, "blocked")
        self.assertIn("without a matching recorded split", rep.messages[0])

    def test_recorded_split_does_not_block(self):
        from tradinglab.marketdata.bars import Bar

        split_day = date(2019, 3, 13)
        bars = [x if x.day < split_day else Bar(x.day, x.open / 2, x.high / 2, x.low / 2,
                                                 x.close / 2, x.volume * 2, 0.0,
                                                 2.0 if x.day == split_day else 1.0)
                for x in DATA["IWM"].bars]
        data = {**DATA, "IWM": BarSeries("IWM", bars)}
        eng, store, _ = engine(FlakySource(data))
        eng.run(evening(date(2019, 3, 11)))
        rep = eng.run(evening(date(2019, 3, 15)))
        self.assertEqual(rep.status, "processed")

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


class ConfigAndSettleTests(unittest.TestCase):
    def test_held_symbol_removed_from_config_is_still_managed(self):
        # Auditor probe p14: dropping a held symbol from SYMBOLS orphaned its position.
        eng, store, source = engine()
        eng.run(evening(date(2019, 1, 2)))
        eng.run(evening(date(2019, 12, 31)))
        state = store.load_state()
        held = sorted(state.positions)
        self.assertTrue(held, "fixture should hold at least one position")
        gone = held[0]
        remaining = ",".join(s for s in SYMS if s != gone)
        eng2 = LiveSimulationEngine(settings(SYMBOLS=remaining), store, source,
                                    TrendFollowing.create())
        eng2.run(evening(date(2020, 6, 30)))
        self.assertIn("config_changed", [e.kind for e in store.events()])
        # the orphan kept being marked and managed: either still held with fresh marks,
        # or closed by its stop/exit rules - never frozen at a stale price
        state = store.load_state()
        if gone in state.positions:
            self.assertEqual(state.last_close[gone], DATA[gone].bar_on(date(2020, 6, 30)).close)
        else:
            self.assertTrue(any(t.symbol == gone and t.exit_day > date(2019, 12, 31)
                                for t in store.trades()))
        self.assertFalse(any(x.symbol == gone and x.outcome == "order_queued"
                             and x.action == "enter_long" and x.day > date(2019, 12, 31)
                             for x in store.decisions()))

    def test_early_close_day_waits_for_corrections_window(self):
        eng, store, _ = engine()
        early = date(2019, 11, 29)  # day after Thanksgiving, 1 pm close
        five_pm_et = session(early).close_utc + timedelta(hours=4)  # 17:00 ET
        eng.run(five_pm_et)
        self.assertEqual(store.load_state().last_session, date(2019, 11, 27))
        eng.run(session(early).close_utc + timedelta(hours=7, minutes=31))  # 20:31 ET
        self.assertEqual(store.load_state().last_session, early)


class CapitalRestartTests(unittest.TestCase):
    def test_untraded_account_restarts_with_new_capital_traded_one_does_not(self):
        from dataclasses import replace

        store = InMemoryStore()
        small = replace(settings(), capital_base_usd=Decimal("10000"))
        LiveSimulationEngine(small, store, FlakySource(DATA), TrendFollowing.create()).run(
            evening(date(2019, 3, 15)))
        before = store.load_state()
        self.assertEqual(before.initial_capital, 10_000.0)
        self.assertTrue(before.pending)  # an order waiting for Monday, never filled
        eng = LiveSimulationEngine(settings(), store, FlakySource(DATA), TrendFollowing.create())
        eng.run(evening(date(2019, 3, 15)))
        st = store.load_state()
        self.assertEqual(st.initial_capital, float(settings().capital_base_usd))
        self.assertEqual(st.last_session, date(2019, 3, 15))
        self.assertGreater(sum(o.shares for o in st.pending), sum(o.shares for o in before.pending))
        self.assertTrue(any(e.kind == "restarted" for e in store.events()))
        eng.run(evening(date(2019, 3, 20)))  # fills happen: from now on it is a real history
        self.assertTrue(store.fills())
        LiveSimulationEngine(small, store, FlakySource(DATA), TrendFollowing.create()).run(
            evening(date(2019, 3, 21)))
        self.assertEqual(store.load_state().initial_capital, float(settings().capital_base_usd))


class PriceHistoryTests(unittest.TestCase):
    def test_prices_saved_on_run_and_backfilled_when_stale(self):
        eng, store, source = engine()
        eng.run(evening(date(2019, 3, 15)))
        hist = store.price_history("SPY", 400)
        self.assertEqual(hist[-1].day, date(2019, 3, 15))
        self.assertLessEqual(len(hist), 320)
        store._prices.clear()  # e.g. first deploy of the feature: portfolio exists, no prices
        calls = source.calls
        rep = eng.run(evening(date(2019, 3, 15)) + timedelta(hours=1))
        self.assertEqual(rep.status, "up_to_date")
        self.assertEqual(source.calls, calls + 1)
        self.assertEqual(store.price_history("QQQ", 1)[-1].day, date(2019, 3, 15))
        eng.run(evening(date(2019, 3, 15)) + timedelta(hours=2))
        self.assertEqual(source.calls, calls + 1)  # not stale any more: no extra API calls


class ControlsTests(unittest.TestCase):
    def test_pause_flag_reaches_the_core(self):
        eng, store, _ = engine()
        eng.run(evening(date(2018, 1, 2)))
        eng.pause(evening(date(2018, 1, 2)) + timedelta(minutes=1))
        eng.run(evening(date(2018, 12, 31)))
        queued_entries = [x for x in store.decisions()
                          if x.action == "enter_long" and x.outcome == "order_queued"
                          and x.day > date(2018, 1, 2)]
        self.assertEqual(queued_entries, [])
        self.assertTrue(any("emergency_pause" in x.reason for x in store.decisions()))
        eng.resume(evening(date(2018, 12, 31)) + timedelta(minutes=1))
        self.assertFalse(store.get_flag("paused"))

    def test_catch_up_applies_pause_as_of_each_session(self):
        # Pause was switched on mid-way through a stretch the job missed: sessions before
        # the pause must trade normally, sessions after it must not open positions.
        eng, store, _ = engine()
        eng.run(evening(date(2018, 1, 2)))
        pause_at = evening(date(2018, 6, 29)) + timedelta(minutes=1)
        store.set_flag("paused", True, pause_at)
        eng.run(evening(date(2018, 12, 31)))
        entries = [x for x in store.decisions()
                   if x.action == "enter_long" and x.outcome == "order_queued"]
        self.assertTrue(any(x.day <= date(2018, 6, 29) for x in entries))
        self.assertFalse(any(x.day > date(2018, 6, 29) for x in entries))


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
