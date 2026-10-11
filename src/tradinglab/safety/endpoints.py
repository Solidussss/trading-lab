"""Egress policy: the only network destinations this process may ever reach.

Trading Lab never connects to a brokerage. All trading is simulated in-process against
market data. The egress policy makes that structural rather than a convention:

  * DENY first: any host under a known brokerage / exchange-trading domain raises
    BrokerageForbiddenError, even if someone adds it to the allowlist by mistake.
  * ALLOW only: the market-data provider (api.tiingo.com), the Claude API
    (api.anthropic.com), the configured PostgreSQL host, and loopback/private addresses.
  * Everything else raises EgressDeniedError (fail closed), including hosts we have never
    heard of and public IP literals (which cannot be checked against a hostname list).

Hosts are IDNA-normalised exactly as the socket layer would before any check, so Unicode
look-alikes, trailing dots, ports, case and zero-width characters cannot disguise a host.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

DATA_PROVIDER_HOST: Final = "api.tiingo.com"
DATA_PROVIDER_URL: Final = f"https://{DATA_PROVIDER_HOST}"
RESEARCH_HOST: Final = "api.anthropic.com"

DEFAULT_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({DATA_PROVIDER_HOST, RESEARCH_HOST})

# Domains of brokerages and trading venues with order-entry APIs. Matched as the domain
# itself or any subdomain. This list exists for clear errors and defence in depth: the
# allowlist above would already refuse them.
BROKERAGE_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "alpaca.markets", "interactivebrokers.com", "interactivebrokers.ca", "ibkr.com",
        "ibllc.com", "questrade.com", "wealthsimple.com", "tradier.com", "schwabapi.com",
        "schwab.com", "tdameritrade.com", "etrade.com", "robinhood.com", "tastytrade.com",
        "tastyworks.com", "tradestation.com", "webull.com", "moomoo.com", "futunn.com",
        "oanda.com", "fxcm.com", "lime.co", "public.com", "firstrade.com", "ally.com",
        "fidelity.com", "vanguard.com", "qtrade.ca", "td.com", "rbcdirectinvesting.com",
        "bmoinvestorline.com", "cibc.com", "scotiaitrade.com", "nbdb.ca", "disnat.com",
        "coinbase.com", "kraken.com", "binance.com", "binance.us", "gemini.com",
        "bitstamp.net", "deribit.com", "bybit.com", "okx.com",
    }
)


class BrokerageForbiddenError(RuntimeError):
    """Raised whenever anything attempts to reach a brokerage or trading venue."""


class EgressDeniedError(RuntimeError):
    """Raised for any destination that is not on the egress allowlist."""


def normalize_host(host: str) -> str:
    """Canonical ASCII host: IDNA-encoded, lower-case, no whitespace, trailing dot, or port."""
    h = host.strip()
    if not h.isascii():
        try:
            h = h.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise EgressDeniedError("Unconvertible non-ASCII hostname refused.") from exc
    h = h.lower()
    if h.startswith("[") and "]" in h:  # IPv6 literal
        return h[1 : h.index("]")]
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def is_brokerage_host(host: str) -> bool:
    h = normalize_host(host)
    return any(h == d or h.endswith("." + d) for d in BROKERAGE_DOMAINS)


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class EgressPolicy:
    """Hostname allowlist. Hosts can be added until `freeze()`; the denylist always wins."""

    def __init__(self, allowed: Iterable[str] = DEFAULT_ALLOWED_HOSTS) -> None:
        self._allowed: set[str] = set()
        self._frozen = False
        self.allow(allowed)

    def allow(self, hosts: Iterable[str]) -> None:
        normalized = [normalize_host(h) for h in hosts]
        for h in normalized:
            if is_brokerage_host(h):
                raise BrokerageForbiddenError(f"Refusing to allow brokerage host {h!r}.")
        new = set(normalized) - self._allowed
        if new and self._frozen:
            raise EgressDeniedError("Egress policy is frozen; refusing to widen it.")
        self._allowed |= new

    def freeze(self) -> None:
        self._frozen = True

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(self._allowed)

    def check(self, host: str) -> str:
        """Return the normalised host if allowed; raise otherwise."""
        h = normalize_host(host)
        if not h:
            raise EgressDeniedError("Empty host.")
        if is_brokerage_host(h):
            raise BrokerageForbiddenError(f"Refusing to connect to brokerage host {h!r}.")
        if h == "localhost" or h.endswith(".localhost"):
            return h
        ip = _ip(h)
        if ip is not None:
            if ip.is_loopback or ip.is_private or ip.is_link_local:
                return h
            raise EgressDeniedError(f"Public IP literal {h!r} cannot be verified; refused.")
        if h in self._allowed:
            return h
        raise EgressDeniedError(f"Host {h!r} is not on the egress allowlist.")

    def check_url(self, url: str) -> str:
        """Validate an https URL against the policy. Returns the normalised host."""
        if not isinstance(url, str) or not url.strip():
            raise EgressDeniedError("Empty or non-string URL.")
        parts = urlsplit(url.strip())
        if not parts.hostname:
            raise EgressDeniedError(f"URL {url!r} has no host.")
        host = normalize_host(parts.hostname)
        if is_brokerage_host(host):  # loudest error first, even for disguised URLs
            raise BrokerageForbiddenError(f"Refusing brokerage URL {url!r}.")
        if parts.scheme.lower() != "https":
            raise EgressDeniedError(f"URL {url!r} must use https.")
        if parts.username or parts.password:
            raise EgressDeniedError("Credentials embedded in URLs are not allowed.")
        if parts.port not in (None, 443):
            raise EgressDeniedError(f"URL {url!r} uses a non-standard port.")
        return self.check(host)
