"""Following good traders: whose wallets are watched, and what happens when one of them buys.

Who is followed
  * KNOWN: traders whose wallets are public knowledge (the list and where it came from are
    in hype/known_traders.py). A name is only a label: what counts is the record below.
  * Found by the scout: any wallet that earned "proven" on the scout's own scoreboard (five
    scored big buys, three in five up 20% an hour later). Nobody has to have heard of it.

What is done with a buy
  * It goes on the page ("Traders we follow"), whoever made it.
  * It is scored an hour later like every other wallet's buys, so each followed trader gets a
    record measured here: of the coins they bought, how many were up 20% an hour after this
    program saw the buy. That is the number that matters to someone following them, and it is
    usually worse than the trader's own profit, because a fast trader has often sold by then.
  * It raises an alert only when it means something: the trader has a proven record here
    ("trader"), or two or more followed traders bought the same coin within half an hour
    ("crowd"). One unproven wallet buying is not an alert: the busiest of these wallets trade
    dozens of times a day.

How the buys are seen: `TraderFeed` is a background thread that asks a public Solana node, in
turn, for each followed wallet's newest transactions (hype/chain.py: read-only). The scout
collects what it found once a minute. Nothing here can trade.

What cannot be seen: some of the best-known wallets are mentioned in thousands of other
people's transactions a minute (bots that imitate them to bait copy-traders). A wallet's own
trades are a handful among those, and each candidate costs a request to a free, shared node to
tell apart. So a wallet with more new successful transactions than TX_PER_WALLET in one look is
"flooded": a few of the newest are read, the rest are let go, and it is looked at less often
until the flood stops. Quiet wallets, which is most of them, are followed trade for trade.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from tradinglab.hype.chain import ChainError, Move
from tradinglab.hype.known_traders import KNOWN

log = logging.getLogger(__name__)

FOUND_MAX = 12  # proven wallets found by the scout that are followed alongside the known ones
PASS_SECONDS = 45.0  # one look at every followed wallet is spread over about this long
MIN_GAP = 0.35  # a node is never asked faster than this on average, however short the list
TX_PER_WALLET = 6  # of a wallet's new successful transactions, at most this many are read per look
NARROW, WIDE = 100, 1000  # how far back one look reaches; WIDE once a wallet has overflowed NARROW
FLOOD_REST = 4  # a flooded wallet sits out this many passes before it is looked at again
# Why so gentle: the free nodes refuse an address that opens more than about 40 connections in
# ten seconds (seen in testing at 2.5 a second). About 50 wallets come to roughly one request a
# second at this pace, with the flooded ones costing a fifth of what they otherwise would.
BACK_OFF = 90.0  # after a node refuses or fails, leave them alone this long
QUEUE_MAX = 500
MIN_USD = 50.0  # smaller than this is a test buy or dust
STALE = timedelta(minutes=10)  # a buy first priced later than this is no longer news
CROWD_WINDOW = timedelta(minutes=30)
CROWD_MIN = 2
FEED_MAX = 60  # latest moves kept for the page
WAITING_MAX = 60
PRICED_PER_STEP = 60


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def short(wallet: str) -> str:
    return f"{wallet[:4]}…{wallet[-4:]}"


def followed(proven: Iterable[tuple[str, list]] = ()) -> dict[str, str]:
    """wallet -> name for everyone followed: the known traders, then up to FOUND_MAX wallets
    the scout proved itself (`proven`: (wallet, record) pairs, best first)."""
    out = {t.wallet: t.name for t in KNOWN}
    room = FOUND_MAX
    for wallet, _ in proven:
        if room <= 0:
            break
        if wallet not in out:
            out[wallet] = f"wallet {short(wallet)}"
            room -= 1
    return out


class TraderFeed:
    """Watches the followed wallets from a background thread. `chain` needs signatures(),
    move() and node(). The scout calls follow() with the current list and drain() for what
    was seen; everything else happens on the thread."""

    def __init__(self, chain: Any, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, pass_seconds: float = PASS_SECONDS) -> None:
        self._chain, self._clock, self._sleep, self._pass = chain, clock, sleep, pass_seconds
        self._lock = threading.Lock()
        self._wallets: list[str] = []
        self._last: dict[str, str] = {}  # wallet -> newest transaction already looked at
        self._busy: set[str] = set()  # wallets whose last look overflowed: look WIDE next time
        self._rest: dict[str, int] = {}  # flooded wallets -> passes still to sit out
        self._found: deque[Move] = deque(maxlen=QUEUE_MAX)
        self._ok: float | None = None
        self._error = ""
        self._reads = 0  # transactions read for the wallet just looked at
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- called by the scout ---------------------------------------------------------------

    def follow(self, wallets: Iterable[str]) -> None:
        with self._lock:
            self._wallets = list(dict.fromkeys(wallets))

    def drain(self) -> list[Move]:
        with self._lock:
            out = list(self._found)
            self._found.clear()
        return out

    def status(self) -> dict:
        with self._lock:
            return {"on": True, "wallets": len(self._wallets), "ok": self._ok, "error": self._error,
                    "node": self._chain.node(), "flooded": sorted(self._rest)}

    # -- the thread ------------------------------------------------------------------------

    def poll(self, wallet: str) -> list[Move]:
        """One wallet's buys and sells since the last look, oldest first. The first look only
        notes where the wallet's history stands: old trades are never replayed as news."""
        last = self._last.get(wallet)
        limit = 1 if last is None else WIDE if wallet in self._busy else NARROW
        rows = self._chain.signatures(wallet, until=last or "", limit=limit)
        good = [r["signature"] for r in rows if r.get("err") is None]
        flooded = last is not None and len(good) > TX_PER_WALLET
        with self._lock:
            if rows:
                self._last[wallet] = rows[0]["signature"]
            if len(rows) >= limit > 1:
                self._busy.add(wallet)  # more happened than one look holds: look wider next time
            elif len(rows) < NARROW // 2:
                self._busy.discard(wallet)
            if flooded:  # sample the newest, and come back less often
                self._rest[wallet] = FLOOD_REST
            else:
                self._rest.pop(wallet, None)
        if last is None:
            return []
        good = good[:TX_PER_WALLET]
        self._reads = len(good)
        moves = [self._chain.move(sig, wallet) for sig in reversed(good)]
        return [m for m in moves if m is not None]

    def run_once(self) -> bool:
        """One look at every followed wallet. False when a node failed (the caller waits)."""
        with self._lock:
            wallets = list(self._wallets)
            keep = set(wallets)  # a wallet no longer followed is forgotten
            self._last = {w: s for w, s in self._last.items() if w in keep}
            self._rest = {w: n for w, n in self._rest.items() if w in keep}
            self._busy &= keep
        if not wallets:
            return True
        gap = max(self._pass / len(wallets), MIN_GAP)
        for wallet in wallets:
            if self._stop.is_set():
                break
            self._reads = 0
            with self._lock:
                resting = self._rest.get(wallet, 0)
                if resting > 0:
                    self._rest[wallet] = resting - 1  # flooded lately: sits this pass out
            if resting > 0:
                continue
            try:
                moves = self.poll(wallet)
            except ChainError as exc:
                with self._lock:
                    self._error = str(exc)[:200]
                log.warning("trader watch: %s", exc)
                return False
            with self._lock:
                self._found.extend(moves)
                self._ok, self._error = self._clock(), ""
            self._sleep(max(gap, MIN_GAP * (1 + self._reads)))  # a busy wallet earns a longer pause
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ok = self.run_once()
            except Exception as exc:  # noqa: BLE001 - the watch must outlive one bad reply
                log.warning("trader watch: %s: %s", type(exc).__name__, exc)
                ok = False
            self._stop.wait(5.0 if ok else BACK_OFF)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="trader-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def _row(m: Move) -> dict:
    return {"wallet": m.wallet, "token": m.token, "side": m.side, "tokens": m.tokens, "sol": m.sol,
            "usd": m.usd, "at": _iso(m.at), "sig": m.sig}


def process(doc: dict, now: datetime, moves: Iterable[Move], names: Mapping[str, str],
            proven: set[str], prices: Callable[[list[str]], Mapping[str, Any]],
            raise_alert: Callable[..., bool], links: Callable[[Any], dict]) -> None:
    """Turn the trades just seen into the page's feed, scores-to-come and alerts.

    `doc` is the scout's document (its "pending" and "wallets" are shared with the whale
    scoreboard); `prices(tokens)` returns a snapshot per token it can price; `raise_alert` is
    the scout's own, so these alerts get the scam check and the once-per-coin rule too."""
    book = doc.setdefault("traders", {})
    for key, empty in (("feed", []), ("waiting", []), ("last", {})):
        book.setdefault(key, empty)
    rows = [r for r in book["waiting"] if datetime.fromisoformat(r["at"]) + STALE > now]
    rows += [_row(m) for m in moves if m.wallet in names and m.at + STALE > now and m.at <= now]
    rows.sort(key=lambda r: r["at"])
    tokens = list(dict.fromkeys(r["token"] for r in rows))[:PRICED_PER_STEP]
    try:
        found = prices(tokens) if tokens else {}
    except Exception as exc:  # noqa: BLE001 - no prices this minute: everything waits a minute
        log.warning("followed traders: prices unavailable (%s)", type(exc).__name__)
        found = {}
    waiting: list[dict] = []
    known = {(p["wallet"], p["token"]) for p in doc["pending"]}
    for r in rows:
        s = found.get(r["token"])
        if s is None or s.price <= 0:
            waiting.append(r)  # too new to have a price yet: asked again next minute
            continue
        usd = r["usd"] or (r["sol"] * s.sol_usd if s.sol_usd > 0 and r["sol"] > 0 else r["tokens"] * s.price)
        if usd < MIN_USD:
            continue
        wallet, name = r["wallet"], names[r["wallet"]]
        entry = {"at": r["at"], "wallet": wallet, "name": name, "side": r["side"], "token": r["token"],
                 "symbol": s.symbol, "usd": round(usd), "links": links(s)}
        book["feed"] = ([entry] + book["feed"])[:FEED_MAX]
        book["last"][wallet] = {"at": r["at"], "side": r["side"], "symbol": s.symbol}
        if r["side"] != "buy":
            continue
        if (wallet, r["token"]) not in known:  # scored an hour from now, like any whale buy
            known.add((wallet, r["token"]))
            doc["pending"].append({"wallet": wallet, "token": r["token"], "symbol": s.symbol,
                                   "price": s.price, "at": _iso(now)})
        if wallet in proven:
            rec = doc["wallets"][wallet]
            raise_alert(doc, now, "trader", s,
                        f"{name} bought about ${usd:,.0f}. Coins they bought were up 20% an hour "
                        f"later {rec[1]} times out of the last {rec[0]} scored here.",
                        key=f"trader:{wallet}:{r['token']}")
        since = _iso(now - CROWD_WINDOW)
        buyers = list(dict.fromkeys(e["name"] for e in book["feed"]
                                    if e["token"] == r["token"] and e["side"] == "buy" and e["at"] >= since))
        if len(buyers) >= CROWD_MIN:
            raise_alert(doc, now, "crowd", s,
                        f"{len(buyers)} traders we follow bought within {CROWD_WINDOW.seconds // 60} min: "
                        + ", ".join(buyers[:6]),
                        key=f"crowd:{r['token']}", bonus=min(5.0 * (len(buyers) - CROWD_MIN), 15.0))
    book["waiting"] = waiting[-WAITING_MAX:]
    book["last"] = {w: v for w, v in book["last"].items() if w in names}
