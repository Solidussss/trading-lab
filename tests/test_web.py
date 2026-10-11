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
        self.assertIn("Safety self-check passed", body)
        self.assertIn("SPY, QQQ, IWM", body)
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
        self.assertIn("Not ready", body)
        self.assertIn("TRADING_MODE", body)

    def test_unknown_path_404(self):
        port, _ = self.start()
        self.assertEqual(self.get(port, "/admin")[0], 404)


if __name__ == "__main__":
    unittest.main()
