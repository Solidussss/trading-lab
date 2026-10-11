"""PostgreSQL implementation of the day-trading bot storage (mixed into PostgresStore)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from tradinglab.engine.intraday_store import BotDay, FetchInfo
from tradinglab.intraday.core import BotState, DayResult, IFill, ISignal, ITrade
from tradinglab.marketdata.calendar import NY
from tradinglab.marketdata.intraday import IBar

INTRADAY_MIGRATION = """
CREATE TABLE IF NOT EXISTS ibars (
    symbol text NOT NULL,
    ts timestamptz NOT NULL,
    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS ibar_fetches (
    symbol text NOT NULL,
    day date NOT NULL,
    bars integer NOT NULL,
    final boolean NOT NULL,
    fetched_at timestamptz NOT NULL,
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS bots (
    bot_id text PRIMARY KEY,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bot_days (
    bot_id text NOT NULL,
    day date NOT NULL,
    start_equity double precision NOT NULL,
    end_equity double precision NOT NULL,
    trades integer NOT NULL,
    halted boolean NOT NULL,
    excluded jsonb NOT NULL DEFAULT '{}'::jsonb,
    committed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bot_id, day)
);
CREATE TABLE IF NOT EXISTS bot_trades (
    id bigserial PRIMARY KEY,
    bot_id text NOT NULL,
    day date NOT NULL,
    symbol text NOT NULL,
    strategy text NOT NULL,
    entry_ts timestamptz NOT NULL,
    exit_ts timestamptz NOT NULL,
    shares integer NOT NULL CHECK (shares > 0),
    entry_price double precision NOT NULL,
    exit_price double precision NOT NULL,
    pnl double precision NOT NULL,
    costs double precision NOT NULL,
    return_pct double precision NOT NULL,
    r_multiple double precision,
    entry_reason text NOT NULL,
    exit_reason text NOT NULL,
    FOREIGN KEY (bot_id, day) REFERENCES bot_days(bot_id, day)
);
CREATE INDEX IF NOT EXISTS bot_trades_idx ON bot_trades(bot_id, day);
CREATE TABLE IF NOT EXISTS bot_fills (
    id bigserial PRIMARY KEY,
    bot_id text NOT NULL,
    day date NOT NULL,
    ts timestamptz NOT NULL,
    symbol text NOT NULL,
    side text NOT NULL CHECK (side IN ('buy', 'sell')),
    shares integer NOT NULL CHECK (shares > 0),
    ref_price double precision NOT NULL,
    fill_price double precision NOT NULL,
    fees double precision NOT NULL,
    reason text NOT NULL,
    FOREIGN KEY (bot_id, day) REFERENCES bot_days(bot_id, day)
);
CREATE INDEX IF NOT EXISTS bot_fills_idx ON bot_fills(bot_id, day);
CREATE TABLE IF NOT EXISTS bot_signals (
    id bigserial PRIMARY KEY,
    bot_id text NOT NULL,
    day date NOT NULL,
    ts timestamptz NOT NULL,
    symbol text NOT NULL,
    action text NOT NULL,
    outcome text NOT NULL,
    reason text NOT NULL,
    diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb,
    FOREIGN KEY (bot_id, day) REFERENCES bot_days(bot_id, day)
);
CREATE INDEX IF NOT EXISTS bot_signals_idx ON bot_signals(bot_id, day);
CREATE TABLE IF NOT EXISTS backtests (
    bot_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


def _f(x: float | None) -> float | None:
    return x if x is not None and math.isfinite(x) else None


def _js(v: Any) -> Any:
    return v if not isinstance(v, str) else json.loads(v)


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    a = datetime.combine(start, datetime.min.time(), tzinfo=NY).astimezone(UTC)
    b = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=NY).astimezone(UTC)
    return a, b


class IntradayPgMixin:
    """Requires self._tx(), self._query() and self._json() from PostgresStore."""

    _tx: Any
    _query: Any

    # prices
    def save_ibars(self, symbol: str, bars: Iterable[IBar]) -> None:
        with self._tx() as cur:
            for b in bars:
                cur.execute(
                    "INSERT INTO ibars (symbol, ts, open, high, low, close, volume) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (symbol, ts) DO UPDATE SET "
                    "open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, "
                    "close = EXCLUDED.close, volume = EXCLUDED.volume",
                    (symbol, b.ts, b.open, b.high, b.low, b.close, b.volume))

    def replace_ibars(self, symbol: str, start: date, end: date, bars: Iterable[IBar]) -> None:
        """Per day: replace only when the new response has at least as many bars."""
        new: dict[date, list[IBar]] = {}
        for x in bars:
            if start <= x.day <= end:
                new.setdefault(x.day, []).append(x)
        with self._tx() as cur:
            for day, got in new.items():
                a, b = _day_bounds(day, day)
                cur.execute("SELECT count(*) FROM ibars WHERE symbol = %s AND ts >= %s AND ts < %s",
                            (symbol, a, b))
                if len(got) < cur.fetchone()[0]:
                    continue
                cur.execute("DELETE FROM ibars WHERE symbol = %s AND ts >= %s AND ts < %s",
                            (symbol, a, b))
                for x in got:
                    cur.execute("INSERT INTO ibars (symbol, ts, open, high, low, close, volume) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                (symbol, x.ts, x.open, x.high, x.low, x.close, x.volume))

    def ibars(self, symbol: str, start: date, end: date | None = None) -> list[IBar]:
        a, b = _day_bounds(start, end or start)
        rows = self._query("SELECT ts, open, high, low, close, volume FROM ibars WHERE symbol = %s "
                           "AND ts >= %s AND ts < %s ORDER BY ts", (symbol, a, b))
        return [IBar(r[0].astimezone(UTC), *map(float, r[1:])) for r in rows]

    def mark_fetched(self, symbol: str, day: date, bars: int, final: bool,
                     at: datetime | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO ibar_fetches (symbol, day, bars, final, fetched_at) VALUES "
                "(%s, %s, %s, %s, %s) ON CONFLICT (symbol, day) DO UPDATE SET bars = EXCLUDED.bars, "
                "final = EXCLUDED.final, fetched_at = EXCLUDED.fetched_at",
                (symbol, day, bars, final, at or datetime.now(UTC)))

    def fetches(self, symbol: str, start: date, end: date) -> dict[date, FetchInfo]:
        rows = self._query("SELECT day, bars, final, fetched_at FROM ibar_fetches WHERE symbol = %s "
                           "AND day BETWEEN %s AND %s", (symbol, start, end))
        return {r[0]: FetchInfo(r[1], r[2], r[3]) for r in rows}

    # bots
    def load_bot(self, bot_id: str) -> BotState | None:
        rows = self._query("SELECT state FROM bots WHERE bot_id = %s", (bot_id,))
        return BotState.from_dict(_js(rows[0][0])) if rows else None

    def save_bot(self, bot_id: str, state: BotState) -> None:
        with self._tx() as cur:
            self._upsert_bot(cur, bot_id, state)

    def _upsert_bot(self, cur: Any, bot_id: str, state: BotState) -> None:
        cur.execute("INSERT INTO bots (bot_id, state, updated_at) VALUES (%s, %s::jsonb, now()) "
                    "ON CONFLICT (bot_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
                    (bot_id, self._json(state.to_dict())))

    def commit_bot_day(self, bot_id: str, state: BotState, result: DayResult) -> None:
        excluded = self._json(dict(result.excluded))
        try:
            with self._tx() as cur:
                cur.execute("INSERT INTO bot_days (bot_id, day, start_equity, end_equity, trades, "
                            "halted, excluded) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                            (bot_id, result.day, result.start_equity, result.end_equity,
                             len(result.trades), result.halted, excluded))
                for t in result.trades:
                    cur.execute(
                        "INSERT INTO bot_trades (bot_id, day, symbol, strategy, entry_ts, exit_ts, "
                        "shares, entry_price, exit_price, pnl, costs, return_pct, r_multiple, "
                        "entry_reason, exit_reason) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (bot_id, t.day, t.symbol, t.strategy, t.entry_ts, t.exit_ts, t.shares,
                         t.entry_price, t.exit_price, t.pnl, t.costs, t.return_pct,
                         _f(t.r_multiple), t.entry_reason, t.exit_reason))
                for f in result.fills:
                    cur.execute(
                        "INSERT INTO bot_fills (bot_id, day, ts, symbol, side, shares, ref_price, "
                        "fill_price, fees, reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (bot_id, result.day, f.ts, f.symbol, f.side, f.shares, f.ref_price,
                         f.fill_price, f.fees, f.reason))
                for g in result.signals:
                    cur.execute(
                        "INSERT INTO bot_signals (bot_id, day, ts, symbol, action, outcome, reason, "
                        "diagnostics) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                        (bot_id, result.day, g.ts, g.symbol, g.action, g.outcome, g.reason,
                         self._json(dict(g.diagnostics))))
                self._upsert_bot(cur, bot_id, state)
        except Exception as exc:
            if "23505" in str(exc) or type(exc).__name__ in ("UniqueViolation", "IntegrityError"):
                raise ValueError(f"{bot_id} {result.day} already committed") from exc
            raise

    def bot_days(self, bot_id: str) -> list[BotDay]:
        rows = self._query("SELECT day, start_equity, end_equity, trades, halted, excluded "
                           "FROM bot_days WHERE bot_id = %s ORDER BY day", (bot_id,))
        return [BotDay(r[0], r[1], r[2], r[3], r[4], _js(r[5]) or {}) for r in rows]

    _TRADE_COLS = ("day, symbol, strategy, entry_ts, exit_ts, shares, entry_price, exit_price, "
                   "pnl, costs, return_pct, r_multiple, entry_reason, exit_reason")

    def bot_trades(self, bot_id: str, day: date | None = None) -> list[ITrade]:
        sql = f"SELECT {self._TRADE_COLS} FROM bot_trades WHERE bot_id = %s"
        params: tuple = (bot_id,)
        if day is not None:
            sql, params = sql + " AND day = %s", (bot_id, day)
        rows = self._query(sql + " ORDER BY id", params)
        return [ITrade(r[0], r[1], r[2], r[3].astimezone(UTC), r[4].astimezone(UTC), *r[5:11],
                       r[11] if r[11] is not None else math.nan, r[12], r[13]) for r in rows]

    def bot_fills(self, bot_id: str, day: date) -> list[IFill]:
        rows = self._query("SELECT ts, symbol, side, shares, ref_price, fill_price, fees, reason "
                           "FROM bot_fills WHERE bot_id = %s AND day = %s ORDER BY id", (bot_id, day))
        return [IFill(r[0].astimezone(UTC), *r[1:]) for r in rows]

    def bot_signals(self, bot_id: str, day: date) -> list[ISignal]:
        rows = self._query("SELECT ts, symbol, action, outcome, reason, diagnostics FROM bot_signals "
                           "WHERE bot_id = %s AND day = %s ORDER BY id", (bot_id, day))
        return [ISignal(r[0].astimezone(UTC), r[1], r[2], r[3], r[4], _js(r[5]) or {}) for r in rows]

    def reset_bot(self, bot_id: str) -> None:
        with self._tx() as cur:
            for table in ("bot_signals", "bot_fills", "bot_trades", "bot_days", "bots"):
                cur.execute(f"DELETE FROM {table} WHERE bot_id = %s", (bot_id,))  # noqa: S608

    # research
    def save_backtest(self, bot_id: str, payload: dict[str, Any]) -> None:
        with self._tx() as cur:
            cur.execute("INSERT INTO backtests (bot_id, payload, updated_at) VALUES "
                        "(%s, %s::jsonb, now()) ON CONFLICT (bot_id) DO UPDATE SET "
                        "payload = EXCLUDED.payload, updated_at = now()",
                        (bot_id, self._json(payload)))

    def backtest(self, bot_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT payload FROM backtests WHERE bot_id = %s", (bot_id,))
        return _js(rows[0][0]) if rows else None
