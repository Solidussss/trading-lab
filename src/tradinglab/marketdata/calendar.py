"""NYSE trading calendar: holidays, early closes, special closures, DST-correct times.

Two sources are used, and they must agree before the live engine trades:
  1. This rules-based calendar (deterministic, offline, used by backtests).
  2. The data provider's bars: a bar on a day the rules call closed is flagged and ignored;
     a missing bar on a rules trading day makes the live engine wait for data (and, after
     a grace period, record a data gap - e.g. an unscheduled closure).
`cross_check()` compares the rules calendar with any external list of sessions.

All session times are expressed in America/New_York and converted with zoneinfo, so
daylight-saving transitions are handled by the tz database rather than by hand.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Unscheduled full-day closures (national mourning, weather, 9/11). Not rule-derivable.
SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
        date(2004, 6, 11),  # Reagan
        date(2007, 1, 2),  # Ford
        date(2012, 10, 29), date(2012, 10, 30),  # Hurricane Sandy
        date(2018, 12, 5),  # G.H.W. Bush
        date(2025, 1, 9),  # Carter
    }
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date | None:
    """Saturday holidays move to Friday, Sunday to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=128)
def nyse_holidays(year: int) -> frozenset[date]:
    h: set[date | None] = set()
    # New Year's Day: Sunday -> Monday; Saturday is NOT observed on the prior Friday.
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        h.add(ny + timedelta(days=1))
    elif ny.weekday() < 5:
        h.add(ny)
    if year >= 1998:
        h.add(_nth_weekday(year, 1, 0, 3))  # MLK Day
    h.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    h.add(_easter(year) - timedelta(days=2))  # Good Friday
    h.add(_last_weekday(year, 5, 0))  # Memorial Day
    if year >= 2022:
        h.add(_observed(date(year, 6, 19)))  # Juneteenth
    h.add(_observed(date(year, 7, 4)))  # Independence Day
    h.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    h.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    h.add(_observed(date(year, 12, 25)))  # Christmas
    return frozenset(d for d in h if d is not None and d.year == year)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year) and d not in SPECIAL_CLOSURES


def is_early_close(d: date) -> bool:
    if not is_trading_day(d):
        return False
    if d.month == 7 and d.day == 3:
        return True  # trading day before Independence Day
    if d.month == 11 and d.weekday() == 4 and d - timedelta(days=1) == _nth_weekday(d.year, 11, 3, 4):
        return True  # day after Thanksgiving
    if d.month == 12 and d.day == 24:
        return True  # Christmas Eve
    return False


@dataclass(frozen=True)
class Session:
    day: date
    open_utc: datetime
    close_utc: datetime
    early_close: bool

    @property
    def open_ny(self) -> datetime:
        return self.open_utc.astimezone(NY)

    @property
    def close_ny(self) -> datetime:
        return self.close_utc.astimezone(NY)


def session(d: date) -> Session:
    if not is_trading_day(d):
        raise ValueError(f"{d} is not an NYSE trading day.")
    early = is_early_close(d)
    open_local = datetime.combine(d, REGULAR_OPEN, tzinfo=NY)
    close_local = datetime.combine(d, EARLY_CLOSE if early else REGULAR_CLOSE, tzinfo=NY)
    return Session(d, open_local.astimezone(UTC), close_local.astimezone(UTC), early)


def trading_days(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def iter_trading_days(start: date) -> Iterator[date]:
    d = start
    while True:
        if is_trading_day(d):
            yield d
        d += timedelta(days=1)


def next_trading_day(d: date) -> date:
    return next(iter_trading_days(d + timedelta(days=1)))


def previous_trading_day(d: date) -> date:
    x = d - timedelta(days=1)
    while not is_trading_day(x):
        x -= timedelta(days=1)
    return x


def ny_date(ts: datetime) -> date:
    """Session date of an aware timestamp, in New York time."""
    if ts.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; pass an aware timestamp.")
    return ts.astimezone(NY).date()


def last_completed_session(now_utc: datetime, settle_minutes: int = 20) -> date:
    """Most recent session whose daily bar is final and queryable on the free SIP rule.

    A session counts as complete `settle_minutes` after its close, once the provider has
    published and corrected the day's bar. Acting earlier would use a bar that may still
    change.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware.")
    d = now_utc.astimezone(NY).date()
    for _ in range(15):
        if is_trading_day(d) and now_utc >= session(d).close_utc + timedelta(minutes=settle_minutes):
            return d
        d -= timedelta(days=1)
    raise RuntimeError("no completed session found in the last 15 days")


@dataclass(frozen=True)
class CalendarMismatch:
    only_in_rules: tuple[date, ...]
    only_in_broker: tuple[date, ...]

    @property
    def ok(self) -> bool:
        return not self.only_in_rules and not self.only_in_broker


def cross_check(start: date, end: date, broker_days: Iterable[date]) -> CalendarMismatch:
    rules = set(trading_days(start, end))
    broker = {d for d in broker_days if start <= d <= end}
    return CalendarMismatch(tuple(sorted(rules - broker)), tuple(sorted(broker - rules)))
