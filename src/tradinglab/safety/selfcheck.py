"""Startup self-check. Every process (dashboard service, scheduled job) runs this first.

If any check fails, the process exits non-zero before doing anything else. The report
(without secrets) is shown on the dashboard's system-status panel.
"""

from __future__ import annotations

import asyncio
import os
import socket
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tradinglab.config import ConfigError, Settings, load_settings
from tradinglab.safety.endpoints import BrokerageForbiddenError, EgressDeniedError
from tradinglab.safety.network_guard import (
    freeze_egress,
    install_network_guard,
    is_installed,
    policy,
)

# Probed at every startup: each must be refused by the guard before any DNS happens.
PROBE_BROKERAGE_HOSTS = ("api.alpaca.markets", "api.ibkr.com", "api01.iq.questrade.com",
                         "trade-service.wealthsimple.com")
PROBE_UNKNOWN_HOST = "example.com"


class SafetyCheckFailedError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelfCheckReport:
    settings: Settings
    checks: tuple[tuple[str, bool, str], ...]

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.checks)


def standard_event_loop() -> bool:
    """uvloop/libuv resolve and connect in C, bypassing audit hooks (and the guard)."""
    return type(asyncio.get_event_loop_policy()).__module__.startswith("asyncio")


def _blocked(host: str, expected: type[Exception]) -> bool:
    try:
        socket.getaddrinfo(host, 443)
    except expected:
        return True
    except OSError:
        return False  # DNS ran: the guard did not intercept first
    return False


def run_selfcheck(
    env: Mapping[str, str] | None = None,
    *,
    system_proxies: Callable[[], Mapping[str, str]] = urllib.request.getproxies,
    freeze: bool = True,
) -> SelfCheckReport:
    env = os.environ if env is None else env
    try:
        settings = load_settings(env)
    except ConfigError as exc:
        raise SafetyCheckFailedError(f"Configuration refused: {exc}") from exc

    install_network_guard(settings.egress_hosts)
    if freeze:
        freeze_egress()

    checks: list[tuple[str, bool, str]] = [
        ("network_guard_installed", is_installed(), "audit hook active"),
        ("simulation_only", settings.trading_mode == "simulated", "TRADING_MODE=simulated"),
        ("egress_allowlist", True, ", ".join(sorted(policy().allowed_hosts))),
    ]
    brokers_blocked = all(_blocked(h, BrokerageForbiddenError) for h in PROBE_BROKERAGE_HOSTS)
    checks.append(("brokerage_hosts_blocked", brokers_blocked, ", ".join(PROBE_BROKERAGE_HOSTS)))
    checks.append(("unknown_hosts_blocked", _blocked(PROBE_UNKNOWN_HOST, EgressDeniedError),
                   PROBE_UNKNOWN_HOST))

    found = {k: v for k, v in dict(system_proxies()).items() if k.lower() != "no" and v}
    checks.append(("no_proxies", not found, ", ".join(sorted(found)) or "none"))
    checks.append(("standard_asyncio_loop", standard_event_loop(),
                   type(asyncio.get_event_loop_policy()).__module__))

    report = SelfCheckReport(settings, tuple(checks))
    if not report.ok:
        failed = [name for name, passed, _ in checks if not passed]
        raise SafetyCheckFailedError("Safety self-check failed: " + ", ".join(failed))
    return report
