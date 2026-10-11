"""Tiny PostgreSQL client for TESTS ONLY (stdlib, simple-query protocol, trust auth).

Production talks to PostgreSQL through psycopg. This sandbox cannot install packages, so
the test suite uses this minimal client to run the real SQL against a real local
PostgreSQL server. It mimics the small subset of the psycopg API the store uses:
connect(), cursor().execute(sql, params) with %s placeholders, fetchone/fetchall,
commit/rollback/close, `autocommit` and `closed`. Parameters are interpolated client-side
with strict literal quoting - acceptable for tests, never used in production.
"""

from __future__ import annotations

import json
import math
import socket
import struct
from datetime import date, datetime
from decimal import Decimal


class DatabaseError(Exception):
    pass


class IntegrityError(DatabaseError):
    pass


OIDS = {16: "bool", 20: "int", 21: "int", 23: "int", 700: "float", 701: "float",
        1700: "numeric", 1082: "date", 1184: "timestamptz", 114: "json", 3802: "json"}


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "'NaN'::float8"
        if math.isinf(v):
            return "'Infinity'::float8" if v > 0 else "'-Infinity'::float8"
        return repr(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return "'" + v.isoformat() + "'::timestamptz"
    if isinstance(v, date):
        return "'" + v.isoformat() + "'::date"
    if isinstance(v, str):
        if "\x00" in v:
            raise ValueError("NUL in string literal")
        return "'" + v.replace("'", "''") + "'"
    raise TypeError(f"unsupported parameter type {type(v)!r}")


def _interpolate(sql: str, params) -> str:
    if not params:
        return sql.replace("%%", "%")
    out, i, it = [], 0, iter(params)
    while i < len(sql):
        if sql.startswith("%%", i):
            out.append("%")
            i += 2
        elif sql.startswith("%s", i):
            out.append(_literal(next(it)))
            i += 2
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _convert(kind: str | None, raw: bytes | None):
    if raw is None:
        return None
    s = raw.decode()
    if kind == "bool":
        return s == "t"
    if kind == "int":
        return int(s)
    if kind in ("float", "numeric"):
        return float(s)
    if kind == "date":
        return date.fromisoformat(s)
    if kind == "timestamptz":
        s = s.replace(" ", "T")
        if s[-3] in "+-" and s[-3:].lstrip("+-").isdigit():
            s += ":00"
        return datetime.fromisoformat(s)
    if kind == "json":
        return json.loads(s)
    return s


class Cursor:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.rows: list[tuple] = []
        self.description = None
        self.rowcount = -1

    def execute(self, sql: str, params=None):
        if not self.conn.autocommit and not self.conn._in_tx:
            self.conn._simple("BEGIN")
            self.conn._in_tx = True
        self.rows, self.description, self.rowcount = self.conn._simple(_interpolate(sql, params))
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Connection:
    def __init__(self, host: str, port: int, user: str, dbname: str) -> None:
        self.sock = socket.create_connection((host, port), timeout=10)
        self.autocommit = False
        self._in_tx = False
        self.closed = False
        body = struct.pack("!I", 196608)
        for k, v in (("user", user), ("database", dbname), ("client_encoding", "UTF8")):
            body += k.encode() + b"\0" + v.encode() + b"\0"
        body += b"\0"
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        while True:
            t, payload = self._read()
            if t == b"R" and struct.unpack("!I", payload[:4])[0] != 0:
                raise DatabaseError("only trust auth is supported by the test client")
            if t == b"E":
                raise DatabaseError(self._error(payload))
            if t == b"Z":
                break

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise DatabaseError("connection closed")
            buf += chunk
        return buf

    def _read(self) -> tuple[bytes, bytes]:
        head = self._recv(5)
        t, length = head[:1], struct.unpack("!I", head[1:])[0]
        return t, self._recv(length - 4)

    @staticmethod
    def _error(payload: bytes) -> str:
        fields = {}
        for part in payload.split(b"\0"):
            if part:
                fields[part[:1]] = part[1:].decode(errors="replace")
        return f"{fields.get(b'C', '')}: {fields.get(b'M', '')}"

    def _simple(self, sql: str):
        data = sql.encode() + b"\0"
        self.sock.sendall(b"Q" + struct.pack("!I", len(data) + 4) + data)
        rows, kinds, names, error, count = [], [], None, None, -1
        while True:
            t, p = self._read()
            if t == b"T":
                n = struct.unpack("!H", p[:2])[0]
                pos, names, kinds = 2, [], []
                for _ in range(n):
                    end = p.index(b"\0", pos)
                    names.append(p[pos:end].decode())
                    oid = struct.unpack("!I", p[end + 7:end + 11])[0]
                    kinds.append(OIDS.get(oid))
                    pos = end + 19
            elif t == b"D":
                n = struct.unpack("!H", p[:2])[0]
                pos, vals = 2, []
                for i in range(n):
                    ln = struct.unpack("!i", p[pos:pos + 4])[0]
                    pos += 4
                    raw = None if ln < 0 else p[pos:pos + ln]
                    pos += max(ln, 0)
                    vals.append(_convert(kinds[i], raw))
                rows.append(tuple(vals))
            elif t == b"C":
                tag = p.rstrip(b"\0").decode().split()
                if tag and tag[-1].isdigit():
                    count = int(tag[-1])
            elif t == b"E":
                error = self._error(p)
            elif t == b"Z":
                if error:
                    if not self.autocommit:
                        self._in_tx = p[:1] != b"I"
                    cls = IntegrityError if error.startswith("23") else DatabaseError
                    raise cls(error)
                return rows, ([(n,) for n in names] if names else None), count

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self):
        if self._in_tx:
            self._simple("COMMIT")
            self._in_tx = False

    def rollback(self):
        if self._in_tx:
            self._simple("ROLLBACK")
            self._in_tx = False

    def close(self):
        if not self.closed:
            try:
                self.sock.sendall(b"X" + struct.pack("!I", 4))
            finally:
                self.sock.close()
                self.closed = True


def connect(host="127.0.0.1", port=55432, user="tl", dbname="postgres") -> Connection:
    return Connection(host, port, user, dbname)
