"""PostgreSQL implementation of the engine Store (production persistence on Railway).

Same contract as InMemoryStore:
  * commit_session() writes the new portfolio state and every record of the session in ONE
    transaction. The sessions table's primary key makes a double commit impossible.
  * run_lock() takes a PostgreSQL session-level advisory lock on a dedicated connection, so
    two engine runs (e.g. the in-app scheduler and a manual cron run) cannot overlap.
  * Schema changes are applied by ordered, idempotent migrations recorded in schema_version.

The connection factory is injected: production passes psycopg.connect(DATABASE_URL); the
test suite passes a minimal client pointed at a real local PostgreSQL server.
"""

from __future__ import annotations

import contextlib
import json
import math
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime
from typing import Any

from tradinglab.engine.store import EngineBusyError, EngineEvent
from tradinglab.marketdata.bars import Bar
from tradinglab.sim.core import Decision, EquityPoint, Fill, PortfolioState, StepResult, Trade

ADVISORY_LOCK_KEY = 7_401_552_093  # arbitrary, fixed: "trading-lab engine run"

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE IF NOT EXISTS engine_state (
        id integer PRIMARY KEY CHECK (id = 1),
        state jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS sessions (
        session_date date PRIMARY KEY,
        committed_at timestamptz NOT NULL DEFAULT now(),
        bars jsonb NOT NULL
    );
    CREATE TABLE IF NOT EXISTS decisions (
        id bigserial PRIMARY KEY,
        session_date date NOT NULL REFERENCES sessions(session_date),
        symbol text NOT NULL,
        action text NOT NULL,
        outcome text NOT NULL,
        reason text NOT NULL,
        shares integer NOT NULL DEFAULT 0,
        stop double precision,
        diagnostics jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX IF NOT EXISTS decisions_session_idx ON decisions(session_date);
    CREATE TABLE IF NOT EXISTS fills (
        id bigserial PRIMARY KEY,
        session_date date NOT NULL REFERENCES sessions(session_date),
        symbol text NOT NULL,
        side text NOT NULL CHECK (side IN ('buy', 'sell')),
        shares integer NOT NULL CHECK (shares > 0),
        ref_price double precision NOT NULL,
        fill_price double precision NOT NULL,
        fees double precision NOT NULL,
        slippage_cost double precision NOT NULL,
        reason text NOT NULL,
        order_id text NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS trades (
        id bigserial PRIMARY KEY,
        symbol text NOT NULL,
        strategy text NOT NULL DEFAULT '',
        entry_day date NOT NULL,
        exit_day date NOT NULL REFERENCES sessions(session_date),
        shares integer NOT NULL,
        entry_price double precision NOT NULL,
        exit_price double precision NOT NULL,
        pnl double precision NOT NULL,
        return_pct double precision NOT NULL,
        r_multiple double precision,
        bars_held integer NOT NULL,
        costs double precision NOT NULL,
        entry_reason text NOT NULL,
        exit_reason text NOT NULL,
        dividends double precision NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS equity (
        session_date date PRIMARY KEY REFERENCES sessions(session_date),
        equity double precision NOT NULL,
        cash double precision NOT NULL,
        exposure double precision NOT NULL,
        open_positions integer NOT NULL,
        halted boolean NOT NULL
    );
    CREATE TABLE IF NOT EXISTS events (
        id bigserial PRIMARY KEY,
        at timestamptz NOT NULL,
        session_date date,
        kind text NOT NULL,
        detail text NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS flags (
        id bigserial PRIMARY KEY,
        name text NOT NULL,
        value boolean NOT NULL,
        at timestamptz NOT NULL
    );
    CREATE INDEX IF NOT EXISTS flags_name_idx ON flags(name, at);
    """,
]


def _clean_json(value: Any) -> Any:
    """jsonb rejects NaN/Infinity: store them as null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean_json(v) for v in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_clean_json(value), allow_nan=False, default=str)


def _finite_or_none(x: float | None) -> float | None:
    return x if x is not None and math.isfinite(x) else None


def _bar_to_list(b: Bar | None) -> list | None:
    if b is None:
        return None
    return [b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume, b.dividend, b.split]


def _list_to_bar(v: list | None) -> Bar | None:
    if v is None:
        return None
    return Bar(date.fromisoformat(v[0]), *[float(x) for x in v[1:]])


class PostgresStore:
    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect
        self._conn: Any = None
        self._lock = threading.RLock()

    # -- connection & schema ---------------------------------------------------------------

    def _db(self) -> Any:
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._connect()
        return self._conn

    @contextlib.contextmanager
    def _tx(self) -> Iterator[Any]:
        """One transaction; rolls back on any error. Serialised across threads."""
        with self._lock:
            conn = self._db()
            cur = conn.cursor()
            try:
                yield cur
                conn.commit()
            except BaseException:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001 - connection may be gone; reconnect next time
                    self._conn = None
                raise

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._tx() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def migrate(self) -> int:
        """Apply pending migrations; returns the schema version."""
        with self._tx() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version integer NOT NULL)")
            cur.execute("SELECT max(version) FROM schema_version")
            row = cur.fetchone()
            current = row[0] if row and row[0] is not None else 0
            for version, sql in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                cur.execute(sql)
                cur.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
                current = version
        return current

    # -- state -----------------------------------------------------------------------------

    def load_state(self) -> PortfolioState | None:
        rows = self._query("SELECT state FROM engine_state WHERE id = 1")
        if not rows:
            return None
        state = rows[0][0]
        if isinstance(state, str):
            state = json.loads(state)
        return PortfolioState.from_dict(state)

    def save_state(self, state: PortfolioState) -> None:
        payload = _json(state.to_dict())
        with self._tx() as cur:
            self._upsert_state(cur, payload)

    @staticmethod
    def _upsert_state(cur: Any, payload: str) -> None:
        cur.execute(
            "INSERT INTO engine_state (id, state, updated_at) VALUES (1, %s::jsonb, now()) "
            "ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()",
            (payload,))

    def commit_session(self, state: PortfolioState, result: StepResult,
                       bars_used: Mapping[str, Bar | None]) -> None:
        payload = _json(state.to_dict())  # serialise before touching the database
        bars = _json({s: _bar_to_list(b) for s, b in bars_used.items()})
        try:
            with self._tx() as cur:
                cur.execute("INSERT INTO sessions (session_date, bars) VALUES (%s, %s::jsonb)",
                            (result.day, bars))
                self._upsert_state(cur, payload)
                for d in result.decisions:
                    cur.execute(
                        "INSERT INTO decisions (session_date, symbol, action, outcome, reason, "
                        "shares, stop, diagnostics) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                        (d.day, d.symbol, d.action, d.outcome, d.reason, d.shares,
                         _finite_or_none(d.stop), _json(dict(d.diagnostics))))
                for f in result.fills:
                    cur.execute(
                        "INSERT INTO fills (session_date, symbol, side, shares, ref_price, "
                        "fill_price, fees, slippage_cost, reason, order_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (f.day, f.symbol, f.side, f.shares, f.ref_price, f.fill_price, f.fees,
                         f.slippage_cost, f.reason, f.order_id))
                for t in result.trades:
                    cur.execute(
                        "INSERT INTO trades (symbol, strategy, entry_day, exit_day, shares, "
                        "entry_price, exit_price, pnl, return_pct, r_multiple, bars_held, costs, "
                        "entry_reason, exit_reason, dividends) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (t.symbol, t.strategy, t.entry_day, t.exit_day, t.shares, t.entry_price,
                         t.exit_price, t.pnl, t.return_pct, _finite_or_none(t.r_multiple),
                         t.bars_held, t.costs, t.entry_reason, t.exit_reason, t.dividends))
                if result.equity is not None:
                    e = result.equity
                    cur.execute(
                        "INSERT INTO equity (session_date, equity, cash, exposure, "
                        "open_positions, halted) VALUES (%s, %s, %s, %s, %s, %s)",
                        (e.day, e.equity, e.cash, e.exposure, e.open_positions, e.halted))
        except Exception as exc:
            if "23505" in str(exc) or type(exc).__name__ in ("UniqueViolation", "IntegrityError"):
                raise ValueError(f"session {result.day} already committed") from exc
            raise

    # -- records ---------------------------------------------------------------------------

    def add_event(self, event: EngineEvent) -> None:
        with self._tx() as cur:
            cur.execute("INSERT INTO events (at, session_date, kind, detail) "
                        "VALUES (%s, %s, %s, %s)",
                        (event.at, event.session, event.kind, event.detail))

    def events(self) -> list[EngineEvent]:
        return self.recent_events(None)

    def recent_events(self, n: int | None = 50) -> list[EngineEvent]:
        sql = "SELECT at, session_date, kind, detail FROM events ORDER BY id DESC"
        rows = self._query(sql + (" LIMIT %s" if n else ""), (n,) if n else ())
        return [EngineEvent(*r) for r in reversed(rows)]

    _DEC_COLS = "session_date, symbol, action, outcome, reason, shares, stop, diagnostics"

    def decisions(self) -> list[Decision]:
        return self.recent_decisions(None)

    def recent_decisions(self, n: int | None = 100) -> list[Decision]:
        sql = f"SELECT {self._DEC_COLS} FROM decisions ORDER BY id DESC"
        rows = self._query(sql + (" LIMIT %s" if n else ""), (n,) if n else ())
        out = []
        for r in reversed(rows):
            diag = r[7] if not isinstance(r[7], str) else json.loads(r[7])
            out.append(Decision(r[0], r[1], r[2], r[3], r[4], r[5], r[6], diag or {}))
        return out

    def fills(self) -> list[Fill]:
        rows = self._query("SELECT session_date, symbol, side, shares, ref_price, fill_price, "
                           "fees, slippage_cost, reason, order_id FROM fills ORDER BY id")
        return [Fill(*r) for r in rows]

    def trades(self) -> list[Trade]:
        rows = self._query(
            "SELECT symbol, entry_day, exit_day, shares, entry_price, exit_price, pnl, "
            "return_pct, r_multiple, bars_held, costs, entry_reason, exit_reason, dividends, "
            "strategy FROM trades ORDER BY id")
        return [Trade(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                      r[8] if r[8] is not None else float("nan"), *r[9:]) for r in rows]

    def equity_curve(self) -> list[EquityPoint]:
        rows = self._query("SELECT session_date, equity, cash, exposure, open_positions, halted "
                           "FROM equity ORDER BY session_date")
        return [EquityPoint(*r) for r in rows]

    def bars_used(self, session: date) -> dict[str, Bar | None]:
        rows = self._query("SELECT bars FROM sessions WHERE session_date = %s", (session,))
        if not rows:
            return {}
        bars = rows[0][0] if not isinstance(rows[0][0], str) else json.loads(rows[0][0])
        return {s: _list_to_bar(v) for s, v in bars.items()}

    # -- flags -----------------------------------------------------------------------------

    def get_flag(self, name: str, default: bool = False) -> bool:
        rows = self._query("SELECT value FROM flags WHERE name = %s ORDER BY id DESC LIMIT 1",
                           (name,))
        return rows[0][0] if rows else default

    def set_flag(self, name: str, value: bool, at: datetime | None = None) -> None:
        with self._tx() as cur:
            cur.execute("INSERT INTO flags (name, value, at) VALUES (%s, %s, %s)",
                        (name, bool(value), at or datetime.now(UTC)))

    def flag_at(self, name: str, when: datetime, default: bool = False) -> bool:
        rows = self._query("SELECT value FROM flags WHERE name = %s AND at <= %s "
                           "ORDER BY at DESC, id DESC LIMIT 1", (name, when))
        return rows[0][0] if rows else default

    # -- concurrency -----------------------------------------------------------------------

    @contextlib.contextmanager
    def run_lock(self) -> Iterator[None]:
        conn = self._connect()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            if not cur.fetchone()[0]:
                raise EngineBusyError("another engine run is in progress")
            try:
                yield
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        finally:
            conn.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not getattr(self._conn, "closed", False):
                self._conn.close()
            self._conn = None
