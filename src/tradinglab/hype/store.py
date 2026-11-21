"""PostgreSQL storage for the hype bot: one account row (JSON) per variant and a trade log.

Uses the main PostgresStore's connection and transaction helper, so it shares the same
DATABASE_URL and safety checks. Tables are created on first use and never touch the
stock bots' tables.

The first bot's account ("current") stays where it always was, in hype_state row 1. Every
other variant keeps its account in hype_accounts under its id. All trades share hype_trades,
told apart by the `account` column (rows from before the column existed count as "current").
"""

from __future__ import annotations

import json
from typing import Any

from tradinglab.hype.model import Account, Trade

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hype_state (
        id integer PRIMARY KEY CHECK (id = 1),
        payload text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS hype_trades (
        id bigserial PRIMARY KEY,
        token text NOT NULL, symbol text NOT NULL, url text NOT NULL,
        opened text NOT NULL, closed text NOT NULL,
        stake double precision NOT NULL, proceeds double precision NOT NULL,
        reason text NOT NULL, why text NOT NULL, peak_gain double precision NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS hype_accounts (
        id text PRIMARY KEY,
        payload text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "ALTER TABLE hype_trades ADD COLUMN IF NOT EXISTS account text NOT NULL DEFAULT 'current'",
)

CURRENT = "current"  # the original bot's account id


class PgHypeStore:
    def __init__(self, pg: Any, account: str = CURRENT) -> None:
        if account.startswith("_"):  # that prefix is for documents (load_doc/save_doc)
            raise ValueError("an account id can't start with an underscore")
        self.pg = pg  # a PostgresStore
        self.account = account
        with pg._tx() as cur:
            for sql in SCHEMA:
                cur.execute(sql)

    def load_hype(self) -> Account | None:
        if self.account == CURRENT:
            rows = self.pg._query("SELECT payload FROM hype_state WHERE id = 1")
        else:
            rows = self.pg._query("SELECT payload FROM hype_accounts WHERE id = %s", (self.account,))
        return Account.from_json(json.loads(rows[0][0])) if rows else None

    def save_hype(self, account: Account, trades: list[Trade]) -> None:
        payload = json.dumps(account.to_json(), separators=(",", ":"))
        with self.pg._tx() as cur:  # account and its closed trades commit together
            if self.account == CURRENT:
                cur.execute("INSERT INTO hype_state (id, payload, updated_at) VALUES (1, %s, now()) "
                            "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
                            (payload,))
            else:
                cur.execute("INSERT INTO hype_accounts (id, payload, updated_at) VALUES (%s, %s, now()) "
                            "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
                            (self.account, payload))
            for t in trades:
                cur.execute("INSERT INTO hype_trades (token, symbol, url, opened, closed, stake, proceeds, "
                            "reason, why, peak_gain, account) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (t.token, t.symbol, t.url, t.opened, t.closed, t.stake, t.proceeds,
                             t.reason, t.why, t.peak_gain, self.account))

    # A small JSON document kept beside the accounts (the scout's alerts and wallet records).
    # Same table and statements as an account; the key is prefixed so it can't be one.
    def load_doc(self, key: str) -> dict | None:
        rows = self.pg._query("SELECT payload FROM hype_accounts WHERE id = %s", ("_" + key,))
        return json.loads(rows[0][0]) if rows else None

    def save_doc(self, key: str, doc: dict) -> None:
        payload = json.dumps(doc, separators=(",", ":"))
        with self.pg._tx() as cur:
            cur.execute("INSERT INTO hype_accounts (id, payload, updated_at) VALUES (%s, %s, now()) "
                        "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
                        ("_" + key, payload))

    def hype_trades(self, n: int = 200) -> list[Trade]:
        rows = self.pg._query("SELECT token, symbol, url, opened, closed, stake, proceeds, reason, why, "
                              "peak_gain FROM hype_trades WHERE account = %s ORDER BY id DESC LIMIT %s",
                              (self.account, n))
        return [Trade(*r) for r in rows]

    def hype_stats(self) -> tuple[int, int, float, float, float]:
        """(trades, wins, total P&L, best, worst) over this account's whole history."""
        r = self.pg._query("SELECT count(*), count(*) FILTER (WHERE proceeds > stake), "
                           "coalesce(sum(proceeds - stake), 0), coalesce(max(proceeds - stake), 0), "
                           "coalesce(min(proceeds - stake), 0) FROM hype_trades WHERE account = %s",
                           (self.account,))[0]
        return int(r[0]), int(r[1]), float(r[2]), float(r[3]), float(r[4])
