"""Historical daily bars from Alpaca's market-data API (free Basic plan aware).

Free Basic plan facts this module is built around (verified Oct 2026, see docs/DATA.md):
  * Historical data back to 2016.
  * 200 historical API calls per minute.
  * Real-time data is IEX only (~2% of consolidated volume).
  * Consolidated SIP history IS available on the free plan, provided the request's end is
    at least 15 minutes in the past.

Consequence: a daily-bar strategy evaluated after the close can use full consolidated (SIP)
daily bars for free. This module therefore requests feed="sip" and always clamps `end`
to at least 16 minutes ago. IEX-only bars are never used for signals because their
volume and, on thin days, their OHLC differ from the consolidated tape.

Adjustment defaults to "split": the Alpaca paper broker does not credit dividends, so
research uses split-adjusted price returns to stay comparable with paper results. This
understates the total return of buy-and-hold ETFs by roughly their dividend yield.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from tradinglab.config import Settings
from tradinglab.marketdata.bars import Bar, BarSeries, DatasetMeta
from tradinglab.marketdata.calendar import last_completed_session, ny_date
from tradinglab.safety.endpoints import MARKET_DATA_HOST, validate_data_url
from tradinglab.safety.network_guard import install_network_guard

log = logging.getLogger(__name__)

FREE_PLAN_SIP_DELAY = timedelta(minutes=15)
SAFETY_MARGIN = timedelta(minutes=1)
FREE_PLAN_CALLS_PER_MINUTE = 200
HISTORY_START = date(2016, 1, 1)


class RateLimiter:
    """Sliding-window limiter; stays under the plan limit with headroom."""

    def __init__(self, max_calls: int = 150, per_seconds: float = 60.0, clock=time.monotonic,
                 sleep=time.sleep) -> None:
        self.max_calls, self.per = max_calls, per_seconds
        self._calls: deque[float] = deque()
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.per:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                self._sleep(self.per - (now - self._calls[0]) + 0.01)


def clamp_end_for_free_sip(end: datetime, now_utc: datetime) -> datetime:
    latest = now_utc - FREE_PLAN_SIP_DELAY - SAFETY_MARGIN
    return min(end, latest)


class AlpacaDailyBars:
    def __init__(self, settings: Settings, *, client: Any | None = None,
                 limiter: RateLimiter | None = None, feed: str = "sip",
                 adjustment: str = "split") -> None:
        install_network_guard()
        if feed not in ("sip", "iex"):
            raise ValueError("feed must be 'sip' or 'iex'")
        if adjustment not in ("raw", "split", "dividend", "all"):
            raise ValueError("invalid adjustment")
        self.feed, self.adjustment = feed, adjustment
        self.limiter = limiter or RateLimiter()
        if client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            url = f"https://{MARKET_DATA_HOST}"
            validate_data_url(url)
            client = StockHistoricalDataClient(
                api_key=settings.alpaca_api_key.get(),
                secret_key=settings.alpaca_secret_key.get(),
                url_override=url,
            )
            from tradinglab.broker.alpaca_paper import disable_env_proxies

            disable_env_proxies(client)
        self._client = client

    def build_request(self, **kwargs: Any) -> Any:
        """Build an alpaca-py StockBarsRequest (overridable in tests)."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        return StockBarsRequest(
            symbol_or_symbols=kwargs["symbols"],
            timeframe=TimeFrame.Day,
            start=kwargs["start"],
            end=kwargs["end"],
            adjustment=Adjustment(kwargs["adjustment"]),
            feed=DataFeed(kwargs["feed"]),
        )

    def fetch(self, symbols: Sequence[str], start: date, end: date,
              now_utc: datetime | None = None) -> tuple[dict[str, BarSeries], dict[str, DatasetMeta]]:
        now_utc = now_utc or datetime.now(UTC)
        start = max(start, HISTORY_START)
        # Never request (or keep) a session that is still in progress or not yet settled:
        # its bar would be cached as if final and later revised.
        end = min(end, last_completed_session(now_utc))
        end_dt = clamp_end_for_free_sip(
            datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC), now_utc
        )
        if end_dt <= datetime.combine(start, datetime.min.time(), tzinfo=UTC):
            raise ValueError("requested range ends before it starts after the SIP delay clamp")
        self.limiter.acquire()
        req = self.build_request(
            symbols=list(symbols),
            start=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
            end=end_dt,
            adjustment=self.adjustment,
            feed=self.feed,
        )
        # alpaca-py follows next_page_token internally; one logical call may be several
        # HTTP requests, which the limiter's headroom (150 of 200) absorbs for 3 symbols.
        resp = self._client.get_stock_bars(req)
        data = getattr(resp, "data", resp)
        fetched_at = now_utc.isoformat()
        out: dict[str, BarSeries] = {}
        metas: dict[str, DatasetMeta] = {}
        for sym in symbols:
            raw = data.get(sym, [])
            bars = [bar for bar in (_to_bar(b) for b in raw) if start <= bar.day <= end]
            series = BarSeries(sym, bars)
            out[sym] = series
            metas[sym] = DatasetMeta(sym, "alpaca", self.feed, self.adjustment,
                                     start.isoformat(), end.isoformat(), fetched_at, len(series))
            if series.issues:
                log.warning("%s: %d data issues (first: %s)", sym, len(series.issues),
                            series.issues[0])
        return out, metas


def _to_bar(b: Any) -> Bar:
    ts = b.timestamp
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return Bar(ny_date(ts), float(b.open), float(b.high), float(b.low), float(b.close),
               float(b.volume))
