"""Process-wide network guard enforcing the egress policy (defence in depth).

Installs a Python audit hook that inspects every DNS lookup, socket connect and
http.client connection (including the CONNECT tunnel target behind a proxy) made by any
library in this process. Destinations outside the egress policy raise before a packet is
sent. Audit hooks cannot be removed once installed.

Limits (see docs/SAFETY.md): libraries that resolve and connect in C (e.g. uvloop) do not
raise audit events; the startup self-check refuses non-standard event loops for that reason.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterable
from typing import Any

from tradinglab.safety.endpoints import (
    BrokerageForbiddenError,
    EgressDeniedError,
    EgressPolicy,
)

log = logging.getLogger(__name__)

_lock = threading.Lock()
_installed = False
_policy = EgressPolicy()
_blocked_attempts: list[str] = []

_EVENTS = frozenset(
    {"socket.getaddrinfo", "socket.gethostbyname", "socket.connect", "http.client.connect"}
)


def _extract_hosts(event: str, args: tuple[Any, ...]) -> list[str]:
    hosts: list[str] = []
    try:
        if event == "http.client.connect":  # (connection, host, port)
            conn, host = args[0], args[1]
            for h in (host, getattr(conn, "_tunnel_host", None)):
                if isinstance(h, str | bytes):
                    hosts.append(h.decode("ascii", "ignore") if isinstance(h, bytes) else h)
        elif event in ("socket.getaddrinfo", "socket.gethostbyname"):
            h = args[0]
            if isinstance(h, bytes):
                h = h.decode("ascii", "ignore")
            if isinstance(h, str):
                hosts.append(h)
        elif event == "socket.connect":  # (socket, address)
            address = args[1]
            if isinstance(address, tuple) and address and isinstance(address[0], str):
                hosts.append(address[0])
    except (IndexError, TypeError):
        pass
    return hosts


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in _EVENTS:
        return
    for host in _extract_hosts(event, args):
        if event == "socket.getaddrinfo" and host in ("", None):
            continue
        try:
            _policy.check(host)
        except (BrokerageForbiddenError, EgressDeniedError) as exc:
            _blocked_attempts.append(host)
            log.critical("NETWORK GUARD blocked %s to %r: %s", event, host, exc)
            raise


def install_network_guard(extra_allowed: Iterable[str] = ()) -> None:
    """Install the audit hook once per process and add hosts to the egress allowlist."""
    global _installed
    with _lock:
        extra = list(extra_allowed)
        if extra:
            _policy.allow(extra)
        if not _installed:
            sys.addaudithook(_audit_hook)
            _installed = True
            log.info("Egress guard installed; allowed hosts: %s",
                     ", ".join(sorted(_policy.allowed_hosts)))


def freeze_egress() -> None:
    """Prevent any further widening of the allowlist (production calls this at startup)."""
    _policy.freeze()


def policy() -> EgressPolicy:
    return _policy


def is_installed() -> bool:
    return _installed


def blocked_attempts() -> list[str]:
    return list(_blocked_attempts)
