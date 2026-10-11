"""Live paper simulation: runs the shared simulation core forward, one real session at a time.

A scheduled job calls `LiveSimulationEngine.run(now)` once each evening. Each run:

  1. Takes the run lock (concurrent runs are refused).
  2. Works out the latest *settled* session: close + 4.5 h and never before 8:30 pm ET,
     after the data provider's corrections window.
  3. Loads the persisted portfolio state. The very first run initialises a fresh $1,000,000
     portfolio whose history starts at that session (no back-dated trades).
  4. Fetches recent bars once (configured symbols plus any symbol still held), then processes
     every session after the last processed one, in order - missed days are caught up
     exactly as if the job had run each day, with the pause setting that applied that day.
  5. Never invents or discards data:
       * a symbol's bar missing for session D is accepted as a gap only when the provider
         has already delivered later sessions for that symbol (evidence it has moved past D);
         otherwise the run stops and waits;
       * if every symbol is missing D but all have later bars, the market is treated as
         closed that day (unscheduled closure) and the session is skipped;
       * a session whose bar was rejected by validation (bad OHLC etc.), or whose overnight
         price change is implausible without a recorded split (|ln(open/prev close)| > ln 1.35),
         blocks processing until the provider corrects it or you acknowledge it.
  6. Commits state + records atomically per session. A crash mid-run loses nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from tradinglab.config import Settings
from tradinglab.engine.risk import RiskParams
from tradinglab.engine.store import EngineBusyError, EngineEvent, Store
from tradinglab.marketdata.bars import BarSeries
from tradinglab.marketdata.calendar import (
    is_trading_day,
    last_completed_session,
    previous_trading_day,
    session,
    trading_days,
)
from tradinglab.marketdata.source import BarSource
from tradinglab.research.costs import CostModel
from tradinglab.sim.core import Controls, PortfolioState, SimulationCore
from tradinglab.strategies.base import Strategy

SETTLE_MINUTES = 270  # 4.5 h after the close...
SETTLE_NOT_BEFORE = time(20, 30)  # ...and never before 8:30 pm ET (corrections end ~8 pm)
MAX_OVERNIGHT_LOG_MOVE = math.log(1.35)
STALL_ALERT_SESSIONS = 3
BLOCKING_ISSUES = {"bad_ohlc", "non_positive", "non_finite", "duplicate"}


@dataclass
class RunReport:
    status: str  # initialized|processed|up_to_date|waiting_for_data|blocked|data_error|busy
    target: date | None
    processed: list[date] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def settled_session(now: datetime) -> date:
    return last_completed_session(now, settle_minutes=SETTLE_MINUTES,
                                  not_before=SETTLE_NOT_BEFORE)


def decision_time(day: date) -> datetime:
    """When the evening run for `day` would normally have made its decisions."""
    close = session(day).close_utc
    nb = datetime.combine(day, SETTLE_NOT_BEFORE, tzinfo=session(day).close_ny.tzinfo)
    return max(close + timedelta(minutes=SETTLE_MINUTES), nb.astimezone(UTC))


class LiveSimulationEngine:
    def __init__(self, settings: Settings, store: Store, source: BarSource, strategy: Strategy,
                 costs: CostModel | None = None) -> None:
        self.settings = settings
        self.store = store
        self.source = source
        self.symbols: Sequence[str] = settings.symbols
        self.risk = RiskParams.from_limits(settings.risk)
        self.costs = costs or CostModel()
        self.core = SimulationCore(strategy, settings.symbols, self.risk, self.costs,
                                   drawdown_resume="manual")
        self.fingerprint = hashlib.sha256(json.dumps(
            {"symbols": list(settings.symbols), "strategy": strategy.spec.label,
             "risk": asdict(self.risk), "costs": asdict(self.costs)},
            sort_keys=True).encode()).hexdigest()[:16]

    # ------------------------------------------------------------------------------------

    def run(self, now: datetime | None = None) -> RunReport:
        now = now or datetime.now(UTC)
        try:
            with self.store.run_lock():
                return self._run_locked(now)
        except EngineBusyError as exc:
            return RunReport("busy", None, messages=[str(exc)])
        except Exception as exc:
            try:  # leave an audit trail, but never mask the original error
                self._event(now, None, "run_error", f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001, S110
                pass
            raise

    def _run_locked(self, now: datetime) -> RunReport:
        target = settled_session(now)
        state = self.store.load_state()
        report = RunReport("up_to_date", target)
        capital = float(self.settings.capital_base_usd)
        if (state is not None and state.initial_capital != capital and not self.store.fills()
                and not self.store.trades() and not state.positions):
            # unfilled orders are simply re-decided at the new size when the day is re-run
            self.store.reset_daily_account()  # never traded: start over with the new capital
            self._event(now, None, "restarted",
                        f"account had never traded; restarted with ${capital:,.0f}")
            state = None
        if state is None:
            state = PortfolioState.initial(capital)
            state.last_session = previous_trading_day(target)
            state.config_fingerprint = self.fingerprint
            self.store.save_state(state)
            self._event(now, target, "initialized",
                        f"new ${state.initial_capital:,.0f} simulated portfolio; first session "
                        f"{target}; config {self.fingerprint}")
            report.status = "initialized"
        assert state.last_session is not None
        if state.config_fingerprint != self.fingerprint:
            self._event(now, None, "config_changed",
                        f"configuration {state.config_fingerprint or '?'} -> {self.fingerprint} "
                        "(symbols/strategy/risk/costs); held positions remain managed")
            state.config_fingerprint = self.fingerprint
            self.store.save_state(state)
        if state.last_session >= target:
            self._refresh_prices_if_stale(now, state.last_session)
            return report

        wanted = list(self.symbols) + sorted(s for s in state.positions if s not in self.symbols)
        sessions = trading_days(state.last_session + timedelta(days=1), target)
        try:
            data = self.source.history(wanted, through=target)
        except Exception as exc:  # provider down / bad token: nothing advances
            self._event(now, target, "data_error", f"{type(exc).__name__}: {exc}")
            report.status = "data_error"
            report.messages.append(str(exc))
            return report
        self._save_prices(data)
        self._flag_calendar_anomalies(now, data, state.last_session, target)
        for sym in wanted:
            if sym not in state.last_close:
                hist = data[sym].history_through(state.last_session)
                if hist:
                    state.last_close[sym] = hist[-1].close

        for day in sessions:
            verdict, detail = self._assess(day, wanted, data)
            if verdict in ("wait", "block"):
                kind = "waiting_for_data" if verdict == "wait" else "data_blocked"
                behind = len(trading_days(day, target))
                if behind >= STALL_ALERT_SESSIONS:
                    detail += f" (stalled {behind} sessions - check the data provider)"
                self._event(now, day, kind, detail)
                report.status = "waiting_for_data" if verdict == "wait" else "blocked"
                report.messages.append(detail)
                break
            if verdict == "closed":
                result = SimulationCore.skip_session(state, day, detail)
                self.store.commit_session(state, result, {})
                self._event(now, day, "session_skipped", detail)
                report.processed.append(day)
                report.status = "processed"
                continue
            if detail:  # accepted single-symbol gaps
                self._event(now, day, "data_gap_accepted", detail)

            bars = {s: data[s].bar_on(day) for s in wanted}

            def history(sym: str, d: date, _data: dict[str, BarSeries] = data):
                return _data[sym].history_through(d)

            paused = self.store.flag_at("paused", decision_time(day))
            result = self.core.step(state, day, bars, history, Controls(paused=paused))
            self.store.commit_session(state, result, bars)
            report.processed.append(day)
            report.status = "processed"
        return report

    # -- dashboard price history ----------------------------------------------------------

    PRICE_HISTORY_BARS = 320

    def _save_prices(self, data: dict[str, BarSeries]) -> None:
        saver = getattr(self.store, "save_prices", None)
        if saver is None:
            return
        for sym, series in data.items():
            saver(sym, series.bars[-self.PRICE_HISTORY_BARS:])

    def _refresh_prices_if_stale(self, now: datetime, last_session: date) -> None:
        """Backfill chart prices once (e.g. after an upgrade) without touching the portfolio."""
        reader = getattr(self.store, "price_history", None)
        if reader is None:
            return
        stale = any(not (h := reader(s, 1)) or h[-1].day < last_session for s in self.symbols)
        if not stale:
            return
        try:
            self._save_prices(self.source.history(self.symbols, through=last_session))
        except Exception as exc:  # display data only: never fail the run for it
            self._event(now, None, "price_history_error", f"{type(exc).__name__}: {exc}")

    # -- data integrity -------------------------------------------------------------------

    def _assess(self, day: date, symbols: Sequence[str], data: dict[str, BarSeries]
                ) -> tuple[str, str]:
        """'ok' | 'gap' (proceed with some symbols missing) | 'closed' | 'wait' | 'block'."""
        missing, gaps = [], []
        for sym in symbols:
            series = data[sym]
            bad = [i for i in series.issues if i.day == day and i.kind in BLOCKING_ISSUES]
            if bad and not self._acknowledged(sym, day):
                return "block", (f"{sym} {day}: provider row rejected ({bad[0].kind}); waiting "
                                 "for a correction or your acknowledgement")
            bar = series.bar_on(day)
            if bar is None:
                missing.append(sym)
                continue
            prev = series.history_through(day - timedelta(days=1))
            if prev:
                ratio = bar.open * bar.split / prev[-1].close  # split-normalised overnight move
                if abs(math.log(ratio)) > MAX_OVERNIGHT_LOG_MOVE and not self._acknowledged(sym, day):
                    return "block", (f"{sym} {day}: overnight move {ratio - 1:+.0%} without a "
                                     "matching recorded split; possible missing corporate action")
        if not missing:
            return "ok", ""
        later = {s: any(b.day > day for b in data[s].bars) for s in missing}
        if len(missing) == len(symbols):
            if all(later.values()):
                return "closed", (f"no bars for any symbol on {day} although later sessions "
                                  "exist: treated as an unscheduled market closure")
            return "wait", f"no bars yet for {day}; will retry next run"
        for s in missing:
            if not later[s]:
                return "wait", f"{s} has no bar yet for {day}; will retry next run"
            gaps.append(s)
        return "gap", (f"{', '.join(gaps)} missing on {day} while later sessions exist; "
                       "processed with a gap (pending orders for them cancelled)")

    def _acknowledged(self, sym: str, day: date) -> bool:
        return self.store.get_flag(f"ack:{sym}:{day.isoformat()}")

    def acknowledge_anomaly(self, sym: str, day: date, now: datetime | None = None) -> None:
        """You reviewed a blocked session's data and want it processed as delivered."""
        self.store.set_flag(f"ack:{sym}:{day.isoformat()}", True, now)
        self._event(now or datetime.now(UTC), day, "anomaly_acknowledged", sym)

    def _flag_calendar_anomalies(self, now: datetime, data: dict[str, BarSeries], after: date,
                                 through: date) -> None:
        for sym, series in data.items():
            for b in series.bars:
                if after < b.day <= through and not is_trading_day(b.day):
                    self._event(now, b.day, "calendar_anomaly",
                                f"{sym} has a bar on {b.day}, which the NYSE calendar marks "
                                "closed; bar ignored")

    # -- controls (dashboard) -------------------------------------------------------------

    def pause(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.store.set_flag("paused", True, now)
        self._event(now, None, "paused", "emergency pause ON: no new entries")

    def resume(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.store.set_flag("paused", False, now)
        self._event(now, None, "resumed", "emergency pause OFF")

    def resume_after_drawdown(self, now: datetime | None = None) -> None:
        with self.store.run_lock():
            state = self.store.load_state()
            if state is None or not state.drawdown_halt_latched:
                return
            SimulationCore.resume_after_drawdown(state)
            self.store.save_state(state)
            self._event(now or datetime.now(UTC), state.last_session, "drawdown_resumed",
                        f"new peak baseline ${state.peak_equity:,.2f}")

    def _event(self, now: datetime, day: date | None, kind: str, detail: str) -> None:
        self.store.add_event(EngineEvent(now, day, kind, detail))
