"""DexScreener client: read-only Solana memecoin market data (stdlib HTTP only).

Provider facts this module is built around (public API, no key, documented at
docs.dexscreener.com/api/reference):
  * GET /token-boosts/latest/v1, /token-boosts/top/v1, /token-profiles/latest/v1:
    lists of {chainId, tokenAddress, url, ...} for tokens being promoted right now.
    Limit 60 requests/minute.
  * GET /tokens/v1/{chainId}/{addr,addr,...} (up to 30 addresses): one entry per trading
    pair with priceUsd, txns.{m5,h1,h6,h24}.{buys,sells}, volume.*, priceChange.*,
    liquidity.usd, marketCap, fdv, pairCreatedAt (ms), info.socials/websites, boosts.active.
    Limit 300 requests/minute.

Nothing here can trade: the API has no accounts, keys, wallets or order entry.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from tradinglab.hype.model import Snapshot
from tradinglab.marketdata.tiingo import RateLimiter
from tradinglab.safety.endpoints import CRYPTO_DATA_URL
from tradinglab.safety.network_guard import install_network_guard, policy

log = logging.getLogger(__name__)

CHAIN = "solana"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
BATCH = 30  # addresses per /tokens request

# Kept well under the published limits (60/min lists, 300/min pairs).
LIST_LIMITER = RateLimiter(max_calls=40, per_seconds=60.0)
PAIR_LIMITER = RateLimiter(max_calls=200, per_seconds=60.0)

HttpGet = Callable[[str, dict[str, str], float], tuple]


class MarketDataError(RuntimeError):
    """DexScreener returned an error or malformed data, or was unreachable after retries."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _urllib_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes, dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_RESPONSE_BYTES + 1), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read(MAX_RESPONSE_BYTES + 1) if exc.fp else b""), dict(exc.headers or {})


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None  # drop NaN/inf


def parse_pair(pair: dict, now_ms: int) -> Snapshot | None:
    """One DexScreener pair -> Snapshot, or None if it lacks a usable price."""
    if not isinstance(pair, dict) or pair.get("chainId") != CHAIN:
        return None
    base = pair.get("baseToken") or {}
    price = _f(pair.get("priceUsd"))
    addr = base.get("address")
    if not addr or price is None or price <= 0:
        return None
    tx = (pair.get("txns") or {}).get("m5") or {}
    tx_h1 = (pair.get("txns") or {}).get("h1") or {}
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    info = pair.get("info") or {}
    created = pair.get("pairCreatedAt")
    age_min = (now_ms - int(created)) / 60000 if isinstance(created, (int, float)) and created > 0 else None
    return Snapshot(
        token=str(addr), symbol=str(base.get("symbol") or "?")[:20], name=str(base.get("name") or "")[:60],
        pair=str(pair.get("pairAddress") or ""), dex=str(pair.get("dexId") or ""),
        url=str(pair.get("url") or ""), price=price,
        liquidity=_f((pair.get("liquidity") or {}).get("usd")) or 0.0,
        market_cap=_f(pair.get("marketCap")) or _f(pair.get("fdv")) or 0.0,
        age_min=age_min,
        buys_5m=int(_f(tx.get("buys")) or 0), sells_5m=int(_f(tx.get("sells")) or 0),
        buys_1h=int(_f(tx_h1.get("buys")) or 0), sells_1h=int(_f(tx_h1.get("sells")) or 0),
        vol_5m=_f(vol.get("m5")) or 0.0, vol_1h=_f(vol.get("h1")) or 0.0,
        chg_5m=_f(chg.get("m5")) or 0.0, chg_1h=_f(chg.get("h1")) or 0.0,
        boosts=int(_f((pair.get("boosts") or {}).get("active")) or 0),
        socials=len(info.get("socials") or []) + len(info.get("websites") or []),
    )


class DexScreenerClient:
    def __init__(self, *, http_get: HttpGet = _urllib_get, timeout: float = 15.0,
                 max_attempts: int = 3, sleep: Callable[[float], None] = time.sleep,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        install_network_guard()
        self._http_get, self.timeout, self.max_attempts = http_get, timeout, max_attempts
        self._sleep, self._clock_ms = sleep, clock_ms

    def _get_json(self, path: str, limiter: RateLimiter) -> Any:
        url = f"{CRYPTO_DATA_URL}{path}"
        policy().check_url(url)
        last = ""
        for attempt in range(1, self.max_attempts + 1):
            limiter.acquire()
            try:
                headers = {"Accept": "application/json", "User-Agent": "trading-lab-paper-bot"}
                result = self._http_get(url, headers, self.timeout)
            except (OSError, TimeoutError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                status, body = result[0], result[1]
                if 300 <= status < 400:
                    raise MarketDataError(f"DexScreener redirected (HTTP {status}); refusing to follow")
                if status == 200:
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise MarketDataError("DexScreener response too large")
                    try:
                        return json.loads(body)
                    except ValueError as exc:
                        raise MarketDataError("DexScreener returned malformed JSON") from exc
                last = f"HTTP {status}"
                if status not in (429, 500, 502, 503, 504):
                    break
            if attempt < self.max_attempts:
                self._sleep(min(2 ** attempt, 10))
        raise MarketDataError(f"DexScreener unavailable: {last}")

    def trending_tokens(self) -> list[str]:
        """Solana tokens being boosted or freshly profiled right now (newest first, unique)."""
        seen: dict[str, None] = {}
        for path in ("/token-boosts/latest/v1", "/token-boosts/top/v1", "/token-profiles/latest/v1"):
            try:
                rows = self._get_json(path, LIST_LIMITER)
            except MarketDataError as exc:
                log.warning("hype: %s unavailable: %s", path, exc)
                continue
            for r in rows if isinstance(rows, list) else []:
                if not isinstance(r, dict) or r.get("chainId") != CHAIN:
                    continue
                if isinstance(r.get("tokenAddress"), str):
                    seen.setdefault(r["tokenAddress"], None)
        return list(seen)

    def snapshots(self, tokens: Iterable[str]) -> dict[str, Snapshot]:
        """Best (most liquid) pair per token. Tokens with no live pair are left out."""
        tokens = [t for t in dict.fromkeys(tokens) if t and t.isalnum() and len(t) <= 64]
        out: dict[str, Snapshot] = {}
        now = self._clock_ms()
        for i in range(0, len(tokens), BATCH):
            chunk: Sequence[str] = tokens[i:i + BATCH]
            rows = self._get_json(f"/tokens/v1/{CHAIN}/{','.join(chunk)}", PAIR_LIMITER)
            for pair in rows if isinstance(rows, list) else []:
                snap = parse_pair(pair, now)
                if snap is None or snap.token not in chunk:
                    continue
                if snap.token not in out or snap.liquidity > out[snap.token].liquidity:
                    out[snap.token] = snap
        return out
