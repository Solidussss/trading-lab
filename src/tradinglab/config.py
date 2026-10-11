"""Fail-closed configuration for a simulation-only trading lab.

Every setting is read from environment variables (Railway service variables in production,
a git-ignored .env locally). If anything is missing, malformed, out of range, or ambiguous,
`load_settings()` raises ConfigError and the process must not start.

There is no brokerage configuration of any kind. Variables that look like brokerage
credentials are treated as a configuration error, so a broker key can never be loaded
"just in case".

Secrets are wrapped in `Secret`, whose repr/str/format never reveal the value and which
cannot be pickled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final
from urllib.parse import parse_qsl, urlsplit

from tradinglab.safety.endpoints import DEFAULT_ALLOWED_HOSTS, is_brokerage_host, normalize_host


class ConfigError(RuntimeError):
    """Configuration is missing, invalid, or ambiguous. The app must not start."""


# --- Hard ceilings: configuration can tighten these, never loosen them. -------------------
HARD_MAX_RISK_PER_TRADE: Final = Decimal("0.02")
HARD_MAX_EXPOSURE: Final = Decimal("1.00")  # 100% of capital => no leverage, ever
HARD_MAX_OPEN_POSITIONS: Final = 10
HARD_MAX_INTRADAY_HALT: Final = Decimal("0.10")
HARD_MAX_PEAK_DD_HALT: Final = Decimal("0.30")
HARD_MAX_CAPITAL_BASE: Final = Decimal("1000000")

ALLOWED_APP_ENVS: Final = frozenset({"development", "test", "production"})
SYMBOL_RE: Final = re.compile(r"^[A-Z]{1,5}$")
TIINGO_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9]{20,64}$")
ANTHROPIC_KEY_RE: Final = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,200}$")
_DECIMAL_RE: Final = re.compile(r"^-?\d+(\.\d+)?$")
LIBPQ_HOST_VARS: Final = frozenset({"PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE",
                                     "PGPASSFILE", "PGSYSCONFDIR"})
_TRUE: Final = frozenset({"true", "1", "yes"})
_FALSE: Final = frozenset({"false", "0", "no"})

# Any variable whose name looks like brokerage configuration aborts startup.
BROKERAGE_ENV_RE: Final = re.compile(
    r"^(ALPACA|APCA|IBKR|IB_|TWS_|INTERACTIVE_?BROKERS|QUESTRADE|QTRADE|WEALTHSIMPLE|WS_API|"
    r"TRADIER|SCHWAB|TDA_|TDAMERITRADE|ETRADE|ROBINHOOD|TASTY|WEBULL|MOOMOO|FUTU|TRADESTATION|"
    r"OANDA|FXCM|FIDELITY|COINBASE|KRAKEN|BINANCE|BROKER)|_BROKER(AGE)?(_|$)|LIVE_TRADING|"
    r"REAL_MONEY",
    re.IGNORECASE,
)


class Secret:
    """Holds a secret string; never reveals it via str/repr/format and refuses pickling."""

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

    def __reduce__(self):
        raise TypeError("Secret values cannot be pickled.")

    def __eq__(self, other: object) -> bool:  # pragma: no cover
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
    capital_base_usd: Decimal
    symbols: tuple[str, ...]
    risk: RiskLimits
    data_token: Secret | None = field(default=None, repr=False)
    anthropic_key: Secret | None = field(default=None, repr=False)
    database_url: Secret | None = field(default=None, repr=False)
    egress_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    trading_mode: str = "simulated"

    def __post_init__(self) -> None:
        if self.trading_mode != "simulated":
            raise ConfigError("trading_mode must be 'simulated'. Real trading is not supported.")

    def require_data_token(self) -> Secret:
        if self.data_token is None:
            raise ConfigError("TIINGO_API_TOKEN is required for market-data access.")
        return self.data_token


def load_settings(env: Mapping[str, str]) -> Settings:
    """Build Settings from an environment mapping (pass `os.environ` in production)."""
    _reject_brokerage_and_proxies(env)

    app_env = _required(env, "APP_ENV")
    if app_env not in ALLOWED_APP_ENVS:
        raise ConfigError(f"APP_ENV must be one of {sorted(ALLOWED_APP_ENVS)}.")

    if _required(env, "TRADING_MODE") != "simulated":
        raise ConfigError("TRADING_MODE must be exactly 'simulated'. Real trading is not supported.")

    data_token = None
    if "TIINGO_API_TOKEN" in env:
        data_token = Secret(_optional(env, "TIINGO_API_TOKEN", ""))
        if not TIINGO_TOKEN_RE.match(data_token.get()):
            raise ConfigError("TIINGO_API_TOKEN looks malformed.")

    anthropic_key = None
    if "ANTHROPIC_API_KEY" in env:
        anthropic_key = Secret(_optional(env, "ANTHROPIC_API_KEY", ""))
        if not ANTHROPIC_KEY_RE.match(anthropic_key.get()):
            raise ConfigError("ANTHROPIC_API_KEY looks malformed.")

    egress = set(DEFAULT_ALLOWED_HOSTS)
    database_url = None
    if "DATABASE_URL" in env:
        database_url = Secret(_optional(env, "DATABASE_URL", ""))
        egress.add(_database_host(database_url))

    risk = RiskLimits(
        risk_per_trade_pct=_decimal(env, "RISK_PER_TRADE_PCT", "0.005"),
        max_exposure_pct=_decimal(env, "MAX_PORTFOLIO_EXPOSURE_PCT", "0.30"),
        max_open_positions=_int(env, "MAX_OPEN_POSITIONS", 3),
        intraday_loss_halt_pct=_decimal(env, "INTRADAY_LOSS_HALT_PCT", "0.03"),
        peak_drawdown_halt_pct=_decimal(env, "PEAK_DRAWDOWN_HALT_PCT", "0.10"),
    )
    capital = _decimal(env, "CAPITAL_BASE_USD", "10000")
    _range("CAPITAL_BASE_USD", capital, HARD_MAX_CAPITAL_BASE, minimum=Decimal("100"))

    symbols = tuple(
        s.strip().upper() for s in _optional(env, "SYMBOLS", "SPY,QQQ,IWM").split(",")
    )
    if not symbols or any(not SYMBOL_RE.match(s) for s in symbols):
        raise ConfigError("SYMBOLS must be a comma-separated list of tickers.")
    if len(set(symbols)) != len(symbols):
        raise ConfigError("SYMBOLS contains duplicates.")

    return Settings(
        app_env=app_env, capital_base_usd=capital, symbols=symbols, risk=risk,
        data_token=data_token, anthropic_key=anthropic_key, database_url=database_url,
        egress_hosts=frozenset(egress),
    )


# --- helpers ------------------------------------------------------------------------------


def _reject_brokerage_and_proxies(env: Mapping[str, str]) -> None:
    brokerage = sorted(n for n in env if BROKERAGE_ENV_RE.search(n))
    if brokerage:
        raise ConfigError(
            "Brokerage-style variables found: " + ", ".join(brokerage) + ". Trading Lab is "
            "simulation-only and never loads brokerage credentials. Remove them."
        )
    # A proxy hides real destinations from the socket-level egress guard. Refused everywhere.
    proxies = sorted(
        n for n in env
        if n.upper().endswith("_PROXY") and n.upper() != "NO_PROXY" and env[n].strip()
    )
    # libpq reads these and connects in C, outside the egress guard. DATABASE_URL is the
    # only accepted way to name the database host.
    libpq = sorted(n for n in env if n.upper() in LIBPQ_HOST_VARS)
    if libpq:
        raise ConfigError("Remove " + ", ".join(libpq) + ": the database host must come only "
                          "from DATABASE_URL, which is validated.")
    if proxies:
        raise ConfigError(
            "Proxy variables " + ", ".join(proxies) + " are not allowed: they would hide "
            "network destinations from the egress guard."
        )


def _database_host(url: Secret) -> str:
    parts = urlsplit(url.get())
    if parts.scheme not in ("postgresql", "postgres", "postgresql+psycopg"):
        raise ConfigError("DATABASE_URL must be a postgresql:// URL.")
    if "," in parts.netloc:
        raise ConfigError("DATABASE_URL must name exactly one host.")
    query_keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    forbidden = query_keys & {"host", "hostaddr", "service", "servicefile", "passfile"}
    if forbidden:
        raise ConfigError("DATABASE_URL may not override the host via query parameters ("
                          + ", ".join(sorted(forbidden)) + ").")
    if not parts.hostname:
        raise ConfigError("DATABASE_URL has no host.")
    try:
        host = normalize_host(parts.hostname)
    except Exception as exc:
        raise ConfigError("DATABASE_URL host is malformed.") from exc
    if is_brokerage_host(host):
        raise ConfigError("DATABASE_URL points at a brokerage domain; refused.")
    return host


def _required(env: Mapping[str, str], name: str) -> str:
    """Exact value of a required variable. Surrounding whitespace is an error, not trimmed."""
    if name not in env or not env[name].strip():
        raise ConfigError(f"{name} is required.")
    if env[name] != env[name].strip():
        raise ConfigError(f"{name} has leading/trailing whitespace.")
    return env[name]


def _optional(env: Mapping[str, str], name: str, default: str) -> str:
    """Optional variable: absent -> default. Present but empty/padded -> error (ambiguous)."""
    if name not in env:
        return default
    raw = env[name]
    if not raw.strip():
        raise ConfigError(f"{name} is set but empty; set a value or remove it.")
    if raw != raw.strip():
        raise ConfigError(f"{name} has leading/trailing whitespace.")
    return raw


def _decimal(env: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = _optional(env, name, default)
    if not _DECIMAL_RE.match(raw):
        raise ConfigError(f"{name} must be a plain decimal number, got {raw!r}.")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from exc
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _optional(env, name, str(default))
    if not re.fullmatch(r"\d{1,6}", raw):
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.")
    return int(raw)


def bool_setting(env: Mapping[str, str], name: str, default: bool) -> bool:
    if name not in env:
        return default
    v = _optional(env, name, "").lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {v!r}.")


def _range(name: str, value: Decimal, maximum: Decimal, minimum: Decimal = Decimal(0)) -> None:
    if not (minimum < value <= maximum):
        raise ConfigError(f"{name} must be > {minimum} and <= {maximum}, got {value}.")
