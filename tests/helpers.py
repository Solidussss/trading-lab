"""Shared test fixtures (no real secrets: obviously fake tokens)."""

from __future__ import annotations

from tradinglab.config import Settings, load_settings

FAKE_TIINGO_TOKEN = "faketiingotoken0000000000000000000000000"


def valid_env(**overrides: str) -> dict[str, str]:
    env = {"APP_ENV": "test", "TRADING_MODE": "simulated"}
    env.update(overrides)
    return env


def settings(**overrides: str) -> Settings:
    return load_settings(valid_env(**overrides))
