"""Live day-trading bots: one shared 5-minute feed, one simulated account per bot.

Each scheduler check (every 5 minutes while the market is open, every 15 otherwise):
  1. During the session, refreshes today's 5-minute bars (3 requests, shared by every bot).
     The dashboard replays today's bars through each bot's core to show "today so far";
     nothing is committed mid-day, and the replay is deterministic, so what you see during the
     day is exactly what gets committed.
  2. Once a session has ended (close + 20 min), fetches its final bars and commits that day
     for every bot, atomically per bot. Missed days are caught up in order.
  3. A new bot starts on the first session that opens after it is created: no back-dated
     trades.

The owner's pause applies from the moment it is set: entries signalled after it are refused
(positions are still managed and sold by the close).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

from tradinglab.config import Settings
from tradinglab.engine.risk import RiskParams
from tradinglab.engine.store import EngineBusyError, EngineEvent
from tradinglab.intraday.core import INTRADAY_COSTS, BotState, DayResult, IntradayCore
from tradinglab.intraday.strategies import REGISTRY, IntradayStrategy
from tradinglab.marketdata.calendar import (
    is_trading_day,
    next_trading_day,
    ny_date,
    previous_trading_day,
    session,
    trading_days,
)
from tradinglab.marketdata.intraday import (
    MIN_COVERAGE,
    IBar,
    coverage,
    detect_end_labelled,
    parse_iex_rows,
)

log = logging.getLogger(__name__)

SETTLE = timedelta(minutes=20)  # IEX bars are final shortly after the close
LIVE_REFRESH = timedelta(minutes=4)
FINAL_GRACE = timedelta(hours=3)  # after this, a day's data is taken as it is
MAX_CATCH_UP_DAYS = 30  # per run; a longer outage is caught up over several runs


@dataclass(frozen=True)
class BotSpec:
    id: str
    strategy_id: str
    params: dict = field(default_factory=dict)

    def strategy(self) -> IntradayStrategy:
        return REGISTRY[self.strategy_id](**self.params)


DEFAULT_BOTS: tuple[BotSpec, ...] = tuple(BotSpec(sid, sid) for sid in REGISTRY)


@dataclass
class IntradayReport:
    status: str  # idle | refreshed | committed | waiting | data_error | busy
    committed: list[tuple[str, date]] = field(default_factory=list)
    requests: int = 0
    messages: list[str] = field(default_factory=list)


def settled(day: date, now: datetime) -> bool:
    return now >= session(day).close_utc + SETTLE


def latest_settled_session(now: datetime) -> date:
    d = ny_date(now)
    if not is_trading_day(d) or not settled(d, now):
        d = previous_trading_day(d)
    return d


def first_session_after(now: datetime) -> date:
    d = ny_date(now)
    if is_trading_day(d) and now < session(d).open_utc:
        return d
    return next_trading_day(d)


RowFetcher = Callable[[str, date, date], list]


class IntradayRunner:
    def __init__(self, settings: Settings, store, fetch_rows: RowFetcher,
                 bots: Sequence[BotSpec] = DEFAULT_BOTS) -> None:
        self.settings = settings
        self.store = store
        self.fetch_rows = fetch_rows
        self.bots = list(bots)
        self.symbols = list(settings.symbols)
        self.capital = float(settings.capital_base_usd)
        self.risk = RiskParams.from_limits(settings.risk)

    # -- wiring ---------------------------------------------------------------------------

    def core(self, bot: BotSpec) -> IntradayCore:
        return IntradayCore(bot.strategy(), self.symbols, self.risk, INTRADAY_COSTS)

    def fingerprint(self, bot: BotSpec) -> str:
        blob = {"strategy": bot.strategy().label, "symbols": self.symbols,
                "risk": asdict(self.risk), "costs": asdict(INTRADAY_COSTS),
                "capital": self.capital}
        return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]

    # -- data -----------------------------------------------------------------------------

    def end_labelled(self) -> bool | None:
        """The provider's time-stamp convention, learned once from a complete day."""
        if not self.store.get_flag("iex_labels_known"):
            return None
        return self.store.get_flag("iex_end_labelled")

    def bars_for(self, day: date, now: datetime, report: IntradayReport, *,
                 refresh: bool) -> tuple[dict[str, list[IBar]], bool]:
        """Completed bars for `day` (fetched when missing or stale), and whether every symbol's
        data for the day is final. Only bars that have fully closed by `now` are used."""
        after_close = settled(day, now)
        all_final = True
        out = {}
        for sym in self.symbols:
            info = self.store.fetches(sym, day, day).get(day)
            stale = info is None or (not info.final and (
                after_close or now - info.fetched_at >= LIVE_REFRESH))
            if refresh and stale:
                rows = self.fetch_rows(sym, day, day)
                report.requests += 1
                labels = self.end_labelled()
                if labels is None and after_close:
                    learned = detect_end_labelled(rows)
                    if learned is not None:
                        self.store.set_flag("iex_end_labelled", learned, now)
                        self.store.set_flag("iex_labels_known", True, now)
                        labels = learned
                bars, rejected = parse_iex_rows(rows, labels)
                bars = [b for b in bars if b.day == day and b.end <= now]
                self.store.replace_ibars(sym, day, day, bars)
                kept = self.store.ibars(sym, day)
                complete = after_close and (
                    coverage(kept, day) >= MIN_COVERAGE
                    or now >= session(day).close_utc + FINAL_GRACE)  # stop retrying eventually
                self.store.mark_fetched(sym, day, len(kept), complete, now)
                info = self.store.fetches(sym, day, day).get(day)
                if rejected:
                    self._event(now, day, "data_rows_rejected", f"{sym}: {rejected} bad rows ignored")
            all_final = all_final and info is not None and info.final
            out[sym] = [b for b in self.store.ibars(sym, day) if b.end <= now]
        return out, all_final

    def prev_closes(self, day: date) -> dict[str, float | None]:
        """Yesterday's closing price: the last 5-minute price of the previous session (the same
        source live, in "today so far" and in history tests); the official close if missing."""
        prev = previous_trading_day(day)
        out: dict[str, float | None] = {}
        for sym in self.symbols:
            bars = self.store.ibars(sym, prev)
            out[sym] = bars[-1].close if bars else self.store.official_close(sym, prev)
        return out

    def pause_fn(self) -> Callable[[datetime], bool]:
        return lambda ts: self.store.flag_at("paused", ts)

    # -- the scheduled check --------------------------------------------------------------

    def run(self, now: datetime | None = None) -> IntradayReport:
        now = now or datetime.now(UTC)
        try:
            with self.store.run_lock("intraday"):
                return self._run(now)
        except EngineBusyError as exc:
            return IntradayReport("busy", messages=[str(exc)])

    def _run(self, now: datetime) -> IntradayReport:
        report = IntradayReport("idle")
        states = {b.id: self._load_or_start(b, now) for b in self.bots}
        target = latest_settled_session(now)
        due = sorted({d for st in states.values()
                      for d in self._days_due(st, target)})[:MAX_CATCH_UP_DAYS]
        for day in due:
            try:
                _, prev_final = self.bars_for(previous_trading_day(day), now, report, refresh=True)
                bars, final = self.bars_for(day, now, report, refresh=True)
                final = final and prev_final  # yesterday's close must be settled too
            except Exception as exc:  # provider trouble: nothing is committed, retry later
                report.status = "data_error"
                report.messages.append(f"{type(exc).__name__}: {exc}")
                self._event(now, day, "data_error", report.messages[-1])
                return report
            if not final:  # incomplete data shortly after the close: wait and retry
                report.status = "waiting"
                report.messages.append(f"{day}: waiting for complete 5-minute data")
                return report
            prev = self.prev_closes(day)
            for bot in self.bots:
                st = states[bot.id]
                if day not in self._days_due(st, target):
                    continue
                result = self.core(bot).run_day(st, day, bars, prev, final=True,
                                                paused=self.pause_fn())
                self.store.commit_bot_day(bot.id, st, result)
                report.committed.append((bot.id, day))
            report.status = "committed"
        today = ny_date(now)
        if (is_trading_day(today) and session(today).open_utc <= now and not settled(today, now)
                and any(st.start_day and st.start_day <= today for st in states.values())):
            try:
                self.bars_for(previous_trading_day(today), now, report, refresh=True)
                self.bars_for(today, now, report, refresh=True)
                if report.status == "idle":
                    report.status = "refreshed"
            except Exception as exc:  # noqa: BLE001 - display data only
                report.messages.append(f"live refresh failed: {type(exc).__name__}: {exc}")
        return report

    def _days_due(self, st: BotState, target: date) -> list[date]:
        start = next_trading_day(st.last_day) if st.last_day else st.start_day
        if start is None or start > target:
            return []
        return trading_days(start, target)

    def _load_or_start(self, bot: BotSpec, now: datetime) -> BotState:
        fp = self.fingerprint(bot)
        st = self.store.load_bot(bot.id)
        if st is not None and st.config_fingerprint != fp:
            if st.days == 0:  # never traded: restart cleanly with the new settings
                self.store.reset_bot(bot.id)
                st = None
            else:
                self._event(now, None, "bot_config_changed", f"{bot.id}: settings changed")
                st.config_fingerprint = fp
                self.store.save_bot(bot.id, st)
        if st is None:
            st = BotState.initial(self.capital, first_session_after(now))
            st.config_fingerprint = fp
            self.store.save_bot(bot.id, st)
            self._event(now, st.start_day, "bot_started",
                        f"{bot.id}: new ${self.capital:,.0f} simulated account, first day "
                        f"{st.start_day}")
        return st

    # -- owner actions --------------------------------------------------------------------

    def resume_bot(self, bot_id: str, now: datetime | None = None) -> None:
        """After a 10% drop the bot waits for you: this resets its peak and lets it buy again."""
        now = now or datetime.now(UTC)
        with self.store.run_lock("intraday"):
            st = self.store.load_bot(bot_id)
            if st is None or not st.halt_latched:
                return
            st.halt_latched, st.latched_days, st.peak_equity = False, 0, st.cash
            self.store.save_bot(bot_id, st)
            self._event(now, None, "bot_resumed", f"{bot_id}: new peak ${st.cash:,.2f}")

    # -- today so far (dashboard) ---------------------------------------------------------

    def today(self, bot: BotSpec, now: datetime | None = None) -> DayResult | None:
        """Replay today's stored bars through the bot without committing anything."""
        now = now or datetime.now(UTC)
        st = self.store.load_bot(bot.id)
        day = ny_date(now)
        if st is None or not is_trading_day(day) or now < session(day).open_utc:
            return None
        if st.start_day is None or st.start_day > day or (st.last_day and st.last_day >= day):
            return None
        caught_up = (st.last_day == previous_trading_day(day) if st.last_day
                     else st.start_day == day)
        if not caught_up:  # an earlier day isn't committed yet: don't show a stale balance
            return None
        bars = {s: [b for b in self.store.ibars(s, day) if b.end <= now] for s in self.symbols}
        return self.core(bot).run_day(copy.deepcopy(st), day, bars, self.prev_closes(day),
                                      final=now >= session(day).close_utc, paused=self.pause_fn())

    def _event(self, now: datetime, day: date | None, kind: str, detail: str) -> None:
        self.store.add_event(EngineEvent(now, day, kind, detail))
