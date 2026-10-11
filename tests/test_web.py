import base64
import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer

from tests.helpers import FAKE_TIINGO_TOKEN, valid_env
from tradinglab.web.server import MAX_FAILURES, AppState, make_handler

PASSWORD = "correct-horse-battery"


class WebTests(unittest.TestCase):
    def start(self, **env):
        base = valid_env(DASHBOARD_PASSWORD=PASSWORD, TIINGO_API_TOKEN=FAKE_TIINGO_TOKEN)
        base.update(env)
        state = AppState(base, freeze=False, system_proxies=dict)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1], state

    def get(self, port, path="/", password=None, raw_auth=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {}
        if password is not None:
            headers["Authorization"] = "Basic " + base64.b64encode(
                f"me:{password}".encode()).decode()
        if raw_auth is not None:
            headers["Authorization"] = raw_auth
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        body = r.read().decode()
        conn.close()
        return r.status, body, dict(r.getheaders())

    def test_healthz_is_public(self):
        port, _ = self.start()
        self.assertEqual(self.get(port, "/healthz")[:2], (200, "ok\n"))

    def test_status_requires_login(self):
        port, _ = self.start()
        status, _, headers = self.get(port)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.get(port, password="wrong-password-123")[0], 401)
        self.assertEqual(self.get(port, raw_auth="Basic !!!notbase64")[0], 401)

    def test_status_page_with_password_hides_secrets(self):
        port, state = self.start()
        status, body, headers = self.get(port, password=PASSWORD)
        self.assertEqual(status, 200)
        self.assertIn("Safety checks passed", body)
        self.assertIn("Getting ready", body)
        self.assertNotIn(FAKE_TIINGO_TOKEN, body)
        self.assertNotIn(PASSWORD, body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_locked_without_strong_password(self):
        for pw in (None, "short"):
            env = {} if pw is None else {"DASHBOARD_PASSWORD": pw}
            port, state = self.start(**env) if pw else self._start_without_password()
            self.assertEqual(self.get(port)[0], 503)

    def _start_without_password(self):
        state = AppState(valid_env(), freeze=False, system_proxies=dict)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1], state

    def test_failed_logins_rate_limited(self):
        port, _ = self.start()
        for _ in range(MAX_FAILURES):
            self.assertEqual(self.get(port, password="nope-nope-nope")[0], 401)
        self.assertEqual(self.get(port, password=PASSWORD)[0], 429)  # locked out for a while

    def test_bad_config_shows_reason_but_never_crashes(self):
        port, state = self.start(TRADING_MODE="live")
        self.assertIsNotNone(state.error)
        status, body, _ = self.get(port, password=PASSWORD)
        self.assertEqual(status, 200)
        self.assertIn("a safety check failed", body)
        self.assertIn("TRADING_MODE", body)

    def test_unknown_path_404(self):
        port, _ = self.start()
        self.assertEqual(self.get(port, "/admin")[0], 404)


if __name__ == "__main__":
    unittest.main()


class DashboardTests(unittest.TestCase):
    """Overview rendering with a real simulated portfolio, controls, and scheduler."""

    @classmethod
    def setUpClass(cls):
        from datetime import date

        from tests.test_live_engine import FlakySource, DATA, evening
        from tradinglab.engine.live import LiveSimulationEngine
        from tradinglab.engine.store import InMemoryStore
        from tradinglab.marketdata.calendar import trading_days
        from tradinglab.strategies.trend_following import TrendFollowing
        from tests.helpers import settings

        cls.store = InMemoryStore()
        cls.engine = LiveSimulationEngine(settings(), cls.store, FlakySource(DATA),
                                          TrendFollowing.create())
        for d in trading_days(date(2019, 1, 2), date(2019, 9, 30)):
            cls.engine.run(evening(d))

    def start(self, **kw):
        base = valid_env(DASHBOARD_PASSWORD=PASSWORD, TIINGO_API_TOKEN=FAKE_TIINGO_TOKEN)
        state = AppState(base, freeze=False, system_proxies=dict, store=self.store,
                         controls=self.engine, **kw)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1], state

    def post(self, port, body, origin="same", password=PASSWORD):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Authorization": "Basic " + base64.b64encode(f"me:{password}".encode()).decode()}
        if origin == "same":
            headers["Origin"] = f"http://127.0.0.1:{port}"
        elif origin:
            headers["Origin"] = origin
        conn.request("POST", "/control", body=body, headers=headers)
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status

    def test_overview_explains_in_plain_english(self):
        port, _ = self.start()
        status, body, headers = WebTests.get(self, port, password=PASSWORD)
        self.assertEqual(status, 200)
        for text in ("Your money", "Your three funds", "What it", "How the bot works",
                     "Pause new buys", "Safety checks passed", "simply bought SPY",
                     "50-day average", "200-day average", "<svg", "S&amp;P 500"):
            self.assertIn(text, body)
        st = self.store.load_state()
        self.assertIn(f"Based on {st.last_session.strftime('%a %b')} {st.last_session.day}", body)
        for jargon in ("sma_fast", "sma_slow", "exposure", "drawdown", "P&amp;L", "sized_by"):
            self.assertNotIn(jargon, body)
        for sym in st.positions:
            self.assertIn(f"Holding {st.positions[sym].shares} shares", body)
        for o in st.pending:
            self.assertIn(f"{o.shares} shares when the market opens", body)
        trade = self.store.trades()[-1]
        self.assertIn(f"Sold {trade.shares} {trade.symbol}", body)
        self.assertIn("https://fonts.gstatic.com", headers["Content-Security-Policy"])
        self.assertNotIn("script-src", headers["Content-Security-Policy"])

    def test_reasons_are_translated(self):
        from tradinglab.web.views import why

        self.assertEqual(why("uptrend: close>slow & fast>slow; sized_by=risk_budget"),
                         "it's in an uptrend")
        self.assertIn("safety stop", why("stop_hit"))
        self.assertIn("above the price limit", why("open 101.5 above limit 101.0"))

    def test_controls_pause_and_resume_with_csrf(self):
        port, state = self.start()
        self.assertEqual(self.post(port, f"action=pause&csrf={state.csrf}"), 303)
        self.assertTrue(self.store.get_flag("paused"))
        _, body, _ = WebTests.get(self, port, password=PASSWORD)
        self.assertIn("Resume buying", body)
        self.assertIn("Paused by you", body)
        self.assertEqual(self.post(port, f"action=resume&csrf={state.csrf}"), 303)
        self.assertFalse(self.store.get_flag("paused"))

    def test_controls_reject_forgery(self):
        port, state = self.start()
        good = f"action=pause&csrf={state.csrf}"
        self.assertEqual(self.post(port, "action=pause&csrf=wrong"), 403)
        self.assertEqual(self.post(port, good, origin=None), 403)
        self.assertEqual(self.post(port, good, origin="https://evil.example"), 403)
        self.assertEqual(self.post(port, good, password="not-the-password!"), 401)
        self.assertEqual(self.post(port, f"action=selfdestruct&csrf={state.csrf}"), 400)
        self.assertFalse(self.store.get_flag("paused"))

    def test_empty_portfolio_message(self):
        from tradinglab.engine.store import InMemoryStore

        state = AppState(valid_env(DASHBOARD_PASSWORD=PASSWORD), freeze=False,
                         system_proxies=dict, store=InMemoryStore())
        from tradinglab.web.server import render_page

        self.assertIn("Getting ready", render_page(state))

    def test_build_state_waits_for_database(self):
        from tradinglab.web import server

        orig = server.AppState
        server.AppState = lambda env: orig(env, freeze=False, system_proxies=dict)
        try:
            state = server.build_state(valid_env(DASHBOARD_PASSWORD=PASSWORD,
                                                 TIINGO_API_TOKEN=FAKE_TIINGO_TOKEN))
        finally:
            server.AppState = orig
        self.assertIsNone(state.store)
        self.assertIn("DATABASE_URL", state.engine_note)


class SchedulerTests(unittest.TestCase):
    def test_check_once_records_report_and_recovers_from_errors(self):
        from datetime import UTC, datetime

        from tradinglab.engine.live import RunReport
        from tradinglab.engine.runtime import Scheduler

        calls = {"built": 0, "runs": 0}

        class FakeEngine:
            def run(self, now):
                calls["runs"] += 1
                if calls["runs"] == 2:
                    raise ConnectionError("db went away")
                return RunReport("up_to_date", None)

        def factory():
            calls["built"] += 1
            return FakeEngine()

        s = Scheduler(factory, clock=lambda: datetime(2026, 10, 10, tzinfo=UTC))
        self.assertEqual(s.check_once().status, "up_to_date")
        self.assertIsNone(s.check_once())
        self.assertIn("db went away", s.status.last_error)
        self.assertEqual(s.check_once().status, "up_to_date")  # rebuilt after the error
        self.assertEqual(calls["built"], 2)
        self.assertIsNone(s.status.last_error)
