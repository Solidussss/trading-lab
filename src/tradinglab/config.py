"""Fail-closed configuration.

Every setting is read from environment variables (Railway service variables in production,
a git-ignored .env locally). If anything is missing, malformed, out of range, or ambiguous,
`load_settings()` raises ConfigError and the process must not start.

Secrets are wrapped in `Secret`, whose repr/str never reveal the value, so they cannot leak
through logs, tracebacks, or the dashboard.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final

from tradinglab.safety.endpoints import PAPER_TRADING_URL, validate_trading_url


class ConfigError(RuntimeError):
    """Configuration is missing, invalid, or ambiguous. The app must not start."""


# --- Hard ceilings: configuration can tighten these, never loosen them. -------------------
HARD_MAX_RISK_PER_TRADE: Final = Decimal("0.02")  # 2% of equity
HARD_MAX_EXPOSURE: Final = Decimal("1.00")  # 100% of capital => no leverage, ever
HARD_MAX_OPEN_POSITIONS: Final = 10
HARD_MAX_INTRADAY_HALT: Final = Decimal("0.10")
HARD_MAX_PEAK_DD_HALT: Final = Decimal("0.30")
HARD_MAX_CAPITAL_BASE: Final = Decimal("1000000")

ALLOWED_APP_ENVS: Final = frozenset({"development", "test", "production"})
SYMBOL_RE: Final = re.compile(r"^[A-Z]{1,5}$")

# Environment variables that make the credential source ambiguous. If any is present the
# app refuses to start instead of guessing which keys/URL alpaca-py might pick up.
AMBIGUOUS_ENV_VARS: Final = (
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "APCA_API_BASE_URL",
    "APCA_API_DATA_URL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_API_SECRET",
    "ALPACA_BASE_URL",
    "ALPACA_ENDPOINT",
)
PROXY_ENV_VARS: Final = frozenset({"HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"})
_LIVE_ENV_NAME_RE: Final = re.compile(r"(ALPACA|APCA).*LIVE|LIVE.*(ALPACA|APCA)")
_TRUE: Final = frozenset({"true", "1", "yes"})
_FALSE: Final = frozenset({"false", "0", "no", ""})


class Secret:
    """Holds a secret string; never reveals it via str/repr/format."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('**********')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:  # pragma: no cover - convenience only
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:  # pragma: no cover
        return hash(self._value)


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_pct: Decimal = Decimal("0.005")
    max_exposure_pct: Decimal = Decimal("0.30")
    max_open_positions: int = 3
    intraday_loss_halt_pct: Decimal = Decimal("0.03")
    peak_drawdown_halt_pct: Decimal = Decimal("0.10")

    def __post_init__(self) -> None:
        _range("RISK_PER_TRADE_PCT", self.risk_per_trade_pct, HARD_MAX_RISK_PER_TRADE)
        _range("MAX_PORTFOLIO_EXPOSURE_PCT", self.max_exposure_pct, HARD_MAX_EXPOSURE)
        _range("INTRADAY_LOSS_HALT_PCT", self.intraday_loss_halt_pct, HARD_MAX_INTRADAY_HALT)
        _range("PEAK_DRAWDOWN_HALT_PCT", self.peak_drawdown_halt_pct, HARD_MAX_PEAK_DD_HALT)
        if not (1 <= self.max_open_positions <= HARD_MAX_OPEN_POSITIONS):
            raise ConfigError(
                f"MAX_OPEN_POSITIONS must be 1..{HARD_MAX_OPEN_POSITIONS}, "
                f"got {self.max_open_positions}."
            )


@dataclass(frozen=True)
class Settings:
    app_env: str
    alpaca_api_key: Secret = field(repr=False)
    alpaca_secret_key: Secret = field(repr=False)
    order_submission_enabled: bool
    capital_base_usd: Decimal
    symbols: tuple[str, ...]
    risk: RiskLimits
    trading_mode: str = "paper"
    trading_base_url: str = PAPER_TRADING_URL

    def __post_init__(self) -> None:
        # Belt and braces: even a hand-constructed Settings cannot describe live trading.
        if self.trading_mode != "paper":
            raise ConfigError("trading_mode must be 'paper'.")
        validate_trading_url(self.trading_base_url)


def load_settings(env: Mapping[str, str]) -> Settings:
    """Build Settings from an environment mapping (pass `os.environ` in production)."""
    _reject_ambiguous(env)

    app_env = _required(env, "APP_ENV").lower()
    if app_env not in ALLOWED_APP_ENVS:
        raise ConfigError(f"APP_ENV must be one of {sorted(ALLOWED_APP_ENVS)}.")
    if app_env != "test":
        # An HTTP(S) proxy hides the real destination from the socket-level network guard
        # (libraries such as requests/urllib3 connect only to the proxy). Fail closed.
        proxies = sorted(n for n in env if n.upper() in PROXY_ENV_VARS and env[n].strip())
        if proxies:
            raise ConfigError(
                "Proxy variables " + ", ".join(proxies) + " are not allowed outside tests: "
                "they would hide broker destinations from the paper-only network guard."
            )

    mode = _required(env, "TRADING_MODE")
    if mode != "paper":
        # Exact match only: "Paper", "paper ", "live", "both" all fail.
        raise ConfigError("TRADING_MODE must be exactly 'paper'. Live trading is not supported.")

    key = _required(env, "ALPACA_PAPER_API_KEY")
    secret = _required(env, "ALPACA_PAPER_SECRET_KEY")
    _validate_paper_key_id(key)
    if len(secret) < 20 or secret.strip() != secret:
        raise ConfigError("ALPACA_PAPER_SECRET_KEY looks malformed.")
    if key == secret:
        raise ConfigError("API key and secret must differ.")

    risk = RiskLimits(
        risk_per_trade_pct=_decimal(env, "RISK_PER_TRADE_PCT", "0.005"),
        max_exposure_pct=_decimal(env, "MAX_PORTFOLIO_EXPOSURE_PCT", "0.30"),
        max_open_positions=_int(env, "MAX_OPEN_POSITIONS", 3),
        intraday_loss_halt_pct=_decimal(env, "INTRADAY_LOSS_HALT_PCT", "0.03"),
        peak_drawdown_halt_pct=_decimal(env, "PEAK_DRAWDOWN_HALT_PCT", "0.10"),
    )

    capital = _decimal(env, "CAPITAL_BASE_USD", "10000")
    _range("CAPITAL_BASE_USD", capital, HARD_MAX_CAPITAL_BASE, minimum=Decimal("100"))

    symbols = tuple(s.strip().upper() for s in env.get("SYMBOLS", "SPY,QQQ,IWM").split(","))
    if not symbols or any(not SYMBOL_RE.match(s) for s in symbols):
        raise ConfigError("SYMBOLS must be a comma-separated list of tickers.")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("SYMBOLS contains duplicates.")

    return Settings(
        app_env=app_env,
        alpaca_api_key=Secret(key),
        alpaca_secret_key=Secret(secret),
        order_submission_enabled=_bool(env, "ORDER_SUBMISSION_ENABLED", default=False),
        capital_base_usd=capital,
        symbols=symbols,
        risk=risk,
    )


# --- helpers ------------------------------------------------------------------------------


def _reject_ambiguous(env: Mapping[str, str]) -> None:
    present = [name for name in AMBIGUOUS_ENV_VARS if name in env]
    if present:
        raise ConfigError(
            "Ambiguous broker configuration: remove "
            + ", ".join(sorted(present))
            + ". Use ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY only."
        )
    live_named = sorted(n for n in env if _LIVE_ENV_NAME_RE.search(n.upper()))
    if live_named:
        raise ConfigError(
            "Live-trading style variables found: " + ", ".join(live_named) + ". Remove them."
        )


def _validate_paper_key_id(key: str) -> None:
    if key.strip() != key or not key.isalnum():
        raise ConfigError("ALPACA_PAPER_API_KEY looks malformed.")
    upper = key.upper()
    if upper.startswith("AK"):
        raise ConfigError("ALPACA_PAPER_API_KEY looks like a LIVE key (prefix 'AK'). Refusing.")
    if not upper.startswith("PK"):
        raise ConfigError(
            "ALPACA_PAPER_API_KEY must be a paper key (prefix 'PK'). "
            "Unrecognised key formats are refused."
        )
    if len(key) < 16:
        raise ConfigError("ALPACA_PAPER_API_KEY looks too short.")


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"{name} is required.")
    return value.strip() if name not in ("TRADING_MODE",) else value


def _decimal(env: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = env.get(name, default).strip() or default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc
    if not value.is_finite():
        raise ConfigError(f"{name} must be finite.")
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default)).strip() or str(default)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}.")


def _range(name: str, value: Decimal, maximum: Decimal, minimum: Decimal = Decimal(0)) -> None:
    if not (minimum < value <= maximum):
        raise ConfigError(f"{name} must be > {minimum} and <= {maximum}, got {value}.")
