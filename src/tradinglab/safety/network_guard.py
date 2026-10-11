"""Process-wide network guard enforcing the egress policy (defence in depth).

Two cooperating mechanisms:

  1. A Python audit hook inspects every DNS lookup (socket.getaddrinfo/gethostbyname),
     socket connect/sendto/sendmsg, and http.client connection (including the CONNECT
     tunnel target behind a proxy). Disallowed destinations raise before any packet is sent.
  2. socket.getaddrinfo is wrapped so that the IP addresses returned for an *allowlisted
     hostname* are remembered. A later connect to one of those IPs is permitted; a connect
     to any other public IP is refused. (Without this, connections to api.tiingo.com would
     fail, because sockets connect to the resolved IP, not the name.)

Unparseable addresses on internet sockets are refused rather than allowed. Unix-domain
sockets (local IPC) are not network egress and are permitted.

Limits (docs/SAFETY.md): code that does DNS and sockets in C without going through
Python's socket module (uvloop, libpq) is invisible to the hook. The self-check refuses
uvloop; the PostgreSQL host is validated in configuration instead.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

from tradinglab.safety.endpoints import (
    BrokerageForbiddenError,
    EgressDeniedError,
    EgressPolicy,
    normalize_host,
)

log = logging.getLogger(__name__)

_lock = threading.Lock()
_installed = False
_policy = EgressPolicy()
_blocked_attempts: list[str] = []
_resolved: OrderedDict[str, str] = OrderedDict()  # ip -> allowlisted hostname it came from
_MAX_RESOLVED = 512
_real_getaddrinfo = socket.getaddrinfo

_EVENTS = frozenset({"socket.getaddrinfo", "socket.gethostbyname", "socket.connect",
                     "socket.sendto", "socket.sendmsg", "http.client.connect"})
_INET_FAMILIES = {socket.AF_INET, socket.AF_INET6}


class _Unparseable(Exception):
    pass


def _as_host(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", "strict")
    if isinstance(value, str):
        return value
    raise _Unparseable(repr(type(value)))


def _hosts_for(event: str, args: tuple[Any, ...]) -> list[str]:
    """Hosts an audit event refers to. Raises _Unparseable if it cannot tell."""
    if event == "http.client.connect":  # (connection, host, port)
        conn = args[0]
        hosts = [_as_host(args[1])]
        tunnel = getattr(conn, "_tunnel_host", None)
        if tunnel is not None:
            hosts.append(_as_host(tunnel))
        return hosts
    if event in ("socket.getaddrinfo", "socket.gethostbyname"):
        host = args[0]
        if host is None:  # wildcard bind lookups
            return []
        return [_as_host(host)]
    # socket.connect / sendto / sendmsg: (socket, address)
    sock, address = args[0], args[1]
    if address is None:  # sendto/sendmsg on an already-connected socket
        return []
    family = getattr(sock, "family", None)
    if family is not None and family not in _INET_FAMILIES:
        return []  # AF_UNIX etc.: local IPC, not network egress
    if isinstance(address, tuple) and address:
        return [_as_host(address[0])]
    raise _Unparseable(repr(address))


def _check_host(host: str) -> None:
    h = normalize_host(host)
    if h in _resolved:
        return  # an IP we resolved from an allowlisted name
    _policy.check(h)


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in _EVENTS:
        return
    try:
        hosts = _hosts_for(event, args)
    except (_Unparseable, UnicodeError, IndexError) as exc:
        _blocked_attempts.append(f"<unparseable {event}>")
        raise EgressDeniedError(f"Unparseable network destination for {event}; refused.") from exc
    for host in hosts:
        try:
            _check_host(host)
        except (BrokerageForbiddenError, EgressDeniedError) as exc:
            _blocked_attempts.append(host)
            log.critical("NETWORK GUARD blocked %s to %r: %s", event, host, exc)
            raise


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    """Vet the name, resolve, and remember the IPs of allowlisted names.

    The name is checked here explicitly as well as by the audit hook, so the policy holds
    even if the underlying resolver is replaced or does not raise audit events."""
    if host is not None:
        _audit_hook("socket.getaddrinfo", (host, port))
    results = _real_getaddrinfo(host, port, *args, **kwargs)
    try:
        name = normalize_host(_as_host(host)) if host is not None else ""
    except (_Unparseable, UnicodeError, EgressDeniedError):
        return results
    if name and name not in ("localhost",) and not _is_ip_literal(name):
        with _lock:
            for *_, sockaddr in results:
                ip = normalize_host(str(sockaddr[0]).split("%", 1)[0])
                _resolved[ip] = name
                _resolved.move_to_end(ip)
            while len(_resolved) > _MAX_RESOLVED:
                _resolved.popitem(last=False)
    return results


def _is_ip_literal(h: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def install_network_guard(extra_allowed: Iterable[str] = ()) -> None:
    """Install the hook and resolver wrapper once per process; add hosts to the allowlist."""
    global _installed
    with _lock:
        extra = list(extra_allowed)
        if extra:
            _policy.allow(extra)
        if not _installed:
            sys.addaudithook(_audit_hook)
            socket.getaddrinfo = _guarded_getaddrinfo
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


def resolved_ips() -> dict[str, str]:
    return dict(_resolved)
