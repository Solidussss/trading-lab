"""Storage for the day-trading bots: shared 5-minute prices, and one account per bot.

Contract (both implementations):
  * commit_bot_day() writes a bot's new state and every record of that day in ONE transaction;
    committing the same (bot, day) twice raises ValueError.
  * 5-minute bars are shared by all bots; ibar_fetches records which (symbol, day) has been
    fetched and whether it was fetched after the session ended ("final").
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from tradinglab.intraday.core import BotState, DayResult, IFill, ISignal, ITrade
from tradinglab.marketdata.intraday import IBar


@dataclass(frozen=True)
class BotDay:
    day: date
    start_equity: float
    end_equity: float
    trades: int
    halted: bool
    excluded: dict[str, str]

    @property
    def pnl(self) -> float:
        return self.end_equity - self.start_equity


@dataclass(frozen=True)
class FetchInfo:
    bars: int
    final: bool
    fetched_at: datetime


class InMemoryIntradayStore:
    """Reference implementation (tests, previews); mixed into InMemoryStore."""

    def _istore(self) -> dict[str, Any]:
        if not hasattr(self, "_intraday"):
            self._intraday = {"bars": {}, "fetches": {}, "bots": {}, "days": {}, "trades": {},
                              "fills": {}, "signals": {}, "backtests": {}}
        return self._intraday

    # prices
    def save_ibars(self, symbol: str, bars: Iterable[IBar]) -> None:
        book = self._istore()["bars"].setdefault(symbol, {})
        for b in bars:
            book[b.ts] = b

    def replace_ibars(self, symbol: str, start: date, end: date, bars: Iterable[IBar]) -> None:
        """Per day in [start, end]: replace the stored bars (a re-fetch may restamp them), but
        only when the new response has at least as many bars; a short or empty response never
        wipes better data."""
        new: dict[date, list[IBar]] = {}
        for b in bars:
            new.setdefault(b.day, []).append(b)
        book = self._istore()["bars"].setdefault(symbol, {})
        for day, got in new.items():
            if not start <= day <= end:
                continue
            old = [ts for ts, b in book.items() if b.day == day]
            if len(got) >= len(old):
                for ts in old:
                    del book[ts]
                self.save_ibars(symbol, got)

    def ibars(self, symbol: str, start: date, end: date | None = None) -> list[IBar]:
        end = end or start
        book = self._istore()["bars"].get(symbol, {})
        return sorted((b for b in book.values() if start <= b.day <= end), key=lambda b: b.ts)

    def mark_fetched(self, symbol: str, day: date, bars: int, final: bool,
                     at: datetime | None = None) -> None:
        self._istore()["fetches"][(symbol, day)] = FetchInfo(bars, final, at or datetime.now(UTC))

    def fetches(self, symbol: str, start: date, end: date) -> dict[date, FetchInfo]:
        return {d: f for (s, d), f in self._istore()["fetches"].items()
                if s == symbol and start <= d <= end}

    # bots
    def load_bot(self, bot_id: str) -> BotState | None:
        st = self._istore()["bots"].get(bot_id)
        return copy.deepcopy(st) if st else None

    def save_bot(self, bot_id: str, state: BotState) -> None:
        self._istore()["bots"][bot_id] = copy.deepcopy(state)

    def commit_bot_day(self, bot_id: str, state: BotState, result: DayResult) -> None:
        s = self._istore()
        days = s["days"].setdefault(bot_id, {})
        if result.day in days:
            raise ValueError(f"{bot_id} {result.day} already committed")
        days[result.day] = BotDay(result.day, result.start_equity, result.end_equity,
                                  len(result.trades), result.halted, dict(result.excluded))
        s["trades"].setdefault(bot_id, []).extend(result.trades)
        s["fills"].setdefault(bot_id, []).extend((result.day, f) for f in result.fills)
        s["signals"].setdefault(bot_id, []).extend((result.day, g) for g in result.signals)
        s["bots"][bot_id] = copy.deepcopy(state)

    def bot_days(self, bot_id: str) -> list[BotDay]:
        return [v for _, v in sorted(self._istore()["days"].get(bot_id, {}).items())]

    def bot_trades(self, bot_id: str, day: date | None = None) -> list[ITrade]:
        return [t for t in self._istore()["trades"].get(bot_id, []) if day is None or t.day == day]

    def bot_fills(self, bot_id: str, day: date) -> list[IFill]:
        return [f for d, f in self._istore()["fills"].get(bot_id, []) if d == day]

    def bot_signals(self, bot_id: str, day: date) -> list[ISignal]:
        return [g for d, g in self._istore()["signals"].get(bot_id, []) if d == day]

    def reset_bot(self, bot_id: str) -> None:
        s = self._istore()
        for k in ("bots", "days", "trades", "fills", "signals"):
            s[k].pop(bot_id, None)

    # research
    def save_backtest(self, bot_id: str, payload: dict[str, Any]) -> None:
        self._istore()["backtests"][bot_id] = copy.deepcopy(payload)

    def backtest(self, bot_id: str) -> dict[str, Any] | None:
        b = self._istore()["backtests"].get(bot_id)
        return copy.deepcopy(b) if b else None
