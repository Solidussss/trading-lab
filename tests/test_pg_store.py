"""PostgresStore against a real PostgreSQL server.

Runs when a server is reachable (TL_TEST_PG, default 127.0.0.1:55432, trust auth, user tl);
skipped otherwise. Each test gets its own throwaway database.
"""

import os
import unittest
import uuid
from datetime import UTC, date, datetime, timedelta
from functools import partial

from tests import pgwire
from tests.helpers import settings
from tests.test_live_engine import DATA, SYMS, FlakySource, evening
from tradinglab.engine.live import LiveSimulationEngine
from tradinglab.engine.pg_store import PostgresStore
from tradinglab.engine.store import EngineBusyError, EngineEvent, InMemoryStore
from tradinglab.marketdata.calendar import trading_days
from tradinglab.research.backtest import BacktestConfig, Backtester
from tradinglab.sim.core import PortfolioState
from tradinglab.strategies.trend_following import TrendFollowing

HOST, _, PORT = os.environ.get("TL_TEST_PG", "127.0.0.1:55432").partition(":")


def _server_available() -> bool:
    try:
        pgwire.connect(HOST, int(PORT)).close()
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_server_available(), "no local PostgreSQL test server")
class PgTestCase(unittest.TestCase):
    def setUp(self):
        self.dbname = "tl_" + uuid.uuid4().hex[:12]
        admin = pgwire.connect(HOST, int(PORT))
        admin.autocommit = True
        admin.cursor().execute(f"CREATE DATABASE {self.dbname}")
        admin.close()
        self.connect = partial(pgwire.connect, HOST, int(PORT), "tl", self.dbname)
        self.store = PostgresStore(self.connect)
        self.assertEqual(self.store.migrate(), 1)

    def tearDown(self):
        self.store.close()
        admin = pgwire.connect(HOST, int(PORT))
        admin.autocommit = True
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {self.dbname} WITH (FORCE)")
        admin.close()

    def engine(self, source=None):
        return LiveSimulationEngine(settings(), self.store, source or FlakySource(DATA),
                                    TrendFollowing.create())


class SchemaTests(PgTestCase):
    def test_migrations_are_idempotent(self):
        self.assertEqual(self.store.migrate(), 1)
        self.assertEqual(self.store.migrate(), 1)

    def test_state_round_trip_and_flags(self):
        self.assertIsNone(self.store.load_state())
        st = PortfolioState.initial(10_000.0)
        st.last_session = date(2026, 10, 9)
        self.store.save_state(st)
        self.assertEqual(self.store.load_state(), st)
        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        self.store.set_flag("paused", True, t0)
        self.store.set_flag("paused", False, t0 + timedelta(days=2))
        self.assertFalse(self.store.get_flag("paused"))
        self.assertTrue(self.store.flag_at("paused", t0 + timedelta(days=1)))
        self.assertFalse(self.store.flag_at("paused", t0 - timedelta(days=1)))
        self.store.add_event(EngineEvent(t0, None, "test", "it's quoted"))
        self.assertEqual(self.store.recent_events(5)[-1].detail, "it's quoted")

    def test_advisory_lock_blocks_concurrent_runs(self):
        other = PostgresStore(self.connect)
        with self.store.run_lock():
            with self.assertRaises(EngineBusyError):
                with other.run_lock():
                    pass
        with other.run_lock():  # released afterwards
            pass
        other.close()


class EngineOnPostgresTests(PgTestCase):
    def test_runs_and_double_commit_refused(self):
        eng = self.engine()
        rep = eng.run(evening(date(2019, 3, 15)))
        self.assertEqual(rep.processed, [date(2019, 3, 15)])
        state = self.store.load_state()
        self.assertEqual(state.last_session, date(2019, 3, 15))
        self.assertEqual(self.store.bars_used(date(2019, 3, 15))["SPY"],
                         DATA["SPY"].bar_on(date(2019, 3, 15)))
        from tradinglab.sim.core import StepResult

        with self.assertRaises(ValueError):
            self.store.commit_session(state, StepResult(date(2019, 3, 15)), {})

    def test_commit_failure_rolls_back_everything(self):
        eng = self.engine()
        eng.run(evening(date(2019, 3, 13)))
        before_state = self.store.load_state()
        before_dec = len(self.store.decisions())
        original = self.store._upsert_state

        def boom(cur, payload):
            raise OSError("simulated failure mid-transaction")

        self.store._upsert_state = boom
        with self.assertRaises(OSError):
            eng.run(evening(date(2019, 3, 15)))
        self.store._upsert_state = original
        self.assertEqual(self.store.load_state(), before_state)
        self.assertEqual(len(self.store.decisions()), before_dec)
        self.assertEqual(self.store.bars_used(date(2019, 3, 14)), {})
        rep = eng.run(evening(date(2019, 3, 15)))
        self.assertEqual(rep.processed, [date(2019, 3, 14), date(2019, 3, 15)])

    def test_postgres_and_memory_stores_give_identical_results(self):
        """Same engine, same data, 2019: Postgres-backed run must equal the in-memory run
        and the research backtester."""
        start, end = date(2019, 1, 2), date(2019, 12, 31)
        pg_eng = self.engine()
        mem = InMemoryStore()
        mem_eng = LiveSimulationEngine(settings(), mem, FlakySource(DATA), TrendFollowing.create())
        for d in trading_days(start, end):
            pg_eng.run(evening(d))
            mem_eng.run(evening(d))
        self.assertEqual(self.store.trades(), mem.trades())
        self.assertEqual(self.store.equity_curve(), mem.equity_curve())
        self.assertEqual(self.store.decisions(), mem.decisions())
        self.assertEqual(self.store.load_state(), mem.load_state())
        bt = Backtester(TrendFollowing.create(), DATA, BacktestConfig(start=start, end=end),
                        SYMS).run()
        self.assertEqual(self.store.trades(),
                         [t for t in bt.trades if t.exit_reason != "end_of_backtest"])


if __name__ == "__main__":
    unittest.main()
