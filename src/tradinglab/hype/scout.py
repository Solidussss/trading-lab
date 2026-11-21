"""The scout: an early-warning list for Solana memecoins. It never trades and holds no money.

Once a minute it looks at two public lists (freshly profiled or boosted coins on DexScreener,
which is where pump fun coins show up before they migrate, and GeckoTerminal's trending and
new pools) and raises an alert when it sees one of these:

  early      a coin still on pump fun's curve that is drawing a crowd
  migrated   a pump fun coin that moved to a normal pool minutes ago and is still being bought
  theme      several new coins about the same thing (a subject is trending, not just one coin)
  whales     several big wallets buying a coin, with little big selling against them
  smart      a wallet with a good record here bought something

Wallet records are built by the scout itself: every big buy it sees is remembered, and an hour
later it checks whether the coin went up. A wallet earns "good record" only after five such
buys with at least three in five up by 20% or more.

Every alert keeps the price it was raised at and the best and latest price since, so the page
can show how the alerts actually did. That record is the point: it is how to tell whether an
alert type is worth acting on.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from tradinglab.hype.model import Snapshot, score
from tradinglab.hype.whales import big_size, flow

log = logging.getLogger(__name__)

DOC = "scout"
MAX_ALERTS = 150
MAX_PENDING = 300
MAX_WALLETS = 500
REPEAT_AFTER = timedelta(hours=6)  # the same alert about the same coin is not raised again sooner
TRACK_FOR = timedelta(hours=24)  # how long an alert's price is followed
JUDGE_AFTER = timedelta(minutes=60)  # when a remembered whale buy is scored
WIN_GAIN = 0.20  # ...as a win if the coin is up this much by then
SMART_MIN_BUYS, SMART_MIN_RATE = 5, 0.6
WHALE_LOOKUPS = 1  # coins per minute whose big trades are looked up: the data is tightly
# rate-limited (it answered "slow down" after a handful of calls in testing)
WHALE_WINDOW_MIN, WHALE_BUYERS, WHALE_RATIO = 10.0, 3, 2.0

# early: still on the curve, old enough to judge, busy, more buying than selling
EARLY_AGE_MIN, EARLY_AGE_MAX = 5.0, 180.0
EARLY_TXNS, EARLY_BUY_RATIO, EARLY_CURVE = 60, 0.55, 0.15
# migrated: moved to a normal pool in the last 20 minutes and still being bought
MIGRATED_AGE_MAX, MIGRATED_TXNS, MIGRATED_BUY_RATIO = 20.0, 100, 0.55
THEME_COINS, THEME_AGE_HOURS = 3, 24.0

# words that say nothing about a subject
_BORING = frozenset("""the and for coin token sol solana pump fun inu dog cat pepe meme official real
new baby mini super mega just not you are was with from this that its your one two usd usdc
moon elon ai swap cash money gem king queen lord sir mr mrs of on in to is it""".split())


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text)


def words(s: Snapshot) -> set[str]:
    """The subject words in a coin's name and symbol (lower case, 3+ letters, not filler)."""
    found = re.findall(r"[a-z]{3,}", f"{s.symbol} {s.name}".lower())
    return {w for w in found if w not in _BORING}


def themes(coins: Iterable[Snapshot]) -> dict[str, list[Snapshot]]:
    """word -> the young coins that carry it, for words shared by enough different coins."""
    by_word: dict[str, dict[str, Snapshot]] = {}
    for s in coins:
        if s.age_min is None or s.age_min > THEME_AGE_HOURS * 60:
            continue
        for w in words(s):
            by_word.setdefault(w, {})[s.token] = s
    # Copies of one coin share every word of its name: keep one word per group of coins (the
    # one most of them use as their symbol, then the longest), so "last penny" is one subject.
    best: dict[frozenset, str] = {}
    for w, c in by_word.items():
        if len(c) < THEME_COINS:
            continue
        group = frozenset(c)

        def rank(word: str, coins: dict = c) -> tuple:
            return (sum(word == s.symbol.lower() for s in coins.values()), len(word), word)

        if group not in best or rank(w) > rank(best[group]):
            best[group] = w
    return {w: sorted(by_word[w].values(), key=lambda x: -(x.buys_5m + x.sells_5m))
            for w in best.values()}


def is_early(s: Snapshot) -> bool:
    done = s.curve_done
    return (s.pre_migration and s.age_min is not None and EARLY_AGE_MIN <= s.age_min <= EARLY_AGE_MAX
            and s.buys_5m + s.sells_5m >= EARLY_TXNS and s.buy_ratio >= EARLY_BUY_RATIO
            and done is not None and done >= EARLY_CURVE)


def is_migrated(s: Snapshot) -> bool:
    return (not s.pre_migration and s.token.endswith("pump") and s.age_min is not None
            and s.age_min <= MIGRATED_AGE_MAX and s.buys_5m + s.sells_5m >= MIGRATED_TXNS
            and s.buy_ratio >= MIGRATED_BUY_RATIO)


def links(s: Snapshot) -> dict[str, str]:
    """Where to look the coin up: a chart site and a block explorer, never a trading site (the
    safety layer keeps trading sites out of this code entirely, even as links)."""
    out = {"DexScreener": s.url} if s.url.startswith("https://") else {}
    if s.token.isalnum():
        if "DexScreener" not in out and s.pair.isalnum():
            out["DexScreener"] = f"https://dexscreener.com/solana/{s.pair}"
        out["Solscan"] = f"https://solscan.io/token/{s.token}"
    return out


class Scout:
    """`feed` needs early(), discover(), snapshots() and big_trades(); `store` needs
    load_doc() and save_doc(). One step a minute; never raises."""

    def __init__(self, feed: Any, store: Any,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.feed, self.store, self.clock = feed, store, clock

    # -- state ----------------------------------------------------------------------------

    def load(self) -> dict:
        doc = self.store.load_doc(DOC) or {}
        for key, empty in (("alerts", []), ("seen", {}), ("pending", []), ("wallets", {})):
            doc.setdefault(key, empty)
        return doc

    def step(self) -> dict:
        now = self.clock()
        doc = self.load()
        try:
            self._look(doc, now)
            doc["error"] = ""
        except Exception as exc:  # noqa: BLE001 - a bad minute must not stop the next one
            log.warning("scout: %s: %s", type(exc).__name__, exc)
            doc["error"] = f"{type(exc).__name__}: {exc}"[:300]
        doc["updated"] = _iso(now)
        doc["steps"] = int(doc.get("steps", 0)) + 1
        self.store.save_doc(DOC, doc)
        return doc

    # -- one look at the market -----------------------------------------------------------

    def _look(self, doc: dict, now: datetime) -> None:
        early = self._safely(self.feed.early)
        hot = self._safely(self.feed.discover)
        coins: dict[str, Snapshot] = {}
        for s in [*hot, *early]:  # the early list knows about the curve, so it wins a tie
            coins[s.token] = s
        doc["seen"] = {k: v for k, v in doc["seen"].items() if _when(v) > now}
        smart = self.smart_wallets(doc)
        raised: list[Snapshot] = []
        for s in coins.values():
            if is_early(s):
                done = s.curve_done or 0.0
                text = (f"{s.age_min:.0f} min old on pump fun, {done:.0%} of the way to migrating; "
                        f"{s.buys_5m} buys vs {s.sells_5m} sells in 5 min")
                if self._raise(doc, now, "early", s, text, bonus=10 * done):
                    raised.append(s)
            elif is_migrated(s):
                text = (f"migrated {s.age_min:.0f} min ago; {s.buys_5m} buys vs {s.sells_5m} sells "
                        f"in 5 min, ${s.liquidity:,.0f} pool")
                if self._raise(doc, now, "migrated", s, text):
                    raised.append(s)
        for word, group in themes(coins.values()).items():
            lead = group[0]
            counts: dict[str, int] = {}
            for g in group:
                counts[g.symbol] = counts.get(g.symbol, 0) + 1
            names = ", ".join(sym + (f" ×{n}" if n > 1 else "") for sym, n in list(counts.items())[:6])
            self._raise(doc, now, "theme", lead, f"{len(group)} new coins about '{word}': {names}",
                        key=f"theme:{word}", title=word.upper(), bonus=5.0 * min(len(group), 4))
        # whale flow for the busiest few: this minute's alerts first, then the busiest others
        rest = sorted((s for s in coins.values() if s not in raised and s.pair),
                      key=lambda s: -(s.buys_5m + s.sells_5m))
        for s in [*raised, *rest][:WHALE_LOOKUPS]:
            self._whales(doc, now, s, smart)
        self._judge(doc, now)
        if doc.get("steps", 0) % 5 == 0:
            self._follow(doc, now)

    @staticmethod
    def _safely(call: Callable[[], list]) -> list:
        try:
            return list(call())
        except Exception as exc:  # noqa: BLE001 - one list being down leaves the other
            log.warning("scout: a list is unavailable: %s", exc)
            return []

    def _raise(self, doc: dict, now: datetime, kind: str, s: Snapshot, text: str, *,
               key: str = "", title: str = "", bonus: float = 0.0) -> bool:
        key = key or f"{kind}:{s.token}"
        if key in doc["seen"]:
            return False
        doc["seen"][key] = _iso(now + REPEAT_AFTER)
        grade = round(min(score(s) * 0.8 + bonus + {"smart": 25, "whales": 20}.get(kind, 0), 100.0), 1)
        doc["alerts"] = ([{
            "at": _iso(now), "kind": kind, "token": s.token, "pair": s.pair, "symbol": s.symbol,
            "title": title or s.symbol, "text": text, "score": grade, "price": s.price,
            "last": s.price, "peak": s.price, "dex": s.dex, "mcap": s.market_cap,
            "links": links(s)}] + doc["alerts"])[:MAX_ALERTS]
        log.info("scout alert [%s] %s: %s", kind, title or s.symbol, text)
        return True

    # -- whales and wallet records --------------------------------------------------------

    def _whales(self, doc: dict, now: datetime, s: Snapshot, smart: set[str]) -> None:
        size = big_size(s.liquidity)  # big for this pool: 1% of it, between $250 and $2,000
        rows = self.feed.big_trades(s.pair, size)
        if not rows:
            return
        f = flow(rows, now, WHALE_WINDOW_MIN, size)
        if f.buying(WHALE_BUYERS, WHALE_RATIO):
            self._raise(doc, now, "whales", s, f"{f.words()} in {WHALE_WINDOW_MIN:.0f} min")
        since = now - timedelta(minutes=WHALE_WINDOW_MIN)
        known = {(p["wallet"], p["token"]) for p in doc["pending"]}
        for t in rows:
            wallet = str(t.get("tx_from_address") or "")
            try:
                at = _when(str(t.get("block_timestamp")).replace("Z", "+00:00"))
                usd = float(t.get("volume_in_usd") or 0)
            except (TypeError, ValueError):
                continue
            if t.get("kind") != "buy" or usd < size or not wallet or at < since or at > now:
                continue
            if wallet in smart:
                rec = doc["wallets"][wallet]
                self._raise(doc, now, "smart", s,
                            f"a wallet that was right on {rec[1]} of its last {rec[0]} big buys here "
                            f"just bought ${usd:,.0f}", key=f"smart:{wallet}:{s.token}")
            if (wallet, s.token) not in known:
                known.add((wallet, s.token))
                doc["pending"].append({"wallet": wallet, "token": s.token, "symbol": s.symbol,
                                       "price": s.price, "at": _iso(now)})
        doc["pending"] = doc["pending"][-MAX_PENDING:]

    def _judge(self, doc: dict, now: datetime) -> None:
        """Score whale buys seen an hour ago: did the coin go up?"""
        due = [p for p in doc["pending"] if _when(p["at"]) + JUDGE_AFTER <= now]
        if not due:
            return
        prices = self.feed.snapshots(sorted({p["token"] for p in due}))
        for p in due:
            s = prices.get(p["token"])
            gain = (s.price / p["price"] - 1) if s is not None and p["price"] > 0 else -1.0  # gone = lost
            rec = doc["wallets"].setdefault(p["wallet"], [0, 0, 0.0, "", ""])
            rec[0] += 1
            rec[1] += gain >= WIN_GAIN
            rec[2] = round(rec[2] + max(min(gain, 10.0), -1.0), 4)
            rec[3], rec[4] = _iso(now), p.get("symbol", "")
        doc["pending"] = [p for p in doc["pending"] if p not in due]
        if len(doc["wallets"]) > MAX_WALLETS:  # keep the most recently seen
            keep = sorted(doc["wallets"].items(), key=lambda kv: kv[1][3], reverse=True)[:MAX_WALLETS]
            doc["wallets"] = dict(keep)

    @staticmethod
    def smart_wallets(doc: dict) -> set[str]:
        return {w for w, r in doc.get("wallets", {}).items()
                if r[0] >= SMART_MIN_BUYS and r[1] / r[0] >= SMART_MIN_RATE}

    # -- how the alerts did ---------------------------------------------------------------

    def _follow(self, doc: dict, now: datetime) -> None:
        """Update the latest and best price of every coin alerted in the last day."""
        live = [a for a in doc["alerts"] if _when(a["at"]) + TRACK_FOR > now and a["kind"] != "theme"]
        tokens = sorted({a["token"] for a in live})[:60]
        if not tokens:
            return
        prices = self.feed.snapshots(tokens)
        for a in live:
            s = prices.get(a["token"])
            if s is not None:
                a["last"] = s.price
                a["peak"] = max(a.get("peak", s.price), s.price)


def summary(doc: dict | None, now: datetime, hours: float = 24.0) -> dict:
    """For the page: how the last day's coin alerts did, by kind (count, share that were ever
    up 20%, average best gain, average gain now)."""
    out: dict[str, dict] = {}
    for a in (doc or {}).get("alerts", []):
        if a["kind"] == "theme" or _when(a["at"]) < now - timedelta(hours=hours) or a["price"] <= 0:
            continue
        k = out.setdefault(a["kind"], {"n": 0, "ran": 0, "peak": 0.0, "now": 0.0})
        best, last = a.get("peak", a["price"]) / a["price"] - 1, a.get("last", a["price"]) / a["price"] - 1
        k["n"] += 1
        k["ran"] += best >= WIN_GAIN
        k["peak"] += best
        k["now"] += last
    for k in out.values():
        k["peak"], k["now"] = k["peak"] / k["n"], k["now"] / k["n"]
    return out
