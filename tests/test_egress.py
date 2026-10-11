import unittest

from tradinglab.safety.endpoints import (
    BrokerageForbiddenError,
    EgressDeniedError,
    EgressPolicy,
    is_brokerage_host,
    normalize_host,
)


class NormalizeTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(normalize_host(" API.Tiingo.COM.:443 "), "api.tiingo.com")

    def test_unicode_lookalikes_map_to_real_host(self):
        for h in ("api。alpaca。markets", "api．alpaca．markets",
                  "api｡alpaca｡markets", "api.alpaca﹒markets",
                  "api.alpaca.markets​"):
            with self.subTest(h=repr(h)):
                self.assertEqual(normalize_host(h), "api.alpaca.markets")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = EgressPolicy()

    def test_default_allowlist(self):
        self.assertEqual(self.p.check("api.tiingo.com"), "api.tiingo.com")
        self.assertEqual(self.p.check("API.ANTHROPIC.COM."), "api.anthropic.com")

    def test_brokerages_forbidden(self):
        for h in ("api.alpaca.markets", "paper-api.alpaca.markets", "data.alpaca.markets",
                  "api.ibkr.com", "api01.iq.questrade.com", "login.questrade.com",
                  "trade-service.wealthsimple.com", "api.tradier.com", "api.schwabapi.com",
                  "api.robinhood.com", "api-fxtrade.oanda.com", "api.coinbase.com",
                  "API。IBKR。COM"):
            with self.subTest(h=h), self.assertRaises(BrokerageForbiddenError):
                self.p.check(h)

    def test_brokerage_cannot_be_allowlisted(self):
        with self.assertRaises(BrokerageForbiddenError):
            self.p.allow(["api.alpaca.markets"])
        with self.assertRaises(BrokerageForbiddenError):
            EgressPolicy(["api.questrade.com"])

    def test_unknown_hosts_and_public_ips_denied(self):
        for h in ("example.com", "api.tiingo.com.evil.com", "tiingo.com", "8.8.8.8",
                  "2001:4860:4860::8888", ""):
            with self.subTest(h=h), self.assertRaises(EgressDeniedError):
                self.p.check(h)

    def test_local_and_private_allowed(self):
        for h in ("localhost", "127.0.0.1", "::1", "10.1.2.3", "192.168.0.5"):
            with self.subTest(h=h):
                self.p.check(h)

    def test_lookalike_brokerage_suffix_not_confused(self):
        self.assertFalse(is_brokerage_host("notalpaca.markets.example.com"))
        self.assertTrue(is_brokerage_host("x.y.alpaca.markets"))

    def test_freeze(self):
        self.p.allow(["db.internal"])
        self.p.freeze()
        with self.assertRaises(EgressDeniedError):
            self.p.allow(["new.host"])
        self.assertEqual(self.p.check("db.internal"), "db.internal")

    def test_check_url(self):
        self.assertEqual(self.p.check_url("https://api.tiingo.com/tiingo/daily/spy/prices"),
                         "api.tiingo.com")
        for bad in ("http://api.tiingo.com/x", "https://api.tiingo.com:8443/",
                    "https://user:pw@api.tiingo.com/", "https://evil.com/api.tiingo.com", ""):
            with self.subTest(url=bad), self.assertRaises(EgressDeniedError):
                self.p.check_url(bad)
        with self.assertRaises(BrokerageForbiddenError):
            self.p.check_url("https://api.tiingo.com@api.alpaca.markets/v2/orders")


if __name__ == "__main__":
    unittest.main()
