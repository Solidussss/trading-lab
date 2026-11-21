"""Phone alerts: the scout's best alerts posted to a Discord channel through a webhook.

A Discord webhook is a secret web address that accepts messages for one channel and nothing
else: it cannot read the channel, see its members or act as anyone. The address comes from the
DISCORD_WEBHOOK_URL setting (config.py checks its shape) and is never logged or shown.

What gets sent, so the channel stays worth reading:
  * a coin alert only if it passed the scam check and scores at least PUSH_MIN
    (whale, proven-wallet and followed-trader alerts score higher, so they clear it more
    easily);
  * a trending subject at most once every two hours;
  * never more than PUSH_PER_HOUR messages in an hour;
  * once a day, a short recap of how the previous day's alerts actually did.

Coin names are written by whoever made the coin, so they are stripped of anything Discord
would treat as formatting, a link or a mention, and the message tells Discord to ping nobody.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any

from tradinglab.config import Secret
from tradinglab.safety.network_guard import install_network_guard, policy

log = logging.getLogger(__name__)

PUSH_MIN = 55.0  # the least a checked coin alert must score to be sent
PUSH_PER_HOUR = 6
PUSH_MAX_AGE_MIN = 30.0  # an alert that could not be sent by then is no longer news
THEME_EVERY_MIN = 120.0
MAX_CHARS = 1900  # Discord refuses messages over 2,000 characters
LINK_HOSTS = ("https://dexscreener.com/", "https://solscan.io/")

KIND = {"early": "BEFORE MIGRATION", "migrated": "JUST MIGRATED", "theme": "TRENDING SUBJECT",
        "whales": "WHALES BUYING", "smart": "PROVEN WALLET", "trader": "PROVEN TRADER BOUGHT",
        "crowd": "TOP TRADERS BUYING"}

HttpPost = Callable[[str, bytes, dict[str, str], float], tuple]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # a redirect would carry the secret address to another host


def _urllib_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(4096)
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read(4096) if exc.fp else b"")


def clean(text: Any, limit: int = 220) -> str:
    """Text from the outside world, made safe to put in a message: no formatting, no links,
    no mentions, one line."""
    out = re.sub(r"[`*_~|<>\[\]()@#\\]", "", str(text))
    out = re.sub(r"https?:\S+|discord\.gg\S*", "", out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()[:limit]


def worth_sending(alert: dict) -> bool:
    """Is this alert good enough for the phone? (Subjects are rationed by the caller.)"""
    if alert.get("kind") == "theme":
        return float(alert.get("score", 0)) >= PUSH_MIN - 5
    safety = alert.get("safety") or {}
    return bool(safety.get("checked")) and not safety.get("dangers") \
        and float(alert.get("score", 0)) >= PUSH_MIN


def alert_text(alert: dict) -> str:
    kind = str(alert.get("kind", ""))
    safety = alert.get("safety") or {}
    tail = ""
    if kind != "theme":
        warn = "; ".join(clean(w, 80) for w in safety.get("warnings") or [])
        tail = f" · caution: {warn}" if warn else " · passed scam check"
    lines = [f"**{KIND.get(kind, kind.upper())} · {clean(alert.get('title', '?'), 40)}** · "
             f"score {float(alert.get('score', 0)):.0f}{tail}", clean(alert.get("text", ""))]
    link = next((u for u in (alert.get("links") or {}).values()
                 if isinstance(u, str) and u.startswith(LINK_HOSTS) and " " not in u), "")
    if link:
        lines.append(f"<{link}>")  # angle brackets: a plain link, no preview card
    return "\n".join(lines)


def digest_text(stats: dict, alerts: list, kept_out: int, day: str) -> str:
    """The daily recap: how yesterday's alerts did. `stats` is scout.summary()."""
    n = sum(v["n"] for v in stats.values())
    ran = sum(v["ran"] for v in stats.values())
    lines = [f"**Daily recap · {clean(day, 30)}**",
             f"{n} coin alerts in the last 24 hours, {ran} went up 20% or more at some point. "
             f"{kept_out} coins kept out by the scam check so far."]
    for kind, v in sorted(stats.items()):
        lines.append(f"{KIND.get(kind, kind).lower()}: {v['n']} alerts, {v['ran']} ran, "
                     f"now {v['now']:+.0%} on average")
    scored = [a for a in alerts if a.get("kind") != "theme" and a.get("price")]
    if scored:
        best = max(scored, key=lambda a: a.get("peak", a["price"]) / a["price"])
        lines.append(f"Best: {clean(best.get('title', '?'), 40)}, up "
                     f"{best.get('peak', best['price']) / best['price'] - 1:.0%} at its high "
                     f"({KIND.get(best.get('kind', ''), '').lower()}).")
    lines.append("Heads-ups from a pretend-money lab, not tips. Most new coins fall.")
    return "\n".join(lines)


class DiscordNotifier:
    def __init__(self, webhook: Secret, *, http_post: HttpPost = _urllib_post, timeout: float = 10.0,
                 clock: Callable[[], float] = time.time,
                 check_url: Callable[[str], Any] | None = None) -> None:
        install_network_guard()
        self._webhook, self._http_post, self.timeout, self._clock = webhook, http_post, timeout, clock
        # The process-wide egress policy: Discord's host is on it only when a webhook was
        # configured at startup (config.py), so nothing else can make this class post.
        self._check_url = check_url or (lambda url: policy().check_url(url))
        self._sent: deque[float] = deque()
        self._quiet_until = 0.0
        self.last_ok: float | None = None
        self.last_error = ""

    def room(self) -> int:
        """Messages that may still be sent this hour."""
        now = self._clock()
        while self._sent and now - self._sent[0] >= 3600.0:
            self._sent.popleft()
        return 0 if now < self._quiet_until else max(PUSH_PER_HOUR - len(self._sent), 0)

    def send(self, text: str) -> bool:
        """Post one message. False when it was not sent (rationed, or Discord said no); the
        reason is kept in last_error, without the address."""
        if not text.strip() or self.room() <= 0:
            return False
        url = self._webhook.get()
        body = json.dumps({"content": text[:MAX_CHARS], "allowed_mentions": {"parse": []},
                           "flags": 4}).encode()  # ping nobody; no link preview cards
        now = self._clock()
        try:
            self._check_url(url)
            status, reply = self._http_post(url, body, {"Content-Type": "application/json",
                                                        "User-Agent": "trading-lab-paper-bot"},
                                            self.timeout)
        except Exception as exc:  # noqa: BLE001 - a failed alert must never stop the scout
            self.last_error = type(exc).__name__  # never str(exc): it may contain the address
            log.warning("discord: message not sent (%s)", self.last_error)
            return False
        if status in (200, 204):
            self._sent.append(now)
            self.last_ok, self.last_error = now, ""
            return True
        if status == 429:  # told to slow down: wait as long as it says, up to ten minutes
            try:
                wait = float(json.loads(reply or b"{}").get("retry_after", 60))
            except (ValueError, TypeError, AttributeError):
                wait = 60.0
            self._quiet_until = now + min(max(wait, 1.0), 600.0)
        self.last_error = f"HTTP {status}"
        log.warning("discord: message not sent (%s)", self.last_error)
        return False

    def status(self) -> dict:
        return {"on": True, "last_ok": self.last_ok, "last_error": self.last_error}
