"""Proof that Trading Lab cannot place a real trade.

Trading Lab has no brokerage connection. These tests show each way one could be introduced
is closed:

  A. Configuration  - brokerage credentials or real-trading modes cannot be loaded.
  B. Network        - the process cannot resolve or connect to any brokerage host, nor to
                      any host outside the egress allowlist (data provider, Claude API, DB).
  C. Source code    - no brokerage SDKs, no brokerage hostnames, no Broker implementation,
                      and outbound HTTP exists only in the vetted data client.
  D. Simulation     - trades exist only as records in the simulator's ledger.
"""

from __future__ import annotations

import ast
import pathlib
import re
import socket
import unittest

from tests.helpers import valid_env
from tradinglab.config import ConfigError, load_settings
from tradinglab.safety.endpoints import BrokerageForbiddenError, EgressDeniedError
from tradinglab.safety.network_guard import _audit_hook, blocked_attempts, install_network_guard

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "tradinglab"
BROKERAGE_HOSTS = ["api.alpaca.markets", "paper-api.alpaca.markets", "api.ibkr.com",
                   "localhost.ibkr.com", "api01.iq.questrade.com", "login.questrade.com",
                   "trade-service.wealthsimple.com", "api.tradier.com", "api.schwabapi.com",
                   "api.robinhood.com", "api-fxtrade.oanda.com", "api.tastyworks.com"]


def py_files():
    return list(SRC.rglob("*.py"))


class A_ConfigurationTests(unittest.TestCase):
    def test_no_real_trading_mode(self):
        for mode in ("live", "paper", "real", "broker"):
            with self.subTest(mode=mode), self.assertRaises(ConfigError):
                load_settings(valid_env(TRADING_MODE=mode))

    def test_brokerage_credentials_refused(self):
        for var in ("ALPACA_API_KEY", "IBKR_USERNAME", "QUESTRADE_REFRESH_TOKEN",
                    "WEALTHSIMPLE_OTP", "BROKER_API_KEY"):
            with self.subTest(var=var), self.assertRaises(ConfigError):
                load_settings(valid_env(**{var: "x"}))


class B_NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_network_guard()

    def test_dns_to_brokerages_blocked(self):
        for host in BROKERAGE_HOSTS + ["API.ALPACA.MARKETS", "api.ibkr.com."]:
            with self.subTest(host=host), self.assertRaises(BrokerageForbiddenError):
                socket.getaddrinfo(host, 443)
        self.assertIn("api.alpaca.markets", [h.lower() for h in blocked_attempts()])

    def test_unicode_lookalike_brokerage_hosts_blocked(self):
        for host in ("api。alpaca。markets", "api．ibkr．com",
                     "login.questrade.com​"):
            with self.subTest(host=repr(host)), self.assertRaises(BrokerageForbiddenError):
                socket.getaddrinfo(host, 443)

    def test_unknown_hosts_blocked(self):
        for host in ("example.com", "some-new-broker.io", "8.8.8.8"):
            with self.subTest(host=host), self.assertRaises(EgressDeniedError):
                socket.getaddrinfo(host, 443)

    def test_create_connection_blocked(self):
        with self.assertRaises(BrokerageForbiddenError):
            socket.create_connection(("api.alpaca.markets", 443), timeout=2)
        with self.assertRaises(EgressDeniedError):
            socket.create_connection(("8.8.8.8", 443), timeout=2)

    def test_raw_connect_event_inspected(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(BrokerageForbiddenError):
                _audit_hook("socket.connect", (s, ("api.ibkr.com", 443)))
            _audit_hook("socket.connect", (s, ("127.0.0.1", 5432)))  # local DB allowed
        finally:
            s.close()

    def test_https_proxy_tunnel_target_inspected(self):
        import http.client

        conn = http.client.HTTPSConnection("127.0.0.1", 3128, timeout=1)
        conn.set_tunnel("api.questrade.com", 443)
        with self.assertRaises(BrokerageForbiddenError):
            conn.connect()
        conn.close()

    def test_urllib_blocked_direct_and_via_proxy(self):
        import urllib.request

        for handler in (urllib.request.ProxyHandler({}),
                        urllib.request.ProxyHandler({"https": "http://127.0.0.1:3128"})):
            opener = urllib.request.build_opener(handler)
            with self.subTest(handler=handler.proxies), self.assertRaises(BrokerageForbiddenError):
                opener.open("https://api.alpaca.markets/v2/orders", timeout=2)

    def test_connect_to_ip_resolved_from_allowlisted_name_is_allowed(self):
        # Auditor finding #1: sockets connect to the resolved IP, not the name. With DNS
        # faked, a connection to api.tiingo.com must pass the guard; any other public IP
        # must still be refused.
        from unittest import mock

        import tradinglab.safety.network_guard as ng

        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("52.4.53.104", 443))]
        with mock.patch.object(ng, "_real_getaddrinfo", return_value=fake):
            res = socket.getaddrinfo("api.tiingo.com", 443)
        self.assertEqual(res, fake)
        self.assertEqual(ng.resolved_ips().get("52.4.53.104"), "api.tiingo.com")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _audit_hook("socket.connect", (s, ("52.4.53.104", 443)))  # allowed
            with self.assertRaises(EgressDeniedError):
                _audit_hook("socket.connect", (s, ("52.4.53.105", 443)))
        finally:
            s.close()

    def test_resolution_of_brokerage_name_records_nothing(self):
        from unittest import mock

        import tradinglab.safety.network_guard as ng

        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("34.1.2.3", 443))]
        with mock.patch.object(ng, "_real_getaddrinfo", return_value=fake), \
                self.assertRaises(BrokerageForbiddenError):
            socket.getaddrinfo("api.alpaca.markets", 443)
        self.assertNotIn("34.1.2.3", ng.resolved_ips())

    def test_bytes_hostnames_and_sendto_are_checked(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with self.assertRaises(BrokerageForbiddenError):
                _audit_hook("socket.connect", (s, (b"api.alpaca.markets", 443)))
            with self.assertRaises(EgressDeniedError):
                u.sendto(b"x", ("8.8.8.8", 53))
            with self.assertRaises(BrokerageForbiddenError):
                _audit_hook("socket.sendto", (u, ("api.ibkr.com", 443)))
            with self.assertRaises(EgressDeniedError):
                _audit_hook("socket.sendmsg", (u, ("8.8.8.8", 443)))
            _audit_hook("socket.sendto", (u, None))  # connected socket: no address
        finally:
            s.close()
            u.close()

    def test_unparseable_addresses_are_denied_not_allowed(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            for addr in (object(), 12345, (), (b"\xff\xfe", 443)):
                with self.subTest(addr=addr), self.assertRaises(EgressDeniedError):
                    _audit_hook("socket.connect", (s, addr))
        finally:
            s.close()

    def test_unix_sockets_are_local_ipc(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no AF_UNIX")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            _audit_hook("socket.connect", (s, "/var/run/postgresql/.s.PGSQL.5432"))
        finally:
            s.close()

    def test_nul_and_metadata_tricks_refused(self):
        for host in ("api.alpaca.markets\x00.localhost", "8.8.8.8\x00.localhost"):
            with self.subTest(host=repr(host)), self.assertRaises(EgressDeniedError):
                socket.getaddrinfo(host, 443)
        for ip in ("169.254.169.254", "0.0.0.0", "198.18.0.1", "2002::1"):
            with self.subTest(ip=ip), self.assertRaises(EgressDeniedError):
                socket.getaddrinfo(ip, 80)

    def test_allowed_hosts_not_intercepted(self):
        for host in ("api.tiingo.com", "api.anthropic.com", "localhost"):
            with self.subTest(host=host):
                try:
                    socket.getaddrinfo(host, 443)
                except (BrokerageForbiddenError, EgressDeniedError):
                    self.fail(f"guard wrongly blocked {host}")
                except OSError:
                    pass  # no DNS in the CI sandbox is fine


class C_SourceCodeTests(unittest.TestCase):
    BROKER_SDK_MODULES = re.compile(
        r"^(alpaca|alpaca_trade_api|ib_insync|ib_async|ibapi|questrade|qtrade|wealthsimple|"
        r"ws_api|tradier|schwab|tda|robin_stocks|oandapyV20|tastytrade|webull|moomoo|futu|"
        r"ccxt|coinbase|binance|krakenex)(\.|$)")

    def _imports(self, path):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    yield a.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                yield node.module

    def test_no_brokerage_sdk_imports(self):
        offenders = [f"{p.name}: {m}" for p in py_files() for m in self._imports(p)
                     if self.BROKER_SDK_MODULES.match(m)]
        self.assertEqual(offenders, [])

    def test_outbound_http_only_in_vetted_modules(self):
        # Network clients may appear only in the data client (urllib) and, later, the
        # Claude research client (anthropic SDK).
        net = re.compile(r"^(urllib\.request|http\.client|requests|httpx|aiohttp|urllib3|"
                         r"websocket|websockets|socket|anthropic)(\.|$)")
        allowed = {SRC / "marketdata" / "tiingo.py": {"urllib.request"},
                   SRC / "safety" / "selfcheck.py": {"socket", "urllib.request"},
                   SRC / "safety" / "network_guard.py": {"socket"},
                   SRC / "research" / "claude.py": {"anthropic"}}
        offenders = []
        for p in py_files():
            for m in self._imports(p):
                if net.match(m) and m.split(".")[0] not in {
                        a.split(".")[0] for a in allowed.get(p, set())}:
                    offenders.append(f"{p.relative_to(SRC)}: {m}")
        self.assertEqual(offenders, [])

    def test_brokerage_domains_only_in_denylist(self):
        from tradinglab.safety.endpoints import BROKERAGE_DOMAINS

        allowed = {SRC / "safety" / "endpoints.py", SRC / "safety" / "selfcheck.py"}
        pats = [re.compile(r"(?<![\w-])" + re.escape(d) + r"\b") for d in BROKERAGE_DOMAINS]
        offenders = []
        for p in py_files():
            if p in allowed:
                continue
            text = p.read_text()
            offenders += [f"{p.relative_to(SRC)}: {pat.pattern}" for pat in pats
                          if pat.search(text)]
        self.assertEqual(offenders, [])

    def test_no_broker_implementation(self):
        # Nothing may implement order submission outside the documented future interface.
        offenders = []
        for p in py_files():
            if p == SRC / "broker" / "base.py":
                continue
            tree = ast.parse(p.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in (
                        "submit_order", "place_order", "cancel_order", "replace_order"):
                    offenders.append(f"{p.relative_to(SRC)}:{node.lineno} {node.name}")
        self.assertEqual(offenders, [])


class D_SimulationTests(unittest.TestCase):
    def test_engine_has_no_network_besides_data_source(self):
        # The live engine's only external input is the BarSource it is given.
        src = (SRC / "engine" / "live.py").read_text()
        self.assertNotIn("urllib", src)
        self.assertNotIn("socket", src)
        sim = (SRC / "sim" / "core.py").read_text()
        self.assertNotIn("import urllib", sim)


if __name__ == "__main__":
    unittest.main()
