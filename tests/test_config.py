import unittest
from decimal import Decimal

from tests.helpers import FAKE_PAPER_KEY, FAKE_SECRET, valid_env
from tradinglab.config import ConfigError, RiskLimits, Settings, load_settings
from tradinglab.config import Secret


class LoadSettingsTests(unittest.TestCase):
    def test_valid_defaults(self):
        s = load_settings(valid_env())
        self.assertEqual(s.trading_mode, "paper")
        self.assertEqual(s.trading_base_url, "https://paper-api.alpaca.markets")
        self.assertFalse(s.order_submission_enabled, "order submission must default OFF")
        self.assertEqual(s.capital_base_usd, Decimal("10000"))
        self.assertEqual(s.symbols, ("SPY", "QQQ", "IWM"))
        self.assertEqual(s.risk.risk_per_trade_pct, Decimal("0.005"))
        self.assertEqual(s.risk.max_exposure_pct, Decimal("0.30"))
        self.assertEqual(s.risk.max_open_positions, 3)

    def test_trading_mode_must_be_exactly_paper(self):
        for mode in ("live", "Paper", "PAPER", " paper", "paper ", "", "both", "real"):
            with self.subTest(mode=mode), self.assertRaises(ConfigError):
                load_settings(valid_env(TRADING_MODE=mode))

    def test_missing_required(self):
        for name in ("APP_ENV", "TRADING_MODE", "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY"):
            env = valid_env()
            del env[name]
            with self.subTest(missing=name), self.assertRaises(ConfigError):
                load_settings(env)

    def test_ambiguous_env_vars_fail_closed(self):
        for name in (
            "APCA_API_KEY_ID",
            "APCA_API_SECRET_KEY",
            "APCA_API_BASE_URL",
            "ALPACA_API_KEY",
            "ALPACA_BASE_URL",
        ):
            with self.subTest(var=name), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: "x"}))

    def test_paper_base_url_in_apca_var_is_still_ambiguous(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(APCA_API_BASE_URL="https://paper-api.alpaca.markets"))

    def test_live_named_vars_rejected(self):
        for name in ("ALPACA_LIVE_API_KEY", "LIVE_ALPACA_SECRET", "APCA_LIVE_URL"):
            with self.subTest(var=name), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: "x"}))

    def test_live_looking_key_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_settings(valid_env(ALPACA_PAPER_API_KEY="AKLIVEKEY00000000000"))
        self.assertIn("LIVE", str(ctx.exception))

    def test_unknown_key_prefix_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(ALPACA_PAPER_API_KEY="XXSOMETHING000000000"))

    def test_malformed_secrets(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(ALPACA_PAPER_SECRET_KEY="short"))
        with self.assertRaises(ConfigError):
            load_settings(valid_env(ALPACA_PAPER_API_KEY="PK bad key 000000000"))

    def test_order_submission_flag_parsing(self):
        self.assertTrue(load_settings(valid_env(ORDER_SUBMISSION_ENABLED="true")).order_submission_enabled)
        self.assertFalse(load_settings(valid_env(ORDER_SUBMISSION_ENABLED="false")).order_submission_enabled)
        with self.assertRaises(ConfigError):
            load_settings(valid_env(ORDER_SUBMISSION_ENABLED="maybe"))

    def test_risk_hard_ceilings(self):
        bad = {
            "RISK_PER_TRADE_PCT": "0.05",
            "MAX_PORTFOLIO_EXPOSURE_PCT": "1.5",  # leverage
            "MAX_OPEN_POSITIONS": "50",
            "INTRADAY_LOSS_HALT_PCT": "0.5",
            "PEAK_DRAWDOWN_HALT_PCT": "0.9",
        }
        for name, value in bad.items():
            with self.subTest(name=name), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: value}))
        for name in ("RISK_PER_TRADE_PCT", "MAX_PORTFOLIO_EXPOSURE_PCT"):
            for value in ("0", "-0.1", "nan", "abc"):
                with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                    load_settings(valid_env(**{name: value}))

    def test_symbols_validation(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(SYMBOLS="SPY,spy"))
        with self.assertRaises(ConfigError):
            load_settings(valid_env(SYMBOLS="SPY,BRK.B"))
        self.assertEqual(load_settings(valid_env(SYMBOLS="spy, qqq")).symbols, ("SPY", "QQQ"))


class StrictParsingTests(unittest.TestCase):
    def test_padded_or_empty_values_rejected_not_trimmed(self):
        for name, value in (("APP_ENV", " test"), ("ALPACA_PAPER_API_KEY", "PKTESTFAKE0000000000 "),
                            ("RISK_PER_TRADE_PCT", ""), ("ORDER_SUBMISSION_ENABLED", ""),
                            ("SYMBOLS", " SPY"), ("MAX_OPEN_POSITIONS", " 3")):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: value}))

    def test_app_env_exact_lowercase(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(APP_ENV="Production"))

    def test_numeric_formats(self):
        for name, value in (("MAX_OPEN_POSITIONS", "1_0"), ("MAX_OPEN_POSITIONS", "2.0"),
                            ("RISK_PER_TRADE_PCT", "5e-3"), ("RISK_PER_TRADE_PCT", "0.005%")):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: value}))

    def test_any_proxy_variable_refused_in_every_env(self):
        for app_env in ("test", "development", "production"):
            for var in ("HTTPS_PROXY", "http_proxy", "ALL_PROXY", "WSS_PROXY", "ftp_proxy"):
                with self.subTest(env=app_env, var=var), self.assertRaises(ConfigError):
                    load_settings(valid_env(APP_ENV=app_env, **{var: "http://p:3128"}))
        load_settings(valid_env(NO_PROXY="localhost"))  # NO_PROXY is harmless


class SecretHandlingTests(unittest.TestCase):
    def test_secret_cannot_be_pickled_or_copied(self):
        import copy
        import pickle

        s = load_settings(valid_env())
        with self.assertRaises(TypeError):
            pickle.dumps(s.alpaca_secret_key)
        with self.assertRaises(TypeError):
            copy.deepcopy(s.alpaca_api_key)

    def test_secrets_never_in_repr_or_str(self):
        s = load_settings(valid_env())
        for text in (repr(s), str(s), f"{s}", repr(s.alpaca_api_key), f"{s.alpaca_secret_key}"):
            self.assertNotIn(FAKE_PAPER_KEY, text)
            self.assertNotIn(FAKE_SECRET, text)

    def test_config_error_does_not_echo_secret(self):
        with self.assertRaises(ConfigError) as ctx:
            load_settings(valid_env(ALPACA_PAPER_API_KEY="AKLIVEKEY00000000000"))
        self.assertNotIn("AKLIVEKEY00000000000", str(ctx.exception))


class SettingsConstructionTests(unittest.TestCase):
    def test_cannot_hand_build_live_settings(self):
        common = dict(
            app_env="test",
            alpaca_api_key=Secret(FAKE_PAPER_KEY),
            alpaca_secret_key=Secret(FAKE_SECRET),
            order_submission_enabled=False,
            capital_base_usd=Decimal("10000"),
            symbols=("SPY",),
            risk=RiskLimits(),
        )
        with self.assertRaises(ConfigError):
            Settings(**common, trading_mode="live")
        with self.assertRaises(Exception):
            Settings(**common, trading_base_url="https://api.alpaca.markets")


if __name__ == "__main__":
    unittest.main()
