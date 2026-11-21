"""Whale flow: what the big wallets did in a coin's pool over the last few minutes.

"Whale" here just means a single trade above a dollar size (rules.whale_min_usd). The data is
GeckoTerminal's public trade list for a pool (the last 300 trades of the past day above a
size, each with the wallet that made it, the side, the dollars and the time). It is read-only
market data: no wallet is ever connected and nothing can be signed.

What it can and cannot tell you: big buys from several different wallets, with little selling
against them, is the pattern that tends to come before a run. It is also exactly what a group
dumping on latecomers looks like a few minutes earlier, so it is one more filter, not a
crystal ball. The paper accounts are how we find out whether it helps.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class Flow:
    buyers: int  # different wallets that made a big buy in the window
    buy_usd: float
    sellers: int  # different wallets that made a big sell
    sell_usd: float

    def buying(self, min_buyers: int, ratio: float) -> bool:
        """Several whales buying, and clearly more bought than sold."""
        return self.buyers >= min_buyers and self.buy_usd >= ratio * self.sell_usd

    def selling(self, ratio: float) -> bool:
        """More than one whale selling, and clearly more sold than bought."""
        return self.sellers >= 2 and self.sell_usd >= ratio * max(self.buy_usd, 1.0)

    def words(self) -> str:
        return (f"{self.buyers} big wallet{'s' if self.buyers != 1 else ''} bought "
                f"${self.buy_usd:,.0f}, {self.sellers} sold ${self.sell_usd:,.0f}")


def big_size(liquidity: float, floor: float = 250.0, share: float = 0.01, cap: float = 2000.0) -> float:
    """How many dollars make a trade "big" in this pool: 1% of the pool, never under $250 or
    over $2,000, to the nearest $50. A $1,000 buy is huge in a $20k pool and ordinary in a
    $2m one, so one fixed number would see no whales in small coins and nothing but whales
    in large ones."""
    return float(round(min(max(liquidity * share, floor), cap) / 50.0) * 50)


def _usd(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x and 0 < x < 1e12 else 0.0


def flow(trades: Iterable[Mapping[str, Any]], now: datetime, window_min: float, min_usd: float) -> Flow:
    """Summarise trade rows (GeckoTerminal `attributes`) from the last `window_min` minutes.
    Rows that are too small, too old, flagged anomalous, or unreadable are ignored."""
    since = now - timedelta(minutes=window_min)
    buys: dict[str, float] = {}
    sells: dict[str, float] = {}
    for t in trades:
        if not isinstance(t, Mapping) or t.get("is_anomalous"):
            continue
        usd = _usd(t.get("volume_in_usd"))
        wallet = str(t.get("tx_from_address") or "")
        try:
            at = datetime.fromisoformat(str(t.get("block_timestamp")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if usd < min_usd or not wallet or at.tzinfo is None or not since <= at <= now:
            continue
        side = buys if t.get("kind") == "buy" else sells if t.get("kind") == "sell" else None
        if side is not None:
            side[wallet] = side.get(wallet, 0.0) + usd
    return Flow(len(buys), sum(buys.values()), len(sells), sum(sells.values()))
