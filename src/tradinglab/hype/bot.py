"""The hype paper bot: every cycle it re-prices open positions, exits on its rules, scans
what's being hyped on Solana right now, and buys the strongest setups with pretend money.

It never holds a wallet, a key or an account anywhere: positions are rows in our database,
filled against DexScreener prices with the pessimistic cost model in model.py.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tradinglab.hype.model import (
    Account,
    Position,
    Rules,
    Snapshot,
    Trade,
    buy_fill,
    entry_check,
    exit_check,
    score,
    sell_fill,
)

log = logging.getLogger(__name__)

CYCLE_SECONDS = 30
CURVE_EVERY_MIN = 5  # one portfolio-value point per 5 minutes...
CURVE_POINTS = 2016  # ...kept for 7 days
TRAIL_POINTS = 60  # last 60 prices per open coin (~30 min)
SCAN_EVERY = 2  # look for new coins every 2nd cycle (~1 min); open positions every cycle
WATCH_SIZE = 12


MISSING_LIMIT = 3  # checks in a row with no data before a pool counts as pulled


class Source(Protocol):
    def discover(self) -> list[Snapshot]: ...
    def snapshots(self, tokens: list[str], pairs: dict[str, str] | None = None) -> dict[str, Snapshot]: ...


class FallbackSource:
    """Try each data source in turn; the first that answers wins."""

    def __init__(self, *sources: Any) -> None:
        self.sources = sources

    def _first(self, call: Callable[[Any], Any]) -> Any:
        errors = []
        for src in self.sources:
            try:
                return call(src)
            except Exception as exc:  # noqa: BLE001 - fall through to the next source
                errors.append(f"{type(src).__name__}: {exc}")
        raise RuntimeError("; ".join(errors) or "no data sources")

    def discover(self) -> list[Snapshot]:
        def call(src):
            found = src.discover()
            if not found:
                raise RuntimeError("nothing returned")
            return found
        return self._first(call)

    def snapshots(self, tokens, pairs=None):
        return self._first(lambda src: src.snapshots(tokens, pairs))


class HypeStore(Protocol):
    def load_hype(self) -> Account | None: ...
    def save_hype(self, account: Account, trades: list[Trade]) -> None: ...
    def hype_trades(self, n: int = 200) -> list[Trade]: ...


class MemoryHypeStore:
    def __init__(self) -> None:
        self.account: Account | None = None
        self.trades: list[Trade] = []

    def load_hype(self) -> Account | None:
        if self.account is None:
            return None
        return Account.from_json(json.loads(json.dumps(self.account.to_json())))

    def save_hype(self, account: Account, trades: list[Trade]) -> None:
        self.account = Account.from_json(json.loads(json.dumps(account.to_json())))
        self.trades.extend(trades)

    def hype_trades(self, n: int = 200) -> list[Trade]:
        return list(reversed(self.trades))[:n]

    def hype_stats(self) -> tuple[int, int, float, float, float]:
        pnls = [t.pnl for t in self.trades]
        return (len(pnls), sum(1 for x in pnls if x > 0), sum(pnls),
                max(pnls, default=0.0), min(pnls, default=0.0))


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


class HypeBot:
    def __init__(self, source: Source, store: HypeStore, rules: Rules | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.source, self.store, self.rules, self.clock = source, store, rules or Rules(), clock

    def account(self) -> Account:
        acct = self.store.load_hype()
        return acct or Account(cash=self.rules.bankroll, started=_iso(self.clock()))

    def step(self) -> Account:
        now = self.clock()
        acct = self.account()
        acct.cycles += 1
        closed: list[Trade] = []
        try:
            self._manage(acct, now, closed)
            if acct.cycles % SCAN_EVERY == 1 or not acct.watch:
                self._scan(acct, now)
            acct.last_error = ""
        except Exception as exc:  # noqa: BLE001 - keep the books consistent; retry next cycle
            log.warning("hype cycle failed: %s", exc)
            acct.last_error = f"{type(exc).__name__}: {exc}"[:300]
        acct.last_cycle = _iso(now)
        last = datetime.fromisoformat(acct.curve[-1][0]) if acct.curve else None
        if last is None or now - last >= timedelta(minutes=CURVE_EVERY_MIN):
            acct.curve = (acct.curve + [[_iso(now), round(equity(acct, self.rules), 2)]])[-CURVE_POINTS:]
        self.store.save_hype(acct, closed)
        return acct

    # -- open positions -------------------------------------------------------------------

    def _manage(self, acct: Account, now: datetime, closed: list[Trade]) -> None:
        if not acct.positions:
            return
        snaps = self.source.snapshots([p.token for p in acct.positions],
                                      {p.token: p.pair for p in acct.positions if p.pair})
        keep: list[Position] = []
        for p in acct.positions:
            s = snaps.get(p.token)
            if s is None:
                p.missing += 1
                if p.missing < MISSING_LIMIT:  # a data hiccup is not a rug: wait and see
                    keep.append(p)
                    continue
            else:
                p.missing = 0
                p.trail = (p.trail + [s.price])[-TRAIL_POINTS:]
                p.last, p.last_liquidity = s.price, s.liquidity
                p.peak = max(p.peak, s.price)
                if not p.half_taken and p.gain() >= self.rules.take_half_at:
                    half = p.qty / 2
                    got = sell_fill(s.price, half, s.liquidity, self.rules)
                    p.qty -= half
                    p.realized += got
                    acct.cash += got
                    p.half_taken = True
                    log.info("hype: took half of %s at %+.0f%% (+$%.2f)", p.symbol, p.gain() * 100, got)
            reason = exit_check(p, s, now, self.rules)
            if reason is None:
                keep.append(p)
                continue
            if "rug" in reason:
                got = max(p.stake * (1 + self.rules.rug_loss) - p.realized, 0.0) if not p.half_taken \
                    else 0.0
            else:
                got = sell_fill(s.price, p.qty, s.liquidity, self.rules)  # type: ignore[union-attr]
            acct.cash += got
            acct.cooldown[p.token] = _iso(now + timedelta(hours=self.rules.cooldown_hours))
            closed.append(Trade(p.token, p.symbol, p.url, p.opened, _iso(now), p.stake,
                                round(p.realized + got, 4), reason, p.why,
                                round(p.peak / p.entry_price - 1, 4)))
            log.info("hype: sold %s (%s) P&L $%.2f", p.symbol, reason, p.realized + got - p.stake)
        acct.positions = keep

    # -- scanning and buying --------------------------------------------------------------

    def _scan(self, acct: Account, now: datetime) -> None:
        acct.cooldown = {t: u for t, u in acct.cooldown.items() if datetime.fromisoformat(u) > now}
        held = {p.token for p in acct.positions}
        ranked = sorted((s for s in self.source.discover() if s.token not in held), key=score, reverse=True)
        watch, bought = [], 0
        for s in ranked:
            no = entry_check(s, self.rules)
            if s.token in acct.cooldown:
                no.append("traded recently (cooldown)")
            can_buy = (not no and len(acct.positions) < self.rules.max_open
                       and bought < self.rules.max_new_per_cycle and acct.cash >= self.rules.stake)
            if can_buy:
                self._buy(acct, s, now)
                bought += 1
            elif not no:
                no.append("no free slot" if len(acct.positions) >= self.rules.max_open else "no cash")
            if len(watch) < WATCH_SIZE:
                watch.append({"symbol": s.symbol, "url": s.url, "score": score(s), "price": s.price,
                              "mcap": s.market_cap, "liq": s.liquidity, "chg_5m": s.chg_5m,
                              "buy_ratio": round(s.buy_ratio, 3), "vol_accel": round(s.vol_accel, 2),
                              "bought": can_buy, "why_not": no[:3]})
        acct.watch = watch

    def _buy(self, acct: Account, s: Snapshot, now: datetime) -> None:
        stake = self.rules.stake
        eff, qty = buy_fill(s.price, stake, s.liquidity, self.rules)
        why = (f"score {score(s):.0f}: {s.buy_ratio:.0%} buys, volume {s.vol_accel:.1f}x normal, "
               f"+{s.chg_5m:.0f}% in 5 min, ${s.liquidity:,.0f} pool")
        acct.cash -= stake
        acct.positions.append(Position(
            token=s.token, symbol=s.symbol, url=s.url, opened=_iso(now), stake=stake, qty=qty,
            entry_price=eff, seen_price=s.price, entry_liquidity=s.liquidity, entry_score=score(s),
            why=why, peak=s.price, last=s.price, last_liquidity=s.liquidity, pair=s.pair,
            trail=[s.price]))
        log.info("hype: bought %s for $%.0f (%s)", s.symbol, stake, why)


def equity(acct: Account, rules: Rules) -> float:
    """Cash plus what open positions would sell for now (after costs)."""
    return acct.cash + sum(sell_fill(p.last, p.qty, p.last_liquidity, rules) for p in acct.positions)


class HypeRunner(threading.Thread):
    """Runs the bot every CYCLE_SECONDS, 24/7 (crypto never closes)."""

    def __init__(self, bot_factory: Callable[[], HypeBot], interval: float = CYCLE_SECONDS) -> None:
        super().__init__(name="hype-bot", daemon=True)
        self._factory, self._interval = bot_factory, interval
        self._stop = threading.Event()
        self.bot: HypeBot | None = None
        self.error = ""

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.bot is None:
                    self.bot = self._factory()
                self.bot.step()
                self.error = ""
            except Exception as exc:  # noqa: BLE001 - never let the thread die
                log.exception("hype runner error")
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                self.bot = None  # rebuild (e.g. fresh DB connection) next time
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()


def trade_to_row(t: Trade) -> dict[str, Any]:
    return asdict(t)
