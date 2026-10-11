import copy
import pickle
import unittest
from decimal import Decimal

from tests.helpers import FAKE_TIINGO_TOKEN, valid_env
from tradinglab.config import ConfigError, RiskLimits, Settings, load_settings


class LoadSettingsTests(unittest.TestCase):
    def test_valid_defaults(self):
        s = load_settings(valid_env())
        self.assertEqual(s.trading_mode, "simulated")
        self.assertEqual(s.capital_base_usd, Decimal("10000"))
        self.assertEqual(s.symbols, ("SPY", "QQQ", "IWM"))
        self.assertEqual(s.risk.risk_per_trade_pct, Decimal("0.005"))
        self.assertEqual(s.risk.max_exposure_pct, Decimal("0.30"))
        self.assertEqual(s.risk.max_open_positions, 3)
        self.assertEqual(s.egress_hosts, frozenset({"api.tiingo.com", "api.anthropic.com"}))
        self.assertIsNone(s.data_token)

    def test_trading_mode_must_be_exactly_simulated(self):
        for mode in ("live", "paper", "Simulated", " simulated", "simulated ", "", "real"):
            with self.subTest(mode=mode), self.assertRaises(ConfigError):
                load_settings(valid_env(TRADING_MODE=mode))

    def test_missing_required(self):
        for name in ("APP_ENV", "TRADING_MODE"):
            env = valid_env()
            del env[name]
            with self.subTest(missing=name), self.assertRaises(ConfigError):
                load_settings(env)

    def test_brokerage_variables_refused(self):
        for name in ("ALPACA_API_KEY", "APCA_API_KEY_ID", "IBKR_ACCOUNT", "IB_GATEWAY_HOST",
                     "QUESTRADE_REFRESH_TOKEN", "WEALTHSIMPLE_PASSWORD", "TRADIER_TOKEN",
                     "SCHWAB_APP_KEY", "BROKER_URL", "MY_BROKER_KEY", "LIVE_TRADING",
                     "alpaca_secret"):
            with self.subTest(var=name), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: "x"}))

    def test_tiingo_token(self):
        s = load_settings(valid_env(TIINGO_API_TOKEN=FAKE_TIINGO_TOKEN))
        self.assertEqual(s.require_data_token().get(), FAKE_TIINGO_TOKEN)
        for bad in ("short", "has spaces in it 0000000000", "x" * 70):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_settings(valid_env(TIINGO_API_TOKEN=bad))
        with self.assertRaises(ConfigError):
            load_settings(valid_env()).require_data_token()

    def test_anthropic_key_format(self):
        load_settings(valid_env(ANTHROPIC_API_KEY="sk-ant-" + "a" * 40))
        with self.assertRaises(ConfigError):
            load_settings(valid_env(ANTHROPIC_API_KEY="not-a-key"))

    def test_database_host_added_to_egress(self):
        s = load_settings(valid_env(
            DATABASE_URL="postgresql://u:p@postgres.railway.internal:5432/railway"))
        self.assertIn("postgres.railway.internal", s.egress_hosts)
        for bad in ("mysql://x@h/db", "postgresql:///nohost"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_settings(valid_env(DATABASE_URL=bad))

    def test_database_url_cannot_point_at_brokerage(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(DATABASE_URL="postgresql://u:p@db.alpaca.markets/x"))

    def test_any_proxy_variable_refused(self):
        for var in ("HTTPS_PROXY", "http_proxy", "ALL_PROXY", "WSS_PROXY"):
            with self.subTest(var=var), self.assertRaises(ConfigError):
                load_settings(valid_env(**{var: "http://p:3128"}))
        load_settings(valid_env(NO_PROXY="localhost"))

    def test_risk_hard_ceilings(self):
        bad = {"RISK_PER_TRADE_PCT": "0.05", "MAX_PORTFOLIO_EXPOSURE_PCT": "1.5",
               "MAX_OPEN_POSITIONS": "50", "INTRADAY_LOSS_HALT_PCT": "0.5",
               "PEAK_DRAWDOWN_HALT_PCT": "0.9"}
        for name, value in bad.items():
            with self.subTest(name=name), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: value}))
        for value in ("0", "-0.1", "nan", "abc", "5e-3", "1_0"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings(valid_env(RISK_PER_TRADE_PCT=value))

    def test_padded_or_empty_values_rejected(self):
        for name, value in (("APP_ENV", " test"), ("RISK_PER_TRADE_PCT", ""),
                            ("SYMBOLS", " SPY"), ("MAX_OPEN_POSITIONS", " 3"),
                            ("TIINGO_API_TOKEN", "")):
            with self.subTest(name=name), self.assertRaises(ConfigError):
                load_settings(valid_env(**{name: value}))

    def test_symbols(self):
        self.assertEqual(load_settings(valid_env(SYMBOLS="spy, qqq")).symbols, ("SPY", "QQQ"))
        for bad in ("SPY,spy", "SPY,BRK.B"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load_settings(valid_env(SYMBOLS=bad))

    def test_cannot_hand_build_real_trading_settings(self):
        with self.assertRaises(ConfigError):
            Settings("test", Decimal(10000), ("SPY",), RiskLimits(), trading_mode="live")


class SecretTests(unittest.TestCase):
    def test_secrets_hidden_and_unpicklable(self):
        s = load_settings(valid_env(TIINGO_API_TOKEN=FAKE_TIINGO_TOKEN,
                                    DATABASE_URL="postgresql://u:hunter2@db.internal/x"))
        for text in (repr(s), str(s), f"{s}", repr(s.data_token), f"{s.database_url}"):
            self.assertNotIn(FAKE_TIINGO_TOKEN, text)
            self.assertNotIn("hunter2", text)
        with self.assertRaises(TypeError):
            pickle.dumps(s.data_token)
        with self.assertRaises(TypeError):
            copy.deepcopy(s.database_url)

    def test_errors_do_not_echo_secret(self):
        with self.assertRaises(ConfigError) as ctx:
            load_settings(valid_env(TIINGO_API_TOKEN="bad token value 1234567890"))
        self.assertNotIn("bad token value", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
