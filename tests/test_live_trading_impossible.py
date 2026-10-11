"""Proof that this codebase cannot place a live (real-money) trade.

Each test attacks one route a live order could take and shows it is closed:

  A. Configuration     - live mode / live URLs / live-looking keys cannot be loaded.
  B. Client factory    - TradingClient is always built with paper=True and the paper URL,
                         and a client that resolves anywhere else is refused.
  C. Adapter           - a client tampered with after construction cannot submit.
  D. Order gate        - a broker that is not paper cannot receive orders.
  E. Network           - the process cannot even resolve/connect to live hosts.
  F. Source code       - no live endpoint, paper=False, or stray TradingClient exists.
"""

from __future__ import annotations

import pathlib
import re
import socket
import unittest
from decimal import Decimal

from tests.helpers import settings, valid_env
from tradinglab.broker.alpaca_paper import (
    AlpacaPaperBroker,
    build_paper_trading_client,
    verify_client_is_paper,
)
from tradinglab.broker.base import OrderRequest, Side
from tradinglab.broker.fake import FakePaperBroker
from tradinglab.config import ConfigError, load_settings
from tradinglab.safety.endpoints import (
    DENIED_LIVE_HOSTS,
    EndpointNotAllowedError,
    LiveTradingForbiddenError,
)
from tradinglab.safety.network_guard import blocked_attempts, install_network_guard
from tradinglab.safety.order_gate import GateDecision, GateOutcome, InMemoryPause, OrderGate

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "tradinglab"


class RecordingTradingClient:
    """Stands in for alpaca.trading.client.TradingClient; mimics its URL resolution."""

    instances: list[RecordingTradingClient] = []

    def __init__(self, api_key=None, secret_key=None, oauth_token=None, paper=True,
                 raw_data=False, url_override=None):
        self.kwargs = dict(api_key=api_key, secret_key=secret_key, paper=paper,
                           url_override=url_override)
        # Same precedence alpaca-py uses: override > paper/live default.
        self._base_url = url_override or (
            "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        )
        self._use_raw_data = raw_data
        self.submitted = []
        RecordingTradingClient.instances.append(self)

    def submit_order(self, req):  # pragma: no cover - must never be reached in these tests
        self.submitted.append(req)
        raise AssertionError("submit_order reached the client")


class LiveDefaultClient(RecordingTradingClient):
    """A hypothetical library version that ignores paper=True and defaults to live."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._base_url = "https://api.alpaca.markets"


class OpaqueClient:
    """A client whose base URL cannot be read back."""

    def __init__(self, **kw):
        pass


def _order(**kw):
    base = dict(symbol="SPY", side=Side.BUY, qty=Decimal(1), client_order_id="tl-test-1")
    base.update(kw)
    return OrderRequest(**base)


class A_ConfigurationTests(unittest.TestCase):
    def test_live_mode_cannot_be_configured(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(TRADING_MODE="live"))

    def test_live_key_cannot_be_configured(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(ALPACA_PAPER_API_KEY="AKLIVE00000000000000"))

    def test_live_base_url_env_cannot_be_configured(self):
        with self.assertRaises(ConfigError):
            load_settings(valid_env(APCA_API_BASE_URL="https://api.alpaca.markets"))


class B_ClientFactoryTests(unittest.TestCase):
    def test_factory_forces_paper_true_and_paper_url(self):
        client = build_paper_trading_client(settings(), client_cls=RecordingTradingClient)
        self.assertIs(client.kwargs["paper"], True)
        self.assertEqual(client.kwargs["url_override"], "https://paper-api.alpaca.markets")

    def test_factory_refuses_client_that_resolves_to_live(self):
        with self.assertRaises(LiveTradingForbiddenError):
            build_paper_trading_client(settings(), client_cls=LiveDefaultClient)

    def test_factory_refuses_client_with_unknown_url(self):
        with self.assertRaises(EndpointNotAllowedError):
            build_paper_trading_client(settings(), client_cls=OpaqueClient)

    def test_factory_refuses_tampered_settings(self):
        s = settings()
        object.__setattr__(s, "trading_base_url", "https://api.alpaca.markets")
        with self.assertRaises(LiveTradingForbiddenError):
            build_paper_trading_client(s, client_cls=RecordingTradingClient)
        s2 = settings()
        object.__setattr__(s2, "trading_mode", "live")
        with self.assertRaises(LiveTradingForbiddenError):
            build_paper_trading_client(s2, client_cls=RecordingTradingClient)

    def test_verify_handles_str_enum_base_url(self):
        from enum import Enum

        class BaseURL(str, Enum):
            TRADING_LIVE = "https://api.alpaca.markets"
            TRADING_PAPER = "https://paper-api.alpaca.markets"

        class EnumClient:
            _base_url = BaseURL.TRADING_LIVE

        with self.assertRaises(LiveTradingForbiddenError):
            verify_client_is_paper(EnumClient())
        EnumClient._base_url = BaseURL.TRADING_PAPER
        verify_client_is_paper(EnumClient())  # no raise


class C_AdapterTests(unittest.TestCase):
    def test_adapter_rejects_live_client_at_construction(self):
        live = LiveDefaultClient()
        with self.assertRaises(LiveTradingForbiddenError):
            AlpacaPaperBroker(settings(), client=live)

    def test_client_tampered_after_construction_cannot_submit(self):
        client = RecordingTradingClient(paper=True, url_override="https://paper-api.alpaca.markets")
        broker = AlpacaPaperBroker(settings(), client=client)
        client._base_url = "https://api.alpaca.markets"  # tamper
        approved = GateDecision(GateOutcome.SUBMIT, (), _order().client_order_id)
        with self.assertRaises(LiveTradingForbiddenError):
            broker.submit_order(_order(), approved)
        with self.assertRaises(LiveTradingForbiddenError):
            broker.cancel_order("abc")
        self.assertEqual(client.submitted, [])


class C2_AdapterLookupTests(unittest.TestCase):
    def _broker(self, exc):
        client = RecordingTradingClient(paper=True, url_override="https://paper-api.alpaca.markets")

        def raise_(cid):
            raise exc

        client.get_order_by_client_id = raise_
        return AlpacaPaperBroker(settings(), client=client)

    def test_only_http_404_means_no_order(self):
        class APIError(Exception):
            def __init__(self, msg, status_code=None):
                super().__init__(msg)
                self.status_code = status_code

        self.assertIsNone(self._broker(APIError("order not found", 404))
                          .get_order_by_client_id("tl-x"))
        # "not found" text without a 404 (e.g. a proxy/gateway page) must not be treated as
        # "no existing order" - that would weaken duplicate-order protection.
        with self.assertRaises(APIError):
            self._broker(APIError("upstream not found", 502)).get_order_by_client_id("tl-x")
        with self.assertRaises(APIError):
            self._broker(APIError("resource not found")).get_order_by_client_id("tl-x")

    def test_adapter_submit_requires_gate_approval(self):
        from tradinglab.safety.order_gate import GateBypassError

        client = RecordingTradingClient(paper=True, url_override="https://paper-api.alpaca.markets")
        broker = AlpacaPaperBroker(settings(), client=client)
        with self.assertRaises(GateBypassError):
            broker.submit_order(_order(), None)
        self.assertEqual(client.submitted, [])


class D_OrderGateTests(unittest.TestCase):
    def test_gate_refuses_non_paper_broker(self):
        fake = FakePaperBroker(prices={"SPY": Decimal(500)})
        fake.is_paper = False
        gate = OrderGate(settings(ORDER_SUBMISSION_ENABLED="true"), fake, InMemoryPause())
        with self.assertRaises(LiveTradingForbiddenError):
            gate.check(_order(), held_qty=Decimal(0))

    def test_gate_refuses_broker_without_paper_attribute(self):
        class Bare:
            def get_order_by_client_id(self, cid):
                return None

        gate = OrderGate(settings(ORDER_SUBMISSION_ENABLED="true"), Bare(), InMemoryPause())
        with self.assertRaises(LiveTradingForbiddenError):
            gate.check(_order(), held_qty=Decimal(0))


class E_NetworkGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_network_guard()

    def test_dns_lookup_of_live_hosts_blocked(self):
        for host in sorted(DENIED_LIVE_HOSTS) + ["API.Alpaca.Markets", "api.alpaca.markets."]:
            with self.subTest(host=host), self.assertRaises(LiveTradingForbiddenError):
                socket.getaddrinfo(host, 443)
        self.assertIn("api.alpaca.markets", [h.lower().rstrip(".") for h in blocked_attempts()])

    def test_gethostbyname_blocked(self):
        with self.assertRaises(LiveTradingForbiddenError):
            socket.gethostbyname("api.alpaca.markets")

    def test_create_connection_to_live_host_blocked(self):
        # socket.create_connection is what http.client, urllib3 and websockets use.
        with self.assertRaises(LiveTradingForbiddenError):
            socket.create_connection(("api.alpaca.markets", 443), timeout=2)

    def test_socket_connect_event_inspected(self):
        # A raw sock.connect((host, port)) resolves the name in C before the audit event
        # fires, so with no DNS (CI sandbox) it fails first. Exercise the hook directly
        # with the exact event the interpreter raises once DNS succeeds.
        from tradinglab.safety.network_guard import _audit_hook

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(LiveTradingForbiddenError):
                _audit_hook("socket.connect", (s, ("api.alpaca.markets", 443)))
            _audit_hook("socket.connect", (s, ("paper-api.alpaca.markets", 443)))  # allowed
        finally:
            s.close()

    def test_https_proxy_tunnel_target_inspected(self):
        # Through an HTTPS proxy, http.client connects to the proxy and tunnels to the real
        # host. The guard checks the tunnel target, not just the proxy address.
        import http.client

        conn = http.client.HTTPSConnection("127.0.0.1", 3128, timeout=1)
        conn.set_tunnel("api.alpaca.markets", 443)
        with self.assertRaises(LiveTradingForbiddenError):
            conn.connect()
        conn.close()

    def test_proxy_env_vars_refused_outside_tests(self):
        for app_env in ("production", "development"):
            for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
                with self.subTest(app_env=app_env, var=var), self.assertRaises(ConfigError):
                    load_settings(valid_env(APP_ENV=app_env, **{var: "http://proxy:3128"}))

    def test_unicode_lookalike_hosts_blocked(self):
        # IDNA maps these to api.alpaca.markets inside the socket layer, after the hook runs.
        for host in ("api\u3002alpaca\u3002markets", "api\uff0ealpaca\uff0emarkets",
                     "api\uff61alpaca\uff61markets", "api.alpaca\ufe52markets",
                     "api.alpaca.markets\u200b", "API\u3002Alpaca\u3002Markets"):
            with self.subTest(host=repr(host)), self.assertRaises(LiveTradingForbiddenError):
                socket.getaddrinfo(host, 443)

    def test_unicode_lookalike_urls_rejected(self):
        from tradinglab.safety.endpoints import validate_trading_url

        with self.assertRaises(LiveTradingForbiddenError):
            validate_trading_url("https://api\u3002alpaca\u3002markets")

    def test_unknown_alpaca_host_blocked(self):
        with self.assertRaises(EndpointNotAllowedError):
            socket.getaddrinfo("some-new-host.alpaca.markets", 443)

    def test_paper_and_unrelated_hosts_not_intercepted(self):
        for host in ("paper-api.alpaca.markets", "data.alpaca.markets", "localhost"):
            with self.subTest(host=host):
                try:
                    socket.getaddrinfo(host, 443)
                except (LiveTradingForbiddenError, EndpointNotAllowedError):
                    self.fail(f"guard wrongly blocked {host}")
                except OSError:
                    pass  # no network in CI sandbox is fine

    def test_urllib_request_to_live_host_blocked_direct(self):
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(LiveTradingForbiddenError):
            opener.open("https://api.alpaca.markets/v2/account", timeout=2)

    def test_urllib_request_to_live_host_blocked_via_proxy(self):
        import urllib.request

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": "http://127.0.0.1:3128"})
        )
        with self.assertRaises(LiveTradingForbiddenError):
            opener.open("https://api.alpaca.markets/v2/account", timeout=2)


class F_SourceCodeTests(unittest.TestCase):
    """Static checks over the source tree."""

    def _py_files(self):
        return [p for p in SRC.rglob("*.py")]

    def test_no_paper_false_anywhere(self):
        pat = re.compile(r"paper\s*=\s*(False|0\b|None|[\"']false)")
        offenders = [str(p) for p in self._py_files() if pat.search(p.read_text())]
        self.assertEqual(offenders, [])

    def test_live_host_strings_only_in_denylist(self):
        allowed = {SRC / "safety" / "endpoints.py"}
        pat = re.compile(r"(?<![-\w])(api|broker-api|live-api)\.alpaca\.markets")
        offenders = [
            str(p) for p in self._py_files() if p not in allowed and pat.search(p.read_text())
        ]
        self.assertEqual(offenders, [], "live hostnames must appear only in the denylist")

    def test_trading_client_constructed_only_in_adapter(self):
        allowed = {SRC / "broker" / "alpaca_paper.py"}
        pat = re.compile(r"TradingClient|alpaca\.trading")
        offenders = [
            str(p) for p in self._py_files() if p not in allowed and pat.search(p.read_text())
        ]
        self.assertEqual(offenders, [])

    def test_url_override_only_in_vetted_adapters(self):
        # The paper trading adapter (validated paper URL) and the read-only market-data
        # client (validated data host). Nowhere else may redirect an Alpaca client.
        allowed = {SRC / "broker" / "alpaca_paper.py", SRC / "marketdata" / "alpaca_data.py"}
        pat = re.compile(r"url_override\s*=")
        offenders = [
            str(p) for p in self._py_files() if p not in allowed and pat.search(p.read_text())
        ]
        self.assertEqual(offenders, [])
        data_src = (SRC / "marketdata" / "alpaca_data.py").read_text()
        self.assertIn("validate_data_url(url)", data_src)
        self.assertNotIn("submit_order", data_src)

    def test_no_legacy_or_broker_sdks(self):
        pat = re.compile(r"alpaca_trade_api|alpaca\.broker|BrokerClient|alpaca\.common"
                         r"|RESTClient|TRADING_LIVE|BaseURL")
        offenders = [str(p) for p in self._py_files() if pat.search(p.read_text())]
        self.assertEqual(offenders, [])

    def test_no_live_env_names_read(self):
        pat = re.compile(r"ALPACA_LIVE|LIVE_API_KEY|APCA_API_KEY_ID\"\s*\]")
        offenders = [str(p) for p in self._py_files() if pat.search(p.read_text())]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
