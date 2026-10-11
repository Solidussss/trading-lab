"""Shared test fixtures (no secrets: obviously fake paper-format keys)."""

from __future__ import annotations

from tradinglab.config import Settings, load_settings

FAKE_PAPER_KEY = "PKTESTFAKE0000000000"
FAKE_SECRET = "fake-secret-0000000000000000000000000000"


def valid_env(**overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "test",
        "TRADING_MODE": "paper",
        "ALPACA_PAPER_API_KEY": FAKE_PAPER_KEY,
        "ALPACA_PAPER_SECRET_KEY": FAKE_SECRET,
    }
    env.update(overrides)
    return env


def settings(**overrides: str) -> Settings:
    return load_settings(valid_env(**overrides))
