"""Startup self-check. Every process (API server, scheduled job) runs this first.

If any check fails, the process exits non-zero before touching the broker. The result is
also exposed (without secrets) on the dashboard's system-status panel.
"""

from __future__ import annotations

import asyncio
import os
import socket
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tradinglab.config import ConfigError, Settings, load_settings
from tradinglab.safety.endpoints import (
    DENIED_LIVE_HOSTS,
    PAPER_TRADING_URL,
    LiveTradingForbiddenError,
    validate_trading_url,
)
from tradinglab.safety.network_guard import install_network_guard, is_installed


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
    """True if asyncio uses its stock loop. uvloop/libuv resolve and connect in C, which
    bypasses Python audit hooks and therefore the network guard."""
    policy = asyncio.get_event_loop_policy()
    return type(policy).__module__.startswith("asyncio")


def run_selfcheck(
    env: Mapping[str, str] | None = None,
    *,
    system_proxies: Callable[[], Mapping[str, str]] = urllib.request.getproxies,
) -> SelfCheckReport:
    env = os.environ if env is None else env
    checks: list[tuple[str, bool, str]] = []

    install_network_guard()
    checks.append(("network_guard_installed", is_installed(), "audit hook active"))

    try:
        settings = load_settings(env)
    except ConfigError as exc:
        raise SafetyCheckFailedError(f"Configuration refused: {exc}") from exc
    checks.append(("config_paper_mode", settings.trading_mode == "paper", "TRADING_MODE=paper"))

    # requests (used by alpaca-py) also honours OS-level proxy settings on macOS/Windows,
    # which do not appear in the environment. Any proxy hides destinations from the guard.
    found = {k: v for k, v in dict(system_proxies()).items() if k.lower() != "no" and v}
    checks.append(("no_proxies", not found, ", ".join(sorted(found)) or "none"))
    checks.append(("standard_asyncio_loop", standard_event_loop(),
                   type(asyncio.get_event_loop_policy()).__module__))

    url_ok = validate_trading_url(settings.trading_base_url) == PAPER_TRADING_URL
    checks.append(("trading_url_is_paper", url_ok, PAPER_TRADING_URL))

    # Prove, live in this process, that the guard blocks every known live host.
    blocked_all = True
    for host in sorted(DENIED_LIVE_HOSTS):
        try:
            socket.getaddrinfo(host, 443)
            blocked_all = False
        except LiveTradingForbiddenError:
            pass
        except OSError:
            # DNS failure means the guard did NOT intercept first - treat as failure.
            blocked_all = False
    checks.append(("live_hosts_blocked", blocked_all, ", ".join(sorted(DENIED_LIVE_HOSTS))))

    checks.append(
        (
            "order_submission",
            True,
            "ENABLED" if settings.order_submission_enabled else "disabled (dry run)",
        )
    )

    report = SelfCheckReport(settings, tuple(checks))
    if not report.ok:
        failed = [name for name, passed, _ in checks if not passed]
        raise SafetyCheckFailedError("Safety self-check failed: " + ", ".join(failed))
    return report
