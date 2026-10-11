"""Live paper simulation: runs the shared simulation core forward, one real session at a time.

A scheduled job calls `LiveSimulationEngine.run(now)` once each evening (after the data
provider's corrections window). Each run:

  1. Takes the run lock (concurrent runs are refused).
  2. Works out the latest *settled* session (close + SETTLE_MINUTES).
  3. Loads the persisted portfolio state. The very first run initialises a fresh $10,000
     portfolio whose history starts at that session (no back-dated trades).
  4. Fetches recent bars once, then processes every session after the last processed one,
     in order - so missed days (outage, redeploy) are caught up exactly as if the job had
     run each day.
  5. If a session's bars are incomplete, it waits (stops, without advancing) unless the
     session is MAX_DATA_WAIT_SESSIONS old, in which case it proceeds and records the gap.
  6. Commits state + records atomically per session. A crash mid-run loses nothing: the
     next run resumes after the last committed session.

Fills use the session's real open/high/low prices, which is why orders decided at
close(D) are settled when D+1's bar arrives: the evening job after D+1.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from tradinglab.config import Settings
from tradinglab.engine.risk import RiskParams
from tradinglab.engine.store import EngineEvent, Store
from tradinglab.marketdata.bars import BarSeries
from tradinglab.marketdata.calendar import (
    is_trading_day,
    last_completed_session,
    previous_trading_day,
    trading_days,
)
from tradinglab.marketdata.source import BarSource
from tradinglab.research.costs import CostModel
from tradinglab.sim.core import Controls, PortfolioState, SimulationCore
from tradinglab.strategies.base import Strategy

# Tiingo publishes ~5:30 pm ET and corrects until ~8 pm ET; 4.5 h after a 4 pm close is
# 8:30 pm ET (5:30 pm ET after a 1 pm early close).
SETTLE_MINUTES = 270
MAX_DATA_WAIT_SESSIONS = 2


@dataclass
class RunReport:
    status: str  # initialized|processed|up_to_date|waiting_for_data|data_error|busy
    target: date | None
    processed: list[date] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class LiveSimulationEngine:
    def __init__(self, settings: Settings, store: Store, source: BarSource, strategy: Strategy,
                 costs: CostModel | None = None) -> None:
        self.settings = settings
        self.store = store
        self.source = source
        self.symbols: Sequence[str] = settings.symbols
        self.core = SimulationCore(strategy, settings.symbols,
                                   RiskParams.from_limits(settings.risk), costs or CostModel(),
                                   drawdown_resume="manual")

    # ------------------------------------------------------------------------------------

    def run(self, now: datetime | None = None) -> RunReport:
        now = now or datetime.now(UTC)
        try:
            with self.store.run_lock():
                return self._run_locked(now)
        except Exception as exc:  # noqa: BLE001 - always leave an audit trail, then re-raise
            from tradinglab.engine.store import EngineBusyError

            if isinstance(exc, EngineBusyError):
                return RunReport("busy", None, messages=[str(exc)])
            self._event(now, None, "run_error", f"{type(exc).__name__}: {exc}")
            raise

    def _run_locked(self, now: datetime) -> RunReport:
        target = last_completed_session(now, settle_minutes=SETTLE_MINUTES)
        state = self.store.load_state()
        report = RunReport("up_to_date", target)
        if state is None:
            state = PortfolioState.initial(float(self.settings.capital_base_usd))
            state.last_session = previous_trading_day(target)
            self.store.save_state(state)
            self._event(now, target, "initialized",
                        f"new ${state.initial_capital:,.0f} simulated portfolio; first session "
                        f"{target}")
            report.status = "initialized"
        assert state.last_session is not None
        if state.last_session >= target:
            return report

        sessions = trading_days(state.last_session + timedelta(days=1), target)
        try:
            data = self.source.history(self.symbols, through=target)
        except Exception as exc:  # provider down / bad token: nothing advances
            self._event(now, target, "data_error", f"{type(exc).__name__}: {exc}")
            report.status = "data_error"
            report.messages.append(str(exc))
            return report
        self._check_calendar(now, data, state.last_session, target)
        for sym in self.symbols:  # seed marks for symbols without a close yet
            if sym not in state.last_close:
                hist = data[sym].history_through(state.last_session)
                if hist:
                    state.last_close[sym] = hist[-1].close

        for day in sessions:
            bars = {s: data[s].bar_on(day) for s in self.symbols}
            missing = sorted(s for s, b in bars.items() if b is None)
            if missing:
                age = len(trading_days(day, target)) - 1
                if age < MAX_DATA_WAIT_SESSIONS:
                    self._event(now, day, "waiting_for_data",
                                f"no bar yet for {', '.join(missing)}; will retry next run")
                    report.status = "waiting_for_data"
                    break
                self._event(now, day, "data_gap_accepted",
                            f"{', '.join(missing)} still missing after {age} sessions; "
                            "processing with gap (pending orders for them are cancelled)")

            def history(sym: str, d: date, _data: dict[str, BarSeries] = data):
                return _data[sym].history_through(d)

            result = self.core.step(state, day, bars, history,
                                    Controls(paused=self.store.get_flag("paused")))
            self.store.commit_session(state, result, bars)
            report.processed.append(day)
            report.status = "processed"
        return report

    # -- controls (dashboard) -------------------------------------------------------------

    def pause(self, now: datetime | None = None) -> None:
        self.store.set_flag("paused", True)
        self._event(now or datetime.now(UTC), None, "paused", "emergency pause ON: no new entries")

    def resume(self, now: datetime | None = None) -> None:
        self.store.set_flag("paused", False)
        self._event(now or datetime.now(UTC), None, "resumed", "emergency pause OFF")

    def resume_after_drawdown(self, now: datetime | None = None) -> None:
        with self.store.run_lock():
            state = self.store.load_state()
            if state is None or not state.drawdown_halt_latched:
                return
            SimulationCore.resume_after_drawdown(state)
            self.store.save_state(state)
            self._event(now or datetime.now(UTC), state.last_session, "drawdown_resumed",
                        f"new peak baseline ${state.peak_equity:,.2f}")

    # ------------------------------------------------------------------------------------

    def _check_calendar(self, now: datetime, data: dict[str, BarSeries], after: date,
                        through: date) -> None:
        for sym, series in data.items():
            for b in series.bars:
                if after < b.day <= through and not is_trading_day(b.day):
                    self._event(now, b.day, "calendar_anomaly",
                                f"{sym} has a bar on {b.day}, which the NYSE calendar marks "
                                "closed; bar ignored")

    def _event(self, now: datetime, session: date | None, kind: str, detail: str) -> None:
        self.store.add_event(EngineEvent(now, session, kind, detail))
