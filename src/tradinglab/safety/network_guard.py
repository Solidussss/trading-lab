"""Process-wide network guard (defence in depth).

Installs a Python audit hook that inspects every DNS lookup and socket connect made by
*any* library in this process - alpaca-py, requests, httpx, websockets, urllib, etc.
If the target is a live Alpaca host, or an Alpaca host not on the allowlist, the call
raises before a packet leaves the machine.

Audit hooks cannot be removed once installed (by design), so a bug elsewhere, a library
default, or an injected URL cannot route around the guard at runtime.

This complements, and does not replace, the URL validation in the broker factory.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any

from tradinglab.safety.endpoints import (
    EndpointNotAllowedError,
    LiveTradingForbiddenError,
    check_alpaca_host,
    is_alpaca_host,
)

log = logging.getLogger(__name__)

_lock = threading.Lock()
_installed = False
_blocked_attempts: list[str] = []


_EVENTS = frozenset(
    {"socket.getaddrinfo", "socket.gethostbyname", "socket.connect", "http.client.connect"}
)


def _extract_hosts(event: str, args: tuple[Any, ...]) -> list[str]:
    """Hosts referenced by an audit event. For http.client this includes the CONNECT
    tunnel target, so a request routed through an HTTPS proxy is still inspected."""
    hosts: list[str] = []
    if event == "http.client.connect":
        # args: (connection, host, port)
        try:
            conn, host = args[0], args[1]
        except IndexError:
            return hosts
        for h in (host, getattr(conn, "_tunnel_host", None)):
            if isinstance(h, str):
                hosts.append(h)
        return hosts
    single = _extract_host(event, args)
    return [single] if single else hosts


def _extract_host(event: str, args: tuple[Any, ...]) -> str | None:
    try:
        if event == "socket.getaddrinfo":
            # args: (host, port, family, type, proto, flags)
            host = args[0]
        elif event == "socket.gethostbyname":
            host = args[0]
        elif event == "socket.connect":
            # args: (socket, address); address is (host, port[, ...]) for INET sockets
            address = args[1]
            host = address[0] if isinstance(address, tuple) and address else None
        else:
            return None
    except (IndexError, TypeError):
        return None
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    return host if isinstance(host, str) else None


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in _EVENTS:
        return
    for host in _extract_hosts(event, args):
        try:
            # is_alpaca_host IDNA-normalises first; unconvertible non-ASCII hosts raise.
            if not is_alpaca_host(host):
                continue
            check_alpaca_host(host)
        except (LiveTradingForbiddenError, EndpointNotAllowedError) as exc:
            _blocked_attempts.append(host)
            log.critical("NETWORK GUARD blocked %s to %r: %s", event, host, exc)
            raise


def install_network_guard() -> None:
    """Install the audit hook once per process. Safe to call repeatedly."""
    global _installed
    with _lock:
        if _installed:
            return
        sys.addaudithook(_audit_hook)
        _installed = True
        log.info("Paper-only network guard installed.")


def is_installed() -> bool:
    return _installed


def blocked_attempts() -> list[str]:
    """Hosts the guard has blocked in this process (for health checks and tests)."""
    return list(_blocked_attempts)
