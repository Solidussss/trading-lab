"""Production wiring: build the live engine, and run it on a schedule inside the web service.

Scheduling choice: the engine is idempotent and cheap when nothing is pending (it returns
"up_to_date" without any network call), so the web service simply checks every
CHECK_INTERVAL. A session is processed on the first check after it settles (8:30 pm ET or
later); a restart or outage is caught up on the next check. `python main.py nightly` runs
one check and exits, for anyone who prefers a separate Railway cron service.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tradinglab.config import ConfigError, Settings
from tradinglab.engine.live import LiveSimulationEngine, RunReport
from tradinglab.engine.pg_store import PostgresStore
from tradinglab.marketdata.tiingo import TiingoBarSource, TiingoClient
from tradinglab.strategies.trend_following import TrendFollowing

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 15 * 60
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
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        super().__init__(name="engine-scheduler", daemon=True)
        self._factory = engine_factory
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
            return report
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive; surface the error
            self.status.last_error = f"{type(exc).__name__}: {exc}"
            self._engine = None  # rebuild (e.g. reconnect to the database) next time
            log.exception("engine check failed")
            return None

    def run(self) -> None:
        while not self._stop.is_set():
            report = self.check_once()
            wait = self._interval if report is not None else min(self._interval,
                                                                 RETRY_AFTER_ERROR_S)
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
