"""One market-data feed shared by every hype variant. No I/O of its own: it only decides
which client to ask, and how often.

Why it exists:
  * Open positions are priced from one list of sources (DexScreener, then GeckoTerminal) and
    new coins are found through another (GeckoTerminal, then DexScreener), so a rate-limited
    scan feed can no longer stop the stops from being checked.
  * Every variant looks at the same market, so one scan result is shared for a minute and a
    coin held by several variants is priced once per cycle.
  * A provider that answers 429 ("slow down") has its scan endpoints left alone for five
    minutes instead of being asked again by every variant every cycle.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from tradinglab.hype.model import Snapshot

DISCOVER_TTL = 60.0  # one scan result serves every variant for a minute
RATE_LIMIT_PAUSE = 300.0  # after a 429, don't scan through that provider for five minutes
PRICE_TTL = 8.0  # a coin's price is asked for at most once per cycle, however many variants hold it
STATUS_WINDOW = 3600.0  # status() counts rate-limit replies over the last hour


def _sources(given: Any) -> tuple[Any, ...]:
    """One source, or several to try in order."""
    return tuple(given) if isinstance(given, (list, tuple)) else (given,)


class SharedFeed:
    """A Source for HypeBot (discover + snapshots) that all variants share.

    `discover` and `price` are each one source or a list of sources tried in order. `clock`
    returns epoch seconds; tests pass a fake one.
    """

    def __init__(self, discover: Any, price: Any, clock: Callable[[], float] = time.time) -> None:
        self._discover, self._price, self._clock = _sources(discover), _sources(price), clock
        self._found: list[Snapshot] = []  # the last good scan...
        self._found_at: float | None = None  # ...and when it was made
        self._prices: dict[str, tuple[float, Snapshot | None]] = {}  # token -> (asked at, answer)
        self._quiet: dict[int, tuple[float, str]] = {}  # source -> (don't scan before, the error)
        self._limited: deque[float] = deque()  # when each rate-limit reply arrived
        self._errors = {"price": "", "discover": ""}
        self._last_ok: float | None = None
        self._lock = threading.Lock()  # status() is read from the web thread

    # -- finding new coins ----------------------------------------------------------------

    def discover(self) -> list[Snapshot]:
        now = self._clock()
        if self._found_at is not None and now - self._found_at < DISCOVER_TTL:
            return list(self._found)
        errors: list[str] = []
        for src in self._discover:
            until, why = self._quiet.get(id(src), (now, ""))
            if now < until:
                errors.append(f"{why} (not asked again yet)")
                continue
            try:
                found = src.discover()
                if not found:
                    raise RuntimeError("nothing returned")
            except Exception as exc:  # noqa: BLE001 - fall through to the next source
                errors.append(self._failed(src, exc, now, DISCOVER_TTL))
                continue
            self._found, self._found_at = list(found), now
            self._record("discover", errors, now)
            return list(found)
        self._record("discover", errors, None)
        if self._found_at is not None and now - self._found_at < RATE_LIMIT_PAUSE:
            return list(self._found)  # nobody answered: the last good scan beats nothing
        raise RuntimeError("; ".join(errors) or "no data sources")

    # -- pricing open positions -----------------------------------------------------------

    def snapshots(self, tokens: Iterable[str], pairs: dict[str, str] | None = None) -> dict[str, Snapshot]:
        """Latest snapshot per token. Tokens no source has data for are left out."""
        now = self._clock()
        tokens = list(dict.fromkeys(tokens))
        self._prices = {t: v for t, v in self._prices.items() if now - v[0] < PRICE_TTL}
        need = [t for t in tokens if t not in self._prices]
        if need:
            got = self._ask_prices(need, pairs, now)
            self._prices.update({t: (now, got.get(t)) for t in need})  # "no data" is remembered too
        return {t: s for t in tokens if (s := self._prices[t][1]) is not None}

    def _ask_prices(self, need: list[str], pairs: dict[str, str] | None, now: float) -> dict[str, Snapshot]:
        """Ask each price source in turn for the coins the ones before it could not price."""
        got: dict[str, Snapshot] = {}
        errors: list[str] = []
        answered = False
        for src in self._price:
            left = [t for t in need if t not in got]
            if not left:
                break
            try:
                snaps = src.snapshots(left, pairs)
            except Exception as exc:  # noqa: BLE001 - fall through to the next source
                errors.append(self._failed(src, exc, now, 0.0))  # prices are always asked for again
                continue
            answered = True
            # A snapshot with no pool depth can't be sold against and would read as a pulled
            # pool, so it doesn't count as an answer: the next source gets asked instead.
            got.update({t: s for t, s in snaps.items() if t in left and s.liquidity > 0})
        self._record("price", errors, now if answered else None)
        if not answered:
            raise RuntimeError("; ".join(errors) or "no data sources")
        return got

    # -- bookkeeping ----------------------------------------------------------------------

    def _failed(self, src: Any, exc: Exception, now: float, pause: float) -> str:
        """Note a failed call; a rate-limit reply also keeps scans away from that source."""
        text = f"{type(src).__name__}: {exc}"
        limited = "429" in str(exc)
        if limited:
            with self._lock:
                self._limited.append(now)
                self._forget_old(now)
        until = now + (RATE_LIMIT_PAUSE if limited else pause)
        if until > self._quiet.get(id(src), (now, ""))[0]:
            self._quiet[id(src)] = (until, text)
        return text

    def _record(self, kind: str, errors: list[str], ok_at: float | None) -> None:
        with self._lock:
            self._errors[kind] = "; ".join(errors)[:300]
            if ok_at is not None:
                self._last_ok = ok_at

    def _forget_old(self, now: float) -> None:
        while self._limited and now - self._limited[0] >= STATUS_WINDOW:
            self._limited.popleft()

    def status(self) -> dict[str, Any]:
        """For the dashboard: rate-limit replies in the last hour, last good answer, last error."""
        now = self._clock()
        with self._lock:
            self._forget_old(now)
            last_ok = "" if self._last_ok is None else \
                datetime.fromtimestamp(self._last_ok, UTC).isoformat(timespec="seconds")
            return {"rate_limited_1h": len(self._limited), "last_ok": last_ok,
                    "last_error": "; ".join(e for e in self._errors.values() if e)[:300]}
