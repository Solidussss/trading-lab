"""Production wiring: build the engines, and run them on a schedule inside the web service.

Each check runs, in order: the daily trend account, the day-trading bots, then (with whatever
request budget is left) the intraday history loader and the history tests. Every step is
idempotent and cheap when there is nothing to do. Checks run every 5 minutes while the market
is open and every 15 minutes otherwise. `python main.py nightly` runs one daily-account check
and exits, for anyone who prefers a separate Railway cron service.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from tradinglab.config import ConfigError, Settings
from tradinglab.engine.live import LiveSimulationEngine, RunReport
from tradinglab.engine.pg_store import PostgresStore
from tradinglab.intraday.live import DEFAULT_BOTS, IntradayRunner, settled
from tradinglab.intraday.research import backfill_status, backfill_step, run_history_test
from tradinglab.marketdata.calendar import is_trading_day, ny_date, previous_trading_day, session
from tradinglab.marketdata.tiingo import SHARED_LIMITER, TiingoBarSource, TiingoClient
from tradinglab.strategies.trend_following import TrendFollowing

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 15 * 60
MARKET_INTERVAL_S = 5 * 60
BACKFILL_HEADROOM = 12  # requests per hour always left free for the live bots
RETRY_AFTER_ERROR_S = 5 * 60


def postgres_store(settings: Settings) -> PostgresStore:
    if settings.database_url is None:
        raise ConfigError("DATABASE_URL is not set; attach the Railway PostgreSQL service.")
    import psycopg  # production driver; imported lazily so tests and research don't need it

    url = settings.database_url.get()
    store = PostgresStore(lambda: psycopg.connect(url, connect_timeout=10))
    store.migrate()
    return store


def build_engine(settings: Settings, store: PostgresStore | None = None) -> LiveSimulationEngine:
    store = store or postgres_store(settings)
    source = TiingoBarSource(TiingoClient(settings.require_data_token()))
    return LiveSimulationEngine(settings, store, source, TrendFollowing.create())


def build_intraday(settings: Settings, store: PostgresStore) -> IntradayRunner:
    client = TiingoClient(settings.require_data_token())
    return IntradayRunner(settings, store, client.iex_rows, DEFAULT_BOTS)


def market_busy(now: datetime) -> bool:
    """From 15 minutes before the open until the session has settled."""
    d = ny_date(now)
    if not is_trading_day(d):
        return False
    s = session(d)
    return s.open_utc - timedelta(minutes=15) <= now and not settled(d, now)


class ResearchJob:
    """Loads intraday history in the background and re-runs the history tests when it grows."""

    def __init__(self, runner: IntradayRunner, client: TiingoClient) -> None:
        self.runner, self.client = runner, client
        self.last_loaded = -1
        self.last_test_day: date | None = None

    def check(self, now: datetime) -> str:
        r = self.runner
        end = previous_trading_day(ny_date(now))
        if not market_busy(now):
            budget = max(0, SHARED_LIMITER.max_calls - BACKFILL_HEADROOM - SHARED_LIMITER.used())
            if budget:
                backfill_step(r.store, self.client.iex_rows, r.symbols, end, min(budget, 20), now,
                              fetch_daily=lambda sym, a, b: self.client.daily(sym, a, b).bars)
        status = backfill_status(r.store, r.symbols, end)
        grew = status.days_loaded >= self.last_loaded + max(60, status.days_total // 20)
        new_day = self.last_test_day != end
        if not market_busy(now) and (grew or (new_day and status.done)):
            run_history_test(r.store, r.bots, r.symbols, r.risk, r.capital, end=end, now=now,
                             unlocked=lambda bot_id: r.store.get_flag(f"unlock_test:{bot_id}"))
            self.last_loaded, self.last_test_day = status.days_loaded, end
        return f"history {status.fraction:.0%} loaded"


@dataclass
class SchedulerStatus:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_check: datetime | None = None
    last_report: RunReport | None = None
    last_error: str | None = None
    checks: int = 0


class Scheduler(threading.Thread):
    """Background thread that runs the engine check every CHECK_INTERVAL_S."""

    def __init__(self, engine_factory: Callable[[], LiveSimulationEngine], *,
                 interval: float = CHECK_INTERVAL_S,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 extra_jobs: Callable[[], list[tuple[str, Callable[[datetime], object]]]]
                 | None = None) -> None:
        super().__init__(name="engine-scheduler", daemon=True)
        self._factory = engine_factory
        self._extra_factory = extra_jobs
        self._extra: list[tuple[str, Callable[[datetime], object]]] | None = None
        self.job_status: dict[str, str] = {}
        self._engine: LiveSimulationEngine | None = None
        self._interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self.status = SchedulerStatus()

    def check_once(self) -> RunReport | None:
        now = self._clock()
        self.status.last_check = now
        self.status.checks += 1
        try:
            if self._engine is None:
                self._engine = self._factory()
            report = self._engine.run(now)
            self.status.last_report, self.status.last_error = report, None
            if report.processed or report.status not in ("up_to_date",):
                log.info("engine: %s target=%s processed=%s %s", report.status, report.target,
                         [d.isoformat() for d in report.processed], "; ".join(report.messages))
            self._run_extra(now)
            return report
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive; surface the error
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            self._engine = None  # rebuild (e.g. reconnect to the database) next time
            log.exception("engine check failed")
            return None

    def _run_extra(self, now: datetime) -> None:
        """Further jobs (day-trading bots, research). One failing never stops the others."""
        if self._extra_factory is None:
            return
        if self._extra is None:
            self._extra = self._extra_factory()
        for name, job in self._extra:
            try:
                result = job(now)
                text = str(getattr(result, "status", result))
                if text != self.job_status.get(name):
                    log.info("%s: %s", name, text)
                self.job_status[name] = text
            except Exception as exc:  # noqa: BLE001
                self.job_status[name] = f"error: {type(exc).__name__}: {exc}"
                log.exception("%s failed", name)
                self._extra = None  # rebuild next time (e.g. reconnect)

    def run(self) -> None:
        while not self._stop.is_set():
            report = self.check_once()
            interval = MARKET_INTERVAL_S if market_busy(self._clock()) else self._interval
            wait = interval if report is not None else min(interval, RETRY_AFTER_ERROR_S)
            self._stop.wait(wait)

    def stop(self) -> None:
        self._stop.set()


def run_nightly_once(env) -> int:
    """`python main.py nightly`: one engine check; non-zero exit when attention is needed."""
    from tradinglab.safety.selfcheck import SafetyCheckFailedError, run_selfcheck

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = run_selfcheck(env).settings
    except SafetyCheckFailedError as exc:
        log.error("%s", exc)
        return 2
    try:
        engine = build_engine(settings)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    started = time.monotonic()
    report = engine.run()
    log.info("status=%s target=%s processed=%d (%.1fs) %s", report.status, report.target,
             len(report.processed), time.monotonic() - started, "; ".join(report.messages))
    return 1 if report.status in ("data_error", "blocked") else 0
