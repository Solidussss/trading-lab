"""Strict hostname allowlist for every outbound broker/data connection.

This module is the single source of truth for which Alpaca hosts the lab may talk to.
Anything not explicitly allowed is refused - including hosts we have never heard of.

Allowed:
  * paper-api.alpaca.markets   - paper trading REST API (orders, positions, account, clock)
  * data.alpaca.markets        - market data REST API (read-only; same host for all accounts)
  * stream.data.alpaca.markets - market data websocket (read-only)

Explicitly denied (checked first, so a misconfigured allowlist can never let them through):
  * api.alpaca.markets         - LIVE trading
  * broker-api.alpaca.markets  - Broker API (live)
  * any other *.alpaca.markets host not on the allowlist
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

PAPER_TRADING_HOST: Final = "paper-api.alpaca.markets"
PAPER_TRADING_URL: Final = f"https://{PAPER_TRADING_HOST}"

MARKET_DATA_HOST: Final = "data.alpaca.markets"
MARKET_DATA_STREAM_HOST: Final = "stream.data.alpaca.markets"

ALLOWED_TRADING_HOSTS: Final[frozenset[str]] = frozenset({PAPER_TRADING_HOST})
ALLOWED_DATA_HOSTS: Final[frozenset[str]] = frozenset({MARKET_DATA_HOST, MARKET_DATA_STREAM_HOST})
ALLOWED_ALPACA_HOSTS: Final[frozenset[str]] = ALLOWED_TRADING_HOSTS | ALLOWED_DATA_HOSTS

# Hosts that can place real-money orders. Never reachable from this process.
DENIED_LIVE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "api.alpaca.markets",
        "broker-api.alpaca.markets",
        "live-api.alpaca.markets",
    }
)

ALPACA_DOMAIN_SUFFIX: Final = ".alpaca.markets"


class LiveTradingForbiddenError(RuntimeError):
    """Raised whenever anything attempts to reach a live (real-money) trading endpoint."""


class EndpointNotAllowedError(RuntimeError):
    """Raised for any broker/data URL that is not on the strict allowlist."""


def normalize_host(host: str) -> str:
    """Lower-case, strip whitespace, a trailing dot, and any port."""
    h = host.strip().lower()
    if h.startswith("[") and "]" in h:  # IPv6 literal
        return h[1 : h.index("]")]
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def is_live_host(host: str) -> bool:
    h = normalize_host(host)
    return h in DENIED_LIVE_HOSTS


def is_alpaca_host(host: str) -> bool:
    h = normalize_host(host)
    return h == ALPACA_DOMAIN_SUFFIX[1:] or h.endswith(ALPACA_DOMAIN_SUFFIX)


def check_alpaca_host(host: str) -> str:
    """Return the normalized host if it is an allowed Alpaca host; raise otherwise.

    Live hosts raise LiveTradingForbiddenError. Unknown *.alpaca.markets hosts raise
    EndpointNotAllowedError (fail closed: Alpaca could add a new live hostname).
    """
    h = normalize_host(host)
    if h in DENIED_LIVE_HOSTS:
        raise LiveTradingForbiddenError(f"Refusing to connect to live trading host {h!r}.")
    if h not in ALLOWED_ALPACA_HOSTS:
        raise EndpointNotAllowedError(f"Host {h!r} is not on the paper-only allowlist.")
    return h


def validate_trading_url(url: str) -> str:
    """Validate a trading base URL. Only https://paper-api.alpaca.markets is accepted."""
    host = _validated_https_host(url)
    if host in DENIED_LIVE_HOSTS:
        raise LiveTradingForbiddenError(f"Refusing live trading URL {url!r}.")
    if host not in ALLOWED_TRADING_HOSTS:
        raise EndpointNotAllowedError(
            f"Trading URL {url!r} is not allowed. Only {PAPER_TRADING_URL} is permitted."
        )
    return f"https://{host}"


def validate_data_url(url: str) -> str:
    host = _validated_https_host(url, schemes=("https", "wss"))
    check_alpaca_host(host)
    if host not in ALLOWED_DATA_HOSTS:
        raise EndpointNotAllowedError(f"Data URL {url!r} is not an allowed market-data host.")
    return host


def _validated_https_host(url: str, schemes: tuple[str, ...] = ("https",)) -> str:
    if not isinstance(url, str) or not url.strip():
        raise EndpointNotAllowedError("Empty or non-string URL.")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in schemes:
        raise EndpointNotAllowedError(f"URL {url!r} must use one of {schemes}.")
    if not parts.hostname:
        raise EndpointNotAllowedError(f"URL {url!r} has no host.")
    # Classify live hosts first so the louder error is raised even for disguised URLs
    # such as https://paper-api.alpaca.markets@api.alpaca.markets.
    if normalize_host(parts.hostname) in DENIED_LIVE_HOSTS:
        raise LiveTradingForbiddenError(f"Refusing live trading URL {url!r}.")
    if parts.username or parts.password:
        raise EndpointNotAllowedError("Credentials embedded in URLs are not allowed.")
    if parts.port not in (None, 443):
        raise EndpointNotAllowedError(f"URL {url!r} uses a non-standard port.")
    return normalize_host(parts.hostname)
