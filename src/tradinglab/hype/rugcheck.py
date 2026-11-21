"""Scam check: what RugCheck's public report says about a Solana token (read-only).

Provider facts (checked 2026-11-21 against live tokens; no key, no account):
  * GET https://api.rugcheck.xyz/v1/tokens/<mint>/report/summary returns
    {"score_normalised": 0-100 (higher is riskier), "risks": [{"name", "description",
    "level": "danger" | "warn", "score"}], "lpLockedPct": ...}.
  * Typical risks: the creator has rugged tokens before, one or a few wallets hold most of the
    supply, the creator can still mint more or freeze accounts, a transfer fee, unlocked
    liquidity. A long-established token such as USDC comes back with no risks and a score of 1.
  * It is an analysis service. It has no accounts, wallets or order entry and cannot trade.

What a clean report does and does not mean: it rules out the scams that are visible in the
token's own set-up and its holders. It cannot see intent. A coin can pass every check and
still be dumped by the people who launched it an hour later.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tradinglab.hype.dexscreener import MarketDataError, _urllib_get
from tradinglab.safety.endpoints import RUG_REPORT_URL
from tradinglab.safety.network_guard import install_network_guard, policy

MAX_BYTES = 512 * 1024
# On pump fun's starting curve the pool is small by design and cannot be pulled, so RugCheck's
# liquidity warnings say nothing there. They are kept for coins in a normal pool.
_POOL_WORDS = ("liquidity", "lp ", "lp providers", "bonding curve")


@dataclass(frozen=True)
class Verdict:
    score: int  # 0-100, higher is riskier
    dangers: tuple[str, ...]  # reasons to stay away
    warnings: tuple[str, ...]  # reasons for caution

    @property
    def bad(self) -> bool:
        return bool(self.dangers)

    def to_json(self) -> dict:
        return {"checked": True, "score": self.score, "dangers": list(self.dangers[:3]),
                "warnings": list(self.warnings[:3])}


def _text(risk: dict) -> str:
    words = str(risk.get("description") or risk.get("name") or "unnamed risk").strip().rstrip(".")
    value = str(risk.get("value") or "").strip()
    return (words[:1].lower() + words[1:] + (f" ({value})" if value else ""))[:160]


def judge(payload: Any, pre_migration: bool = False) -> Verdict:
    """RugCheck's summary -> a verdict. Anything that isn't a summary is an error, never a pass."""
    if not isinstance(payload, dict) or not isinstance(payload.get("risks"), list):
        raise MarketDataError("RugCheck returned no risk list")
    dangers, warnings = [], []
    for r in payload["risks"]:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").lower()
        if pre_migration and any(w in name + " " for w in _POOL_WORDS):
            continue
        (dangers if r.get("level") == "danger" else warnings).append(_text(r))
    try:
        score = int(round(float(payload.get("score_normalised") or 0)))
    except (TypeError, ValueError):
        score = 0
    return Verdict(min(max(score, 0), 100), tuple(dangers), tuple(warnings))


class RugCheckClient:
    def __init__(self, *, http_get: Callable[..., tuple] = _urllib_get, timeout: float = 10.0) -> None:
        install_network_guard()
        self._http_get, self.timeout = http_get, timeout

    def summary(self, mint: str) -> dict:
        """The raw summary for a token. One attempt: the shared feed decides when to ask again."""
        if not mint.isalnum() or len(mint) > 64:
            raise MarketDataError("not a token address")
        url = f"{RUG_REPORT_URL}/v1/tokens/{mint}/report/summary"
        policy().check_url(url)
        try:
            result = self._http_get(url, {"Accept": "application/json",
                                          "User-Agent": "trading-lab-paper-bot"}, self.timeout)
        except (OSError, TimeoutError) as exc:
            raise MarketDataError(f"RugCheck unavailable: {type(exc).__name__}") from exc
        status, body = result[0], result[1]
        if status != 200:
            raise MarketDataError(f"RugCheck unavailable: HTTP {status}")
        if len(body) > MAX_BYTES:
            raise MarketDataError("RugCheck response too large")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise MarketDataError("RugCheck returned malformed JSON") from exc

    def verdict(self, mint: str, pre_migration: bool = False) -> Verdict:
        return judge(self.summary(mint), pre_migration)
