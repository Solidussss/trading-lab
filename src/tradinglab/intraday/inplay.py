"""Stocks in play: bots that trade the day's busiest stocks instead of the three funds.

The idea comes from the day-trader skill pack's morning scanner: most stocks do nothing on most
days, so look only at the ones that are moving today. Each session:

  1. Scan. Between 9:33 and 9:45 am New York time, ONE quote request covers the whole universe
     (intraday/universe.py). A stock qualifies when it opened 1% to 50% above yesterday's
     close, is trading above its open, and costs at least $5. The five biggest gaps are the
     day's picks. They are stored with the numbers they were chosen on, and never re-chosen:
     a missed scan means the bots sit that day out. The window closes at 9:45 on purpose: the
     bots' first possible signal is the bar that closes at 9:50, so a pick is always made
     before anything it could trade on.
  2. History. Each pick's 5-minute bars for the 15 sessions before today are loaded once (one
     request per stock), for the odds replay (intraday/replay.py).
  3. Display. During the day the picks' bars refresh as the request budget allows, which on
     the free data plan is about once an hour per stock. The three-fund bots always come
     first, and this runner never waits on the request limiter: when no request is free it
     simply tries again at the next check.
  4. Commit. After the close the picks' final bars are fetched and each bot's day is replayed
     through the same core as every other bot, then committed. The committed day is the
     result; what the page shows during the day is a preview that can lag it.

Like every bot here it is pretend money on real prices, and decisions come only from bars that
had closed at the time. There is no history test: the data plan cannot load years of 5-minute
prices for a hundred stocks, so these bots are judged on new days only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from tradinglab.config import Settings
from tradinglab.intraday.core import INTRADAY_COSTS
from tradinglab.intraday.live import (
    FINAL_GRACE,
    MAX_CATCH_UP_DAYS,
    BotSpec,
    IntradayReport,
    IntradayRunner,
    RowFetcher,
    latest_settled_session,
    settled,
)
from tradinglab.intraday.universe import IN_PLAY
from tradinglab.marketdata.calendar import (
    NY,
    is_trading_day,
    ny_date,
    previous_trading_day,
    session,
    trading_days,
)
from tradinglab.marketdata.intraday import chunks, parse_iex_rows, split_by_day

log = logging.getLogger(__name__)

UNIVERSE_ID = "in_play"
SCAN_AFTER = timedelta(minutes=3)  # let the opening prints settle
# After this the day is sat out. It must not be later than the bots' opening range (15 minutes):
# the first signal comes from the bar that closes 20 minutes after the open, so every pick is
# made before any price it could be traded on.
SCAN_UNTIL = timedelta(minutes=15)
HISTORY_SESSIONS = 15  # one data request per pick (marketdata.intraday.CHUNK_SESSIONS)
DISPLAY_REFRESH = timedelta(minutes=30)
OWN_PER_HOUR = 6  # default scan + display requests in any hour (the service works its own out
FUND_POLL = 3  # from how many funds are still polled: engine/runtime.py)
MIN_GAP = 0.01
MAX_GAP = 0.50  # a bigger "gap" is a split or a bad previous close, not a stock in play
MIN_PRICE = 5.0
TOP = 5
MIN_USABLE = 0.5  # share of the universe that must have usable quotes for a scan to count

_RULES_SCAN = ("A few minutes after the open, scans about 100 heavily traded stocks and keeps the "
               "five that opened furthest above yesterday's close (at least 1%) and are still "
               "rising.")

IN_PLAY_BOTS: tuple[BotSpec, ...] = (
    BotSpec("sip", "orb", {"range_minutes": 15}, universe=UNIVERSE_ID,
            name="Stocks in play breakout",
            summary="Scans about 100 busy stocks each morning and trades opening breakouts in the "
                    "five that gapped up the most.",
            rules=(_RULES_SCAN,
                   "For each of those, notes the highest and lowest price of the first 15 minutes.",
                   "Buys if a 5-minute bar later closes above that high.",
                   "Safety stop at the first 15 minutes' low.",
                   "Sells at the close, or earlier if the stop is hit. At most one trade per "
                   "stock a day.")),
    BotSpec("sip_odds", "orb_odds", universe=UNIVERSE_ID,
            name="Stocks in play, odds-checked",
            summary="The same morning scan, but it only takes a breakout when a replay of the "
                    "stock's recent days says the odds are on its side.",
            rules=(_RULES_SCAN,
                   "When one of them closes above its first 15 minutes' high, replays the rest of "
                   "the day 2,000 times using what that stock did at the same times on up to 15 "
                   "earlier days.",
                   "Buys only if the replays' average result is at least a tenth of the amount "
                   "risked. Otherwise it passes, and says so.",
                   "Passes on any stock with fewer than 10 earlier days of prices.",
                   "Safety stop at the first 15 minutes' low. Sells at the close, or earlier if "
                   "the stop is hit.")),
)

# The two fund strategies that made money before costs in the 2017-2024 history tests, moved to
# where a day's move is several times bigger than the cost of trading it (docs/RESEARCH.md).
IN_PLAY_BOTS += (
    BotSpec("sip_trend", "vwap_trend", universe=UNIVERSE_ID,
            name="Stocks in play, ride the trend",
            summary="The same morning scan. On a stock that is climbing, it buys when the price "
                    "dips back to the day's average price and bounces.",
            rules=(_RULES_SCAN,
                   "After 10 am, only while the stock is above where it opened and its average "
                   "price for the day (VWAP) is rising.",
                   "Buys when a 5-minute bar dips to that average but closes above it.",
                   "Sells if the price closes clearly below the average (the trend broke), or at "
                   "the close.",
                   "Safety stop just under the average. Up to two trades per stock a day.")),
    BotSpec("sip_noise", "noise", universe=UNIVERSE_ID,
            name="Stocks in play, past normal noise",
            summary="The same morning scan. It buys only when a stock's climb today is clearly "
                    "bigger than its normal wobble at that time of day.",
            rules=(_RULES_SCAN,
                   "For every time of day, works out how far the stock usually is from its open, "
                   "averaged over its last 14 trading days. Anything inside that is 'noise'.",
                   "Checks every 30 minutes from 10 am. Buys when the price closes above the top "
                   "of the noise and above the day's average price (VWAP).",
                   "At each later check, sells if the price has dropped back below either line. "
                   "Sells at the close at the latest.",
                   "Emergency stop 5% down. Passes on a stock with fewer than 10 earlier days of "
                   "prices.")),
)

QuoteFetcher = Callable[[Sequence[str]], list]


@dataclass(frozen=True)
class Pick:
    symbol: str
    gap: float  # open vs yesterday's close
    last: float
    open: float
    prev_close: float


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def usable(quotes: Iterable[Mapping[str, Any]], universe: Collection[str], day: date | None = None
           ) -> dict[str, tuple[float, float, float]]:
    """symbol -> (last, open, yesterday's close) for the rows a scan can use. Rows for symbols
    outside `universe`, with missing or bad numbers, or (when `day` is given) stamped on another
    day are left out."""
    wanted = {s.upper() for s in universe}
    out: dict[str, tuple[float, float, float]] = {}
    for q in quotes:
        sym = str(q.get("ticker", "")).upper()
        if sym not in wanted:
            continue
        last = _num(q.get("tngoLast")) or _num(q.get("last"))
        opened, prev = _num(q.get("open")), _num(q.get("prevClose"))
        if last is None or opened is None or prev is None:
            continue
        if day is not None:
            try:
                stamp = datetime.fromisoformat(str(q.get("timestamp")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp.tzinfo is None or stamp.astimezone(NY).date() != day:
                continue  # yesterday's quote: the stock has not traded yet today
        out[sym] = (last, opened, prev)
    return out


def pick(quotes: Iterable[Mapping[str, Any]], universe: Collection[str], day: date | None = None, *,
         min_gap: float = MIN_GAP, max_gap: float = MAX_GAP, min_price: float = MIN_PRICE,
         top: int = TOP) -> list[Pick]:
    """The day's stocks in play from one batch of quotes (see `usable` for what is ignored)."""
    found = []
    for sym, (last, opened, prev) in usable(quotes, universe, day).items():
        gap = opened / prev - 1
        if min_gap <= gap <= max_gap and last > opened and last >= min_price:
            found.append(Pick(sym, gap, last, opened, prev))
    return sorted(found, key=lambda p: (-p.gap, p.symbol))[:top]


class InPlayRunner(IntradayRunner):
    """Runs the stocks-in-play bots. Shares the stores, the core, the costs and the risk rules
    with the three-fund runner; differs only in which symbols a day uses."""

    lock_name = "inplay"

    def __init__(self, settings: Settings, store, fetch_rows: RowFetcher,
                 fetch_quotes: QuoteFetcher, bots: Sequence[BotSpec] = IN_PLAY_BOTS,
                 universe: Sequence[str] = IN_PLAY,
                 budget: Callable[[], int] = lambda: 999, own_per_hour: int = OWN_PER_HOUR,
                 fund_poll: int = FUND_POLL) -> None:
        super().__init__(settings, store, fetch_rows, bots)
        self.own_per_hour = own_per_hour  # scan + display requests allowed in any hour
        self.fund_poll = fund_poll  # requests the fund bots need at their next check
        self.fetch_quotes = fetch_quotes
        self.universe = tuple(universe)
        self.budget = budget  # data requests that can be made right now without waiting
        self.symbols = []  # no fixed list: see symbols_on()
        self.live_symbols = []
        self._spent: deque[datetime] = deque()  # when this runner's daytime requests were made
        self._scan_problem = ""

    # -- wiring ---------------------------------------------------------------------------

    def symbols_on(self, day: date) -> list[str]:
        payload = self.store.picks(UNIVERSE_ID, day)
        return [p["symbol"] for p in payload["picks"]] if payload else []

    def fingerprint(self, bot: BotSpec) -> str:
        blob = {"strategy": bot.strategy().label, "universe": sorted(self.universe),
                "scan": [MIN_GAP, MAX_GAP, MIN_PRICE, TOP], "risk": asdict(self.risk),
                "costs": asdict(INTRADAY_COSTS), "capital": self.capital}
        return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]

    # -- the request budget ---------------------------------------------------------------

    def _allow(self, n: int, now: datetime) -> bool:
        """May `n` daytime requests be made now? Only within this runner's own hourly allowance,
        and only while the three-fund bots' next poll would still go straight through."""
        while self._spent and now - self._spent[0] >= timedelta(hours=1):
            self._spent.popleft()
        return len(self._spent) + n <= self.own_per_hour and self.budget() >= n + self.fund_poll

    def _spend(self, n: int, now: datetime) -> None:
        self._spent.extend([now] * n)

    # -- the scheduled check --------------------------------------------------------------

    def _run(self, now: datetime) -> IntradayReport:
        report = IntradayReport("idle")
        states = {b.id: self._load_or_start(b, now) for b in self.bots}
        today = ny_date(now)
        live = any(st.start_day and st.start_day <= today for st in states.values())
        if live:  # a bot that starts tomorrow needs no picks today
            self._scan(now, report)
        target = latest_settled_session(now)
        due = sorted({d for st in states.values()
                      for d in self._days_due(st, target)})[:MAX_CATCH_UP_DAYS]
        for day in due:
            payload = self.store.picks(UNIVERSE_ID, day)
            if payload is None:  # the service was not running that morning
                payload = self._save_picks(day, now, [], 0, "the morning scan did not run")
            syms = [p["symbol"] for p in payload["picks"]]
            try:
                wait, failed = self._prepare(day, syms, now, report)
                syms = [s for s in syms if s not in failed]
                bars, final = self.bars_for(day, now, report, refresh=False, symbols=syms)
            except Exception as exc:  # provider trouble: nothing is committed, retry later
                report.status = "data_error"
                report.messages.append(f"{type(exc).__name__}: {exc}")
                self._event(now, day, "data_error", f"stocks in play: {report.messages[-1]}")
                return report
            if wait or not final:
                report.status = "waiting"
                report.messages.append(f"{day}: {wait or 'waiting for complete 5-minute data'}")
                return report
            prev = self.prev_closes(day, syms)
            for bot in self.bots:
                st = states[bot.id]
                if day not in self._days_due(st, target):
                    continue
                result = self.core(bot, syms).run_day(st, day, bars, prev, final=True,
                                                      paused=self.pause_fn(),
                                                      history=self.history(bot, day, syms))
                for sym in failed:  # shown on the bot's page as "sat out", with the reason
                    result.excluded[sym] = "its prices could not be loaded"
                self.store.commit_bot_day(bot.id, st, result)
                report.committed.append((bot.id, day))
            report.status = "committed"
        if live:
            self._refresh_today(now, report)
        return report

    # -- the morning scan -----------------------------------------------------------------

    def _scan(self, now: datetime, report: IntradayReport) -> None:
        day = ny_date(now)
        if not is_trading_day(day) or self.store.picks(UNIVERSE_ID, day) is not None:
            return
        opened = session(day).open_utc
        if now < opened + SCAN_AFTER or now >= session(day).close_utc:
            return
        if now > opened + SCAN_UNTIL:
            why = self._scan_problem or "the morning scan missed its window (9:33 to 9:45 am New York time)"
            self._save_picks(day, now, [], 0, why)
            self._scan_problem = ""
            return
        if self.budget() < 1:  # never wait on the limiter: the next check is minutes away
            report.messages.append("scan postponed: no data request free")
            return
        try:
            quotes = self.fetch_quotes(self.universe)
            report.requests += 1
            self._spend(1, now)
            good = usable(quotes, self.universe, day)
        except Exception as exc:  # noqa: BLE001 - try again on the next check, inside the window
            self._scan_problem = "the quote feed could not be reached during the scan window"
            report.messages.append(f"scan failed: {type(exc).__name__}: {exc}")
            self._event(now, day, "data_error", f"stocks in play scan: {type(exc).__name__}: {exc}")
            return
        if len(good) < MIN_USABLE * len(self.universe):
            # An empty or half-empty reply is a feed problem, not a quiet market: don't record
            # it as "nothing gapped". One sample row is logged to show what came back.
            self._scan_problem = (f"the quote feed returned usable prices for only {len(good)} of "
                                  f"{len(self.universe)} stocks")
            sample = next((dict(q) for q in quotes if isinstance(q, Mapping)), None)
            log.warning("stocks in play %s: %s; sample row: %.300r", day, self._scan_problem, sample)
            report.messages.append(f"scan failed: {self._scan_problem}")
            self._event(now, day, "data_error", f"stocks in play scan: {self._scan_problem}")
            return
        picks = pick(quotes, self.universe, day)
        note = "" if picks else "nothing gapped up 1% and kept rising"
        self._scan_problem = ""
        self._save_picks(day, now, picks, len(good), note)
        if report.status == "idle":
            report.status = "scanned"

    def _save_picks(self, day: date, now: datetime, picks: Sequence[Pick], scanned: int,
                    note: str) -> dict[str, Any]:
        payload = {"at": now.astimezone(UTC).isoformat(timespec="seconds"), "scanned": scanned,
                   "picks": [asdict(p) for p in picks], "note": note}
        self.store.save_picks(UNIVERSE_ID, day, payload)
        names = ", ".join(f"{p.symbol} {p.gap:+.1%}" for p in picks) or note
        self._event(now, day, "in_play_scan", f"{scanned} stocks scanned: {names}")
        log.info("stocks in play %s: %s", day, names)
        return payload

    # -- data -----------------------------------------------------------------------------

    def _history_gap(self, sym: str, day: date) -> list[tuple[date, date]]:
        """The request ranges still needed for `sym`'s earlier sessions (empty when loaded). A
        day counts as loaded only once its bars are final."""
        prev = previous_trading_day(day)
        start = prev
        for _ in range(HISTORY_SESSIONS - 1):
            start = previous_trading_day(start)
        have = self.store.fetches(sym, start, prev)
        missing = [d for d in trading_days(start, prev) if d not in have or not have[d].final]
        return chunks(missing[0], missing[-1]) if missing else []

    def _fetch_history(self, sym: str, parts: Sequence[tuple[date, date]], now: datetime,
                       report: IntradayReport) -> None:
        """Bars are stored only once every part has arrived, so a failure leaves nothing half
        written and a retry starts clean."""
        bars = []
        for a, b in parts:
            rows = self.fetch_rows(sym, a, b)
            report.requests += 1
            got, _rejected = parse_iex_rows(rows, self.end_labelled())
            bars.extend(got)
        first, last = parts[0][0], parts[-1][1]
        self.store.replace_ibars(sym, first, last, bars)
        by_day = split_by_day(bars)
        for d in trading_days(first, last):
            self.store.mark_fetched(sym, d, len(by_day.get(d, [])), True, now)

    def _prepare(self, day: date, syms: Sequence[str], now: datetime,
                 report: IntradayReport) -> tuple[str, list[str]]:
        """After the close: final bars and earlier sessions for each pick. Returns why the day
        can't be committed yet ("" when it can) and the stocks to leave out. Never sleeps on the
        limiter. A stock whose prices still can't be fetched three hours after the close sits
        the day out instead of blocking the others."""
        late = now >= session(day).close_utc + FINAL_GRACE
        failed: list[str] = []
        for sym in syms:
            try:
                parts = self._history_gap(sym, day)
                if parts:
                    if self.budget() < len(parts):
                        return "waiting for a free data request", failed
                    self._fetch_history(sym, parts, now, report)
            except Exception as exc:  # noqa: BLE001
                if not late:
                    raise
                self._event(now, day, "data_error", f"{sym}: earlier days not loaded "
                                                    f"({type(exc).__name__}); traded without them")
            try:
                info = self.store.fetches(sym, day, day).get(day)
                if info is None or not info.final:
                    if self.budget() < 1:
                        return "waiting for a free data request", failed
                    self.bars_for(day, now, report, refresh=True, symbols=[sym])
            except Exception as exc:  # noqa: BLE001
                if not late:
                    raise
                failed.append(sym)
                self._event(now, day, "data_error", f"{sym}: no prices after "
                                                    f"{type(exc).__name__}; sat out")
        return "", failed

    def _refresh_today(self, now: datetime, report: IntradayReport) -> None:
        """Daytime, display only: load each pick's earlier sessions, then keep its bars fresh,
        one request at a time and only inside the request allowance."""
        day = ny_date(now)
        if not is_trading_day(day) or now < session(day).open_utc or settled(day, now):
            return
        try:
            for sym in self.symbols_on(day):
                parts = self._history_gap(sym, day)
                if parts and self._allow(len(parts), now):
                    self._fetch_history(sym, parts, now, report)
                    self._spend(len(parts), now)
                info = self.store.fetches(sym, day, day).get(day)
                stale = info is None or (not info.final
                                         and now - info.fetched_at >= DISPLAY_REFRESH)
                if stale and self._allow(1, now):
                    self.bars_for(day, now, report, refresh=True, symbols=[sym],
                                  live_refresh=DISPLAY_REFRESH)
                    self._spend(1, now)
                    if report.status == "idle":
                        report.status = "refreshed"
        except Exception as exc:  # noqa: BLE001 - display data only
            report.messages.append(f"live refresh failed: {type(exc).__name__}: {exc}")

    # -- yesterday's close ----------------------------------------------------------------

    def prev_closes(self, day: date, symbols: Sequence[str] | None = None
                    ) -> dict[str, float | None]:
        """The last 5-minute price of the previous session when those bars are loaded; until
        then (and for a stock with no bars that day) the close the scan saw."""
        syms = self.symbols_on(day) if symbols is None else list(symbols)
        out = super().prev_closes(day, syms)
        payload = self.store.picks(UNIVERSE_ID, day) or {"picks": []}
        for p in payload["picks"]:
            if p["symbol"] in out and out[p["symbol"]] is None:
                out[p["symbol"]] = p.get("prev_close")
        return out
