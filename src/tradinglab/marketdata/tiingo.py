"""Tiingo end-of-day price client (stdlib HTTP only).

Provider facts this module is built around (checked 2026-10-10, see docs/DATA.md):
  * Free plan: 50 requests/hour, 1,000/day, 500 unique symbols/month; 30+ years of daily
    history; ETFs included. Licensed for personal internal use (no redistribution).
  * US equity/ETF EOD prices are published ~5:30 pm ET, with corrections until ~8 pm ET.
  * Each daily row has raw open/high/low/close/volume plus divCash and splitFactor (and
    CRSP-style adjusted fields, which we do not need because corporate actions are
    applied explicitly by the simulator).
  * Auth via `Authorization: Token <token>` header (never in the URL, so it cannot leak
    into logs or error messages).

The engine fetches one request per symbol per day (3 requests for SPY/QQQ/IWM).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from tradinglab.config import Secret
from tradinglab.marketdata.bars import Bar, BarSeries, DatasetMeta
from tradinglab.safety.endpoints import DATA_PROVIDER_URL
from tradinglab.safety.network_guard import install_network_guard, policy

log = logging.getLogger(__name__)

HISTORY_START = date(2016, 1, 1)


class DataProviderError(RuntimeError):
    """The provider returned an error, malformed data, or was unreachable after retries."""


class RateLimiter:
    """Sliding-window limiter with headroom under the plan limit."""

    def __init__(self, max_calls: int, per_seconds: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
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


MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # ~500 days x 3 symbols is well under 1 MB
MAX_RETRY_AFTER_SECONDS = 120  # a longer server-requested wait ends the run (retry tomorrow)

# (status, body, headers); test doubles may return just (status, body).
HttpGet = Callable[[str, dict[str, str], float], tuple]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward the Authorization header (our token)
    to whatever host or scheme the redirect names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _urllib_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes, dict]:
    # ProxyHandler({}) ignores proxy env/OS settings: the egress guard must see the real host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp else b""
        return exc.code, body, dict(exc.headers or {})


class TiingoClient:
    def __init__(self, token: Secret, *, http_get: HttpGet = _urllib_get,
                 limiter: RateLimiter | None = None, timeout: float = 30.0,
                 max_attempts: int = 4, sleep: Callable[[float], None] = time.sleep) -> None:
        install_network_guard()
        self._token = token
        self._http_get = http_get
        # Free plan: 50/hour. Stay at 40/hour for headroom (daily job uses 3).
        self.limiter = limiter or RateLimiter(max_calls=40, per_seconds=3600.0)
        self.timeout, self.max_attempts, self._sleep = timeout, max_attempts, sleep

    def daily(self, symbol: str, start: date, end: date) -> BarSeries:
        if end < start:
            raise ValueError("end before start")
        start = max(start, HISTORY_START)
        query = urllib.parse.urlencode({"startDate": start.isoformat(),
                                        "endDate": end.isoformat(), "format": "json",
                                        "resampleFreq": "daily"})
        url = f"{DATA_PROVIDER_URL}/tiingo/daily/{urllib.parse.quote(symbol.lower())}/prices?{query}"
        rows = self._get_json(url)
        if not isinstance(rows, list):
            raise DataProviderError(f"{symbol}: expected a list of rows")
        return BarSeries(symbol, [parse_row(r) for r in rows])

    def history(self, symbols: Sequence[str], start: date, end: date
                ) -> tuple[dict[str, BarSeries], dict[str, DatasetMeta]]:
        out, metas = {}, {}
        fetched_at = datetime.now().astimezone().isoformat()
        for sym in symbols:
            s = self.daily(sym, start, end)
            out[sym] = s
            metas[sym] = DatasetMeta(sym, "tiingo", "eod", "raw", start.isoformat(),
                                     end.isoformat(), fetched_at, len(s))
            if s.issues:
                log.warning("%s: %d data issues (first: %s)", sym, len(s.issues), s.issues[0])
        return out, metas

    # ------------------------------------------------------------------------------------

    def _get_json(self, url: str) -> Any:
        policy().check_url(url)  # explicit check in addition to the process-wide guard
        headers = {"Authorization": f"Token {self._token.get()}",
                   "Content-Type": "application/json", "User-Agent": "trading-lab/0.1"}
        delay = 2.0
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.acquire()
            resp_headers: dict = {}
            try:
                result = self._http_get(url, headers, self.timeout)
                status, body = result[0], result[1]
                resp_headers = result[2] if len(result) > 2 else {}
            except (TimeoutError, OSError) as exc:  # network trouble: retry
                status, body, last_error = 0, b"", type(exc).__name__
            if len(body) > MAX_RESPONSE_BYTES:
                raise DataProviderError("provider response exceeded the size limit")
            if 300 <= status < 400:
                raise DataProviderError(f"provider redirected (HTTP {status}); refusing to follow")
            if status == 200:
                try:
                    return json.loads(body)
                except ValueError as exc:
                    raise DataProviderError("provider returned invalid JSON") from exc
            if status in (401, 403):
                raise DataProviderError(f"provider refused credentials (HTTP {status})")
            if status == 404:
                raise DataProviderError("symbol or endpoint not found (HTTP 404)")
            if status and status not in (429, 500, 502, 503, 504):
                raise DataProviderError(f"provider error HTTP {status}")
            last_error = last_error or f"HTTP {status}"
            wait = delay
            if status == 429:
                retry_after = _retry_after_seconds(resp_headers)
                if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
                    raise DataProviderError(f"rate limited; provider asked to wait {retry_after:.0f}s")
                wait = max(wait, retry_after or 0.0)
            if attempt < self.max_attempts:
                self._sleep(wait)
                delay *= 2
        raise DataProviderError(f"provider unreachable after {self.max_attempts} attempts "
                                f"({last_error})")


def _retry_after_seconds(headers: dict) -> float | None:
    for k, v in headers.items():
        if str(k).lower() == "retry-after":
            try:
                return max(float(v), 0.0)
            except (TypeError, ValueError):
                return None
    return None


def parse_row(r: dict[str, Any]) -> Bar:
    """One Tiingo daily row -> Bar. The 'date' field is the session date at 00:00 UTC."""
    try:
        day = date.fromisoformat(str(r["date"])[:10])
        return Bar(day, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                   float(r.get("volume") or 0.0), float(r.get("divCash") or 0.0),
                   float(r.get("splitFactor") or 1.0))
    except (KeyError, TypeError, ValueError) as exc:
        raise DataProviderError(f"malformed row: {exc}") from exc


class TiingoBarSource:
    """BarSource for the live engine: recent history ending at `through` (one call/symbol)."""

    LOOKBACK_DAYS = 500

    def __init__(self, client: TiingoClient) -> None:
        self._client = client

    def history(self, symbols: Sequence[str], through: date) -> dict[str, BarSeries]:
        from datetime import timedelta

        series, _ = self._client.history(symbols, through - timedelta(days=self.LOOKBACK_DAYS),
                                         through)
        return series
