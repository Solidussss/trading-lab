import unittest

from tradinglab.safety.endpoints import (
    PAPER_TRADING_URL,
    EndpointNotAllowedError,
    LiveTradingForbiddenError,
    check_alpaca_host,
    normalize_host,
    validate_data_url,
    validate_trading_url,
)


class TradingUrlTests(unittest.TestCase):
    def test_paper_url_accepted(self):
        self.assertEqual(validate_trading_url("https://paper-api.alpaca.markets"), PAPER_TRADING_URL)
        self.assertEqual(
            validate_trading_url("https://PAPER-API.alpaca.markets:443/"), PAPER_TRADING_URL
        )

    def test_live_urls_forbidden(self):
        for url in (
            "https://api.alpaca.markets",
            "https://API.ALPACA.MARKETS/v2",
            "https://api.alpaca.markets.",
            "https://api.alpaca.markets:443",
            "https://broker-api.alpaca.markets",
        ):
            with self.subTest(url=url), self.assertRaises(LiveTradingForbiddenError):
                validate_trading_url(url)

    def test_unknown_and_tricky_urls_rejected(self):
        for url in (
            "http://paper-api.alpaca.markets",  # not https
            "https://paper-api.alpaca.markets.evil.com",
            "https://evil.com/paper-api.alpaca.markets",
            "https://paper-api.alpaca.markets@api.alpaca.markets",  # userinfo trick
            "https://user:pw@paper-api.alpaca.markets",
            "https://paper-api.alpaca.markets:8443",
            "https://data.alpaca.markets",  # data host is not a trading host
            "https://new-live.alpaca.markets",
            "",
            "paper-api.alpaca.markets",
        ):
            with self.subTest(url=url), self.assertRaises(
                (EndpointNotAllowedError, LiveTradingForbiddenError)
            ):
                validate_trading_url(url)

    def test_userinfo_trick_resolves_to_live_host(self):
        # urlsplit treats the part after '@' as the host - that must be caught as live.
        with self.assertRaises(LiveTradingForbiddenError):
            validate_trading_url("https://paper-api.alpaca.markets@api.alpaca.markets")


class HostTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_host(" Paper-API.Alpaca.Markets.:443 "), "paper-api.alpaca.markets")

    def test_data_hosts(self):
        self.assertEqual(validate_data_url("https://data.alpaca.markets/v2"), "data.alpaca.markets")
        self.assertEqual(
            validate_data_url("wss://stream.data.alpaca.markets/v2/iex"), "stream.data.alpaca.markets"
        )
        with self.assertRaises(LiveTradingForbiddenError):
            validate_data_url("https://api.alpaca.markets")

    def test_unknown_alpaca_subdomain_fails_closed(self):
        with self.assertRaises(EndpointNotAllowedError):
            check_alpaca_host("anything-new.alpaca.markets")


if __name__ == "__main__":
    unittest.main()
