"""GeckoTerminal client: read-only Solana pool data (stdlib HTTP only). Primary hype source.

Provider facts (public API v2, no key, docs at apiguide.geckoterminal.com):
  * GET /networks/solana/trending_pools?include=base_token&page=N and /new_pools: 20 pools per
    page with base_token_price_usd, reserve_in_usd (liquidity), fdv_usd, market_cap_usd,
    pool_created_at, price_change_percentage.{m5,h1}, transactions.{m5,h1}.{buys,sells},
    volume_usd.{m5,h1}; the base token's address/symbol/name come in `included`.
  * GET /networks/solana/pools/multi/{addr,addr,...}?include=base_token: up to 30 pools.
  * Free tier: ~30 calls/minute. Accept header pins the API version.

Read-only: no accounts, keys, wallets or order entry exist on this API.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from tradinglab.hype.dexscreener import MarketDataError, _f, _urllib_get
from tradinglab.hype.model import Snapshot
from tradinglab.marketdata.tiingo import RateLimiter
from tradinglab.safety.endpoints import POOL_DATA_URL
from tradinglab.safety.network_guard import install_network_guard, policy

log = logging.getLogger(__name__)

NETWORK = "solana"
BATCH = 30
LIMITER = RateLimiter(max_calls=25, per_seconds=60.0)
ACCEPT = "application/json;version=20230302"


def _tokens(payload: dict) -> dict[str, dict]:
    out = {}
    for inc in payload.get("included") or []:
        if isinstance(inc, dict) and inc.get("type") == "token":
            out[str(inc.get("id"))] = inc.get("attributes") or {}
    return out


def parse_pool(pool: dict, tokens: dict[str, dict], now_ms: int) -> Snapshot | None:
    if not isinstance(pool, dict):
        return None
    a = pool.get("attributes") or {}
    rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    tok = tokens.get(str(rel.get("id")), {})
    mint = tok.get("address") or str(rel.get("id", "")).removeprefix(f"{NETWORK}_")
    price = _f(a.get("base_token_price_usd"))
    pair = a.get("address")
    if not mint or not pair or price is None or price <= 0:
        return None
    tx = a.get("transactions") or {}
    m5, h1 = tx.get("m5") or {}, tx.get("h1") or {}
    vol, chg = a.get("volume_usd") or {}, a.get("price_change_percentage") or {}
    age = None
    created = a.get("pool_created_at")
    if isinstance(created, str):
        try:
            age = (now_ms / 1000 - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) / 60
        except ValueError:
            age = None
    dex = (((pool.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id", "")
    return Snapshot(
        token=str(mint), symbol=str(tok.get("symbol") or (a.get("name") or "?").split(" /")[0])[:20],
        name=str(tok.get("name") or "")[:60], pair=str(pair), dex=str(dex),
        url=f"https://www.geckoterminal.com/{NETWORK}/pools/{pair}", price=price,
        liquidity=_f(a.get("reserve_in_usd")) or 0.0,
        market_cap=_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")) or 0.0, age_min=age,
        buys_5m=int(_f(m5.get("buys")) or 0), sells_5m=int(_f(m5.get("sells")) or 0),
        buys_1h=int(_f(h1.get("buys")) or 0), sells_1h=int(_f(h1.get("sells")) or 0),
        vol_5m=_f(vol.get("m5")) or 0.0, vol_1h=_f(vol.get("h1")) or 0.0,
        chg_5m=_f(chg.get("m5")) or 0.0, chg_1h=_f(chg.get("h1")) or 0.0,
        boosts=0, socials=0, promo_known=False)


class GeckoTerminalClient:
    def __init__(self, *, http_get=_urllib_get, timeout: float = 15.0, max_attempts: int = 3,
                 sleep: Callable[[float], None] = time.sleep,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        install_network_guard()
        self._http_get, self.timeout, self.max_attempts = http_get, timeout, max_attempts
        self._sleep, self._clock_ms = sleep, clock_ms

    def _get_json(self, path: str) -> Any:
        url = f"{POOL_DATA_URL}/api/v2{path}"
        policy().check_url(url)
        last = ""
        for attempt in range(1, self.max_attempts + 1):
            LIMITER.acquire()
            try:
                result = self._http_get(url, {"Accept": ACCEPT, "User-Agent": "trading-lab-paper-bot"},
                                        self.timeout)
            except (OSError, TimeoutError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                status, body = result[0], result[1]
                if 300 <= status < 400:
                    raise MarketDataError(f"GeckoTerminal redirected (HTTP {status}); refusing to follow")
                if status == 200:
                    try:
                        return json.loads(body)
                    except ValueError as exc:
                        raise MarketDataError("GeckoTerminal returned malformed JSON") from exc
                last = f"HTTP {status}"
                if status not in (429, 500, 502, 503, 504):
                    break
            if attempt < self.max_attempts:
                self._sleep(min(3 * attempt, 10))
        raise MarketDataError(f"GeckoTerminal unavailable: {last}")

    def _pools(self, path: str) -> list[Snapshot]:
        payload = self._get_json(path)
        if not isinstance(payload, dict):
            return []
        toks, now = _tokens(payload), self._clock_ms()
        return [s for p in payload.get("data") or [] if (s := parse_pool(p, toks, now)) is not None]

    def discover(self) -> list[Snapshot]:
        """Trending (2 pages) + newest pools on Solana, one snapshot per token (most liquid pool)."""
        best: dict[str, Snapshot] = {}
        for path in (f"/networks/{NETWORK}/trending_pools?include=base_token&page=1",
                     f"/networks/{NETWORK}/trending_pools?include=base_token&page=2",
                     f"/networks/{NETWORK}/new_pools?include=base_token&page=1"):
            try:
                pools = self._pools(path)
            except MarketDataError as exc:
                log.warning("hype: %s unavailable: %s", path, exc)
                continue
            for s in pools:
                if s.token not in best or s.liquidity > best[s.token].liquidity:
                    best[s.token] = s
        return list(best.values())

    def snapshots(self, tokens: Iterable[str], pairs: dict[str, str] | None = None) -> dict[str, Snapshot]:
        """Re-price held tokens by their pool address (`pairs` maps token -> pool)."""
        pairs = pairs or {}
        wanted = {pairs[t]: t for t in tokens if pairs.get(t, "").isalnum()}
        out: dict[str, Snapshot] = {}
        addrs = list(wanted)
        for i in range(0, len(addrs), BATCH):
            for s in self._pools(f"/networks/{NETWORK}/pools/multi/{','.join(addrs[i:i + BATCH])}"
                                 "?include=base_token"):
                if s.pair in wanted:
                    out[wanted[s.pair]] = s
        return out
