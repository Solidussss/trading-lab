"""Intraday (5-minute) bars: the data model, Tiingo IEX parsing, and chunked fetching.

Provider facts (checked 2026-10-10, docs/DATA.md):
  * Endpoint: GET https://api.tiingo.com/iex/<ticker>/prices?startDate=&endDate=
    &resampleFreq=5min&columns=open,high,low,close,volume&afterHours=false
  * Intraday history from August 2017. Free plan: 50 requests/hour, 1,000/day, 1 GB/month,
    internal (personal) use only.
  * Prices are trades on the IEX exchange only. For SPY/QQQ/IWM, which trade heavily on IEX,
    5-minute prices track the consolidated market closely; volume is IEX-only, so VWAP here is
    "IEX VWAP" (a good approximation for these funds, not the official consolidated VWAP).
  * Older documentation mentions a 2,000-point cap per request. Requests are therefore chunked
    to ~15 sessions (~1,170 bars) and a response near the cap is split and re-requested.

Bars are normalised to: UTC timestamp of the bar's START, regular session only. The provider's
labelling convention (start vs end of the interval) is detected per day: a bar stamped at the
session close with none at the open means end-labelled, and the series is shifted back.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from tradinglab.marketdata.calendar import NY, is_trading_day, session, trading_days

BAR_MINUTES = 5
BAR = timedelta(minutes=BAR_MINUTES)
INTRADAY_HISTORY_START = date(2017, 8, 1)
CHUNK_SESSIONS = 15
POINT_CAP = 2000
MIN_COVERAGE = 0.90  # a symbol needs >=90% of a day's expected bars to be traded that day


@dataclass(frozen=True, slots=True)
class IBar:
    ts: datetime  # UTC, start of the 5-minute interval
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def end(self) -> datetime:
        return self.ts + BAR

    @property
    def day(self) -> date:
        return self.ts.astimezone(NY).date()

    @property
    def ny_time(self) -> str:
        return self.ts.astimezone(NY).strftime("%H:%M")


class IntradayDataError(RuntimeError):
    pass


def expected_bars(day: date) -> int:
    s = session(day)
    return int((s.close_utc - s.open_utc) / BAR)


def coverage(bars: Sequence[IBar], day: date) -> float:
    return len(bars) / expected_bars(day) if is_trading_day(day) else 0.0


def _parse_ts(v: Any) -> datetime:
    ts = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if ts.tzinfo is None:  # never guess silently: the provider sends offsets
        raise IntradayDataError(f"timestamp without timezone: {v!r}")
    return ts.astimezone(UTC)


def _valid(o: float, h: float, lo: float, c: float) -> bool:
    vals = (o, h, lo, c)
    return (all(math.isfinite(x) and x > 0 for x in vals) and lo <= min(o, c)
            and h >= max(o, c))


def detect_end_labelled(rows: Iterable[dict[str, Any]]) -> bool | None:
    """From a COMPLETE session's rows: True if stamped at the end of each 5 minutes, False if
    at the start, None if this day can't tell (e.g. a bar is missing at either edge)."""
    votes = set()
    by_day: dict[date, set[datetime]] = {}
    for r in rows:
        try:
            ts = _parse_ts(r["date"])
        except (KeyError, ValueError, IntradayDataError):
            continue
        by_day.setdefault(ts.astimezone(NY).date(), set()).add(ts)
    for day, stamps in by_day.items():
        if not is_trading_day(day):
            continue
        if len(stamps) != expected_bars(day):  # only a complete day can tell reliably
            continue
        s = session(day)
        at_open, at_close = s.open_utc in stamps, s.close_utc in stamps
        if at_open != at_close:
            votes.add(at_close)
    return votes.pop() if len(votes) == 1 else None


def parse_iex_rows(rows: Iterable[dict[str, Any]], end_labelled: bool | None = None
                   ) -> tuple[list[IBar], int]:
    """Provider rows -> clean, start-labelled, regular-session bars. Returns (bars, rejected).
    end_labelled: the provider's convention once known; None guesses per day (complete days)."""
    raw: dict[datetime, IBar] = {}
    rejected = 0
    for r in rows:
        try:
            ts = _parse_ts(r["date"])
            o, h, lo, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            v = float(r.get("volume") or 0.0)
        except (KeyError, TypeError, ValueError, IntradayDataError):
            rejected += 1
            continue
        if not _valid(o, h, lo, c) or not math.isfinite(v) or v < 0:
            rejected += 1
            continue
        raw[ts] = IBar(ts, o, h, lo, c, v)  # duplicates: last one wins
    by_day: dict[date, list[IBar]] = {}
    for b in raw.values():
        by_day.setdefault(b.ts.astimezone(NY).date(), []).append(b)
    out: list[IBar] = []
    for day, bars in by_day.items():
        if not is_trading_day(day):
            rejected += len(bars)
            continue
        s = session(day)
        stamps = {b.ts for b in bars}
        lab = end_labelled if end_labelled is not None else (
            s.close_utc in stamps and s.open_utc not in stamps)
        shift = BAR if lab else timedelta(0)
        for b in bars:
            ts = b.ts - shift
            if s.open_utc <= ts and ts + BAR <= s.close_utc and (ts - s.open_utc) % BAR == timedelta(0):
                out.append(IBar(ts, b.open, b.high, b.low, b.close, b.volume))
    out.sort(key=lambda b: b.ts)
    return out, rejected


def split_by_day(bars: Iterable[IBar]) -> dict[date, list[IBar]]:
    out: dict[date, list[IBar]] = {}
    for b in bars:
        out.setdefault(b.day, []).append(b)
    return out


def chunks(start: date, end: date, size: int = CHUNK_SESSIONS) -> list[tuple[date, date]]:
    days = trading_days(start, end)
    return [(days[i], days[min(i + size, len(days)) - 1]) for i in range(0, len(days), size)]


# (symbol, start, end) -> provider rows
RowFetcher = Callable[[str, date, date], list[dict[str, Any]]]


def fetch_range(fetch: RowFetcher, symbol: str, start: date, end: date
                ) -> tuple[list[IBar], int, int]:
    """Fetch [start, end] in chunks; a chunk that comes back at the point cap is halved and
    re-fetched so nothing is silently truncated. Returns (bars, rejected_rows, requests)."""
    bars: list[IBar] = []
    rejected = requests = 0
    pending = chunks(start, end)
    while pending:
        a, b = pending.pop(0)
        rows = fetch(symbol, a, b)
        requests += 1
        if len(rows) >= POINT_CAP - 10 and a < b:
            days = trading_days(a, b)
            mid = len(days) // 2
            pending[:0] = [(days[0], days[mid - 1]), (days[mid], days[-1])]
            continue
        got, rej = parse_iex_rows(rows)
        bars.extend(got)
        rejected += rej
    bars.sort(key=lambda x: x.ts)
    return bars, rejected, requests
