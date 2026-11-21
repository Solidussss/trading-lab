"""Reading a wallet's trades from the Solana blockchain itself (read-only, no key, no account).

Every Solana trade is public. A node answers two questions this module asks, and nothing else:

  getSignaturesForAddress   the latest transactions an address took part in
  getTransaction            one transaction, including every token balance before and after

A buy is a transaction after which the wallet holds more of one coin and less SOL; a sell is
the reverse. That reading works whichever exchange or trading app the wallet used, because it
looks only at what the wallet ended up with.

Why this cannot trade: moving a coin needs a transaction signed with a wallet's private key.
This program has no wallet and no key, builds no transactions, and `SolanaReadClient` refuses
to send a node any request other than the two reads above (tests/test_traders.py holds it to
that). The nodes are the Solana Foundation's public one and one public fallback
(safety/endpoints.py); both are free and rate-limited, so the caller paces itself.

Provider facts (checked 2026-11-21 against live wallets, both nodes, no key):
  * POST {"jsonrpc":"2.0","id":1,"method":...,"params":[...]}; replies are a few KB each and
    arrive in about 0.2 s.
  * getSignaturesForAddress(addr, {"limit": n, "until": sig}) lists newest first, each with
    "signature", "blockTime" and "err" (null when the transaction succeeded); "until" stops
    at a signature already seen.
  * getTransaction(sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1})
    carries meta.preTokenBalances / postTokenBalances ({"owner", "mint", "uiTokenAmount"})
    and meta.preBalances / postBalances in lamports, in the order of the account keys.
    Without the version setting, newer transactions are refused with error -32015.
  * What a node lists for an address is every transaction that merely mentions it, and
    well-known wallets are mentioned constantly by other people's programs: of 40 well-known
    trader wallets measured, 8 were listed in 600 to 5,000 transactions a minute, nearly all
    of them somebody else's (many failed on purpose). So a listed transaction proves nothing
    until it is read, and parse_move only believes a change in the wallet's own balances,
    which nobody else can cause: taking SOL or coins out of a wallet needs its signature.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tradinglab.safety.endpoints import CHAIN_READ_URLS
from tradinglab.safety.network_guard import install_network_guard, policy

READ_METHODS = frozenset({"getSignaturesForAddress", "getTransaction"})
MAX_BYTES = 2 * 1024 * 1024
WRAPPED_SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MONEY = frozenset({WRAPPED_SOL, USDC})
MIN_SOL = 0.01  # less SOL than this moving is fees and tips, not a trade
_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_SIGNATURE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{64,90}")

HttpPost = Callable[[str, bytes, dict[str, str], float], tuple]


class ChainError(RuntimeError):
    """A node refused, failed or answered with something unusable."""


def is_address(text: Any) -> bool:
    return isinstance(text, str) and _ADDRESS.fullmatch(text) is not None


@dataclass(frozen=True)
class Move:
    """One buy or sell by a wallet. `tokens` and `sol` are sizes (never negative); `sol` is 0
    when the wallet paid or was paid in dollars (USDC), with the amount in `usd`."""

    wallet: str
    token: str
    side: str  # "buy" | "sell"
    tokens: float
    sol: float
    usd: float
    at: datetime
    sig: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _urllib_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read(4096) if exc.fp else b"")


def _amount(balance: dict) -> float:
    try:
        ui = balance.get("uiTokenAmount") or {}
        x = float(ui.get("uiAmountString") or ui.get("uiAmount") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    return x if x == x and 0 <= x < 1e18 else 0.0


def parse_move(tx: Any, wallet: str, sig: str = "") -> Move | None:
    """What `wallet` did in one transaction: a buy, a sell, or None (it failed, it was a plain
    transfer, a swap between two coins, or anything else that isn't coin-for-money)."""
    if not isinstance(tx, dict):
        return None
    meta, message = tx.get("meta"), (tx.get("transaction") or {}).get("message")
    if not isinstance(meta, dict) or not isinstance(message, dict) or meta.get("err") is not None:
        return None
    held: dict[str, float] = {}
    for side, sign in (("preTokenBalances", -1.0), ("postTokenBalances", 1.0)):
        for b in meta.get(side) or []:
            if isinstance(b, dict) and b.get("owner") == wallet and isinstance(b.get("mint"), str):
                held[b["mint"]] = held.get(b["mint"], 0.0) + sign * _amount(b)
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in message.get("accountKeys") or []]
    sol = held.pop(WRAPPED_SOL, 0.0)
    try:
        i = keys.index(wallet)
        sol += (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    except (ValueError, KeyError, IndexError, TypeError):
        pass  # the wallet's own SOL didn't move in this transaction
    usd = held.pop(USDC, 0.0)
    coins = {m: d for m, d in held.items() if abs(d) > 1e-9}
    if len(coins) != 1:
        return None
    (token, change), = coins.items()
    paid = sol <= -MIN_SOL or usd <= -1.0
    got = sol >= MIN_SOL or usd >= 1.0
    if change > 0 and paid and not got:
        kind = "buy"
    elif change < 0 and got and not paid:
        kind = "sell"
    else:
        return None  # a transfer in or out, or something stranger
    try:
        at = datetime.fromtimestamp(int(tx.get("blockTime")), UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return Move(wallet, token, kind, abs(change), abs(sol) if abs(sol) >= MIN_SOL else 0.0,
                abs(usd) if abs(usd) >= 1.0 else 0.0, at, sig)


class SolanaReadClient:
    """Asks a public node the two read-only questions. One attempt per node, in order; the
    node that last answered is asked first next time."""

    def __init__(self, urls: Sequence[str] = CHAIN_READ_URLS, *, http_post: HttpPost = _urllib_post,
                 timeout: float = 8.0, check_url: Callable[[str], Any] | None = None) -> None:
        install_network_guard()
        self._urls, self._http_post, self.timeout = list(urls), http_post, timeout
        self._check_url = check_url or (lambda url: policy().check_url(url))

    def _call(self, method: str, params: list) -> Any:
        if method not in READ_METHODS:  # the whole point: nothing but reads ever leaves here
            raise ChainError(f"refused: {method!r} is not one of the read-only requests")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        problems: list[str] = []
        for url in list(self._urls):
            host = url.split("/")[2]
            try:
                self._check_url(url)
                status, raw = self._http_post(url, body, {"Content-Type": "application/json",
                                                          "Accept": "application/json",
                                                          "User-Agent": "trading-lab-paper-bot"},
                                              self.timeout)
            except (OSError, TimeoutError) as exc:
                problems.append(f"{host}: {type(exc).__name__}")
                continue
            if status != 200 or len(raw) > MAX_BYTES:
                problems.append(f"{host}: HTTP {status}" if status != 200 else f"{host}: reply too large")
                continue
            try:
                reply = json.loads(raw)
            except ValueError:
                problems.append(f"{host}: malformed reply")
                continue
            if not isinstance(reply, dict) or "result" not in reply:
                err = reply.get("error") if isinstance(reply, dict) else None
                code = err.get("code") if isinstance(err, dict) else "?"
                if code == -32015:  # a transaction newer than this client reads: skip it
                    return None
                problems.append(f"{host}: error {code}")
                continue
            if url != self._urls[0]:  # this node works: ask it first from now on
                self._urls.remove(url)
                self._urls.insert(0, url)
            return reply["result"]
        raise ChainError("; ".join(problems) or "no nodes configured")

    def signatures(self, wallet: str, until: str = "", limit: int = 100) -> list[dict]:
        """The latest transactions that mention the wallet, newest first, back to `until`
        (not included). At most 1,000, the most a node gives in one answer."""
        if not is_address(wallet):
            raise ChainError("not a wallet address")
        opts: dict[str, Any] = {"limit": min(max(int(limit), 1), 1000)}
        if until:
            if _SIGNATURE.fullmatch(until) is None:
                raise ChainError("not a transaction signature")
            opts["until"] = until
        rows = self._call("getSignaturesForAddress", [wallet, opts])
        return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("signature"), str)]

    def move(self, sig: str, wallet: str) -> Move | None:
        """What the wallet did in that transaction (see parse_move)."""
        if _SIGNATURE.fullmatch(sig) is None or not is_address(wallet):
            raise ChainError("not a transaction signature")
        tx = self._call("getTransaction", [sig, {"encoding": "jsonParsed",
                                                 "maxSupportedTransactionVersion": 1}])
        return parse_move(tx, wallet, sig)

    def node(self) -> str:
        return self._urls[0].split("/")[2] if self._urls else ""
