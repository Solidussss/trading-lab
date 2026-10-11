"""Minimal web service for Railway (stdlib only), until the full dashboard lands.

Routes:
  GET /healthz   public, returns "ok" while the server is up (Railway health check).
  GET /          password-protected status page: safety self-check results, settings
                 summary (never secrets), and what is configured.

Authentication: HTTP Basic over Railway's HTTPS, password from DASHBOARD_PASSWORD
(min 12 characters). If it is missing or too short, the page is not served at all (fail
closed). Failed logins are rate-limited per client address.
"""

from __future__ import annotations

import base64
import hmac
import html
import logging
import os
import threading
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tradinglab.safety.selfcheck import SafetyCheckFailedError, SelfCheckReport, run_selfcheck

log = logging.getLogger(__name__)

MIN_PASSWORD_LEN = 12
MAX_FAILURES = 10
FAILURE_WINDOW_S = 15 * 60
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Strict-Transport-Security": "max-age=31536000",
}


class AppState:
    """Self-check result computed once at startup (and shown on the status page)."""

    def __init__(self, env: Mapping[str, str], *, freeze: bool = True,
                 system_proxies=None) -> None:
        self.env = env
        self.report: SelfCheckReport | None = None
        self.error: str | None = None
        kwargs = {"freeze": freeze}
        if system_proxies is not None:
            kwargs["system_proxies"] = system_proxies
        try:
            self.report = run_selfcheck(env, **kwargs)
        except SafetyCheckFailedError as exc:
            self.error = str(exc)
        password = env.get("DASHBOARD_PASSWORD", "")
        self.password = password if len(password) >= MIN_PASSWORD_LEN else None
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.started = time.time()

    def too_many_failures(self, client: str) -> bool:
        with self._lock:
            q = self._failures[client]
            now = time.monotonic()
            while q and now - q[0] > FAILURE_WINDOW_S:
                q.popleft()
            return len(q) >= MAX_FAILURES

    def record_failure(self, client: str) -> None:
        with self._lock:
            self._failures[client].append(time.monotonic())

    def check_password(self, header: str | None) -> bool:
        if not self.password or not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, _, supplied = decoded.partition(":")
        return hmac.compare_digest(supplied.encode(), self.password.encode())


def make_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingLab"
        sys_version = ""

        def log_message(self, fmt, *args):  # route to logging, never log headers
            log.info("%s %s", self.address_string(), fmt % args)

        def _client(self) -> str:
            fwd = self.headers.get("X-Forwarded-For", "")
            return fwd.split(",")[0].strip() or self.client_address[0]

        def _send(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8",
                  extra: Mapping[str, str] | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(HTTPStatus.OK, "ok\n")
            if path != "/":
                return self._send(HTTPStatus.NOT_FOUND, "not found\n")
            if state.password is None:
                return self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                                  "Dashboard locked: set DASHBOARD_PASSWORD (at least "
                                  f"{MIN_PASSWORD_LEN} characters) in Railway variables.\n")
            client = self._client()
            if state.too_many_failures(client):
                return self._send(HTTPStatus.TOO_MANY_REQUESTS, "too many attempts; try later\n",
                                  extra={"Retry-After": str(FAILURE_WINDOW_S)})
            if not state.check_password(self.headers.get("Authorization")):
                if self.headers.get("Authorization"):
                    state.record_failure(client)
                return self._send(HTTPStatus.UNAUTHORIZED, "login required\n",
                                  extra={"WWW-Authenticate": 'Basic realm="Trading Lab"'})
            return self._send(HTTPStatus.OK, render_status(state), "text/html; charset=utf-8")

    return Handler


def render_status(state: AppState) -> str:
    esc = html.escape
    rows = []
    ok = state.report is not None and state.report.ok
    if state.report:
        for name, passed, detail in state.report.checks:
            rows.append(f"<tr><td>{'✅' if passed else '❌'}</td><td>{esc(name)}</td>"
                        f"<td>{esc(detail)}</td></tr>")
    settings_html = ""
    if state.report:
        s = state.report.settings
        r = s.risk
        items = [
            ("Mode", s.trading_mode), ("Symbols", ", ".join(s.symbols)),
            ("Starting capital", f"${s.capital_base_usd:,.0f}"),
            ("Risk per trade", f"{r.risk_per_trade_pct:.2%}"),
            ("Max exposure", f"{r.max_exposure_pct:.0%}"),
            ("Max open positions", str(r.max_open_positions)),
            ("Halt: daily loss / drawdown",
             f"{r.intraday_loss_halt_pct:.0%} / {r.peak_drawdown_halt_pct:.0%}"),
            ("Market data token", "configured" if s.data_token else "not set (TIINGO_API_TOKEN)"),
            ("Database", "configured" if s.database_url else "not attached yet"),
            ("Claude research key", "configured" if s.anthropic_key else "not set (optional)"),
        ]
        settings_html = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in items)
    banner = ("<p class='ok'>Safety self-check passed. No brokerage is reachable.</p>" if ok else
              f"<p class='bad'>Not ready: {esc(state.error or 'self-check failed')}</p>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading Lab</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--line:#e3e3de;--ok:#1d7a46;--bad:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ececea;--muted:#9a9a94;--card:#1f1f1d;--line:#33332f;--ok:#5cc489;--bad:#ff8a80}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:720px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 8px;color:var(--muted);font-weight:600}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
td{{padding:9px 12px;border-top:1px solid var(--line);vertical-align:top;word-break:break-word}}
tr:first-child td{{border-top:0}} td:first-child{{white-space:nowrap}}
.ok{{color:var(--ok);font-weight:600}} .bad{{color:var(--bad);font-weight:600}} .muted{{color:var(--muted)}}
</style></head><body><main>
<h1>Trading Lab</h1><p class="muted">Simulation only · no brokerage connection</p>
{banner}
<h2>Settings</h2><table>{settings_html or "<tr><td>Configuration not loaded</td></tr>"}</table>
<h2>Safety self-check</h2><table>{''.join(rows) or "<tr><td>Not run</td></tr>"}</table>
<h2>Engine</h2><table><tr><td>Status</td><td>Simulation engine built; database and nightly job are the next deploy.</td></tr></table>
</main></body></html>"""


def serve(env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state = AppState(env)
    if state.error:
        log.error("Self-check failed; trading functions stay disabled: %s", state.error)
    port = int(env.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))  # noqa: S104
    log.info("Trading Lab web service listening on port %d", port)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
