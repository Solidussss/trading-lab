"""Trading Lab web service (stdlib only): dashboard + engine scheduler, for Railway.

Routes:
  GET  /healthz   public, "ok" while the server is up (Railway health check).
  GET  /          password-protected plain-English briefing (see web/views.py).
  POST /control   password-protected: pause / resume / resume after drawdown halt.

Authentication: HTTP Basic over Railway's HTTPS, password from DASHBOARD_PASSWORD (min 12
characters); missing or short means the dashboard is locked (fail closed). Failed logins are
rate-limited per client. POSTs require a CSRF token and a same-origin Origin/Referer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import sys
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from zoneinfo import ZoneInfo

from tradinglab.safety.selfcheck import SafetyCheckFailedError, SelfCheckReport, run_selfcheck

log = logging.getLogger(__name__)

MIN_PASSWORD_LEN = 12
MAX_FAILURES = 10
FAILURE_WINDOW_S = 15 * 60
MAX_BODY = 4096
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": ("default-src 'none'; img-src 'self'; "
                                "style-src 'unsafe-inline' https://fonts.googleapis.com; "
                                "font-src https://fonts.gstatic.com; "
                                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    "Strict-Transport-Security": "max-age=31536000",
}


class AppState:
    """Everything the web layer needs: self-check result, auth, and (optionally) the engine."""

    def __init__(self, env: Mapping[str, str], *, freeze: bool = True, system_proxies=None,
                 store: Any = None, controls: Any = None, scheduler: Any = None) -> None:
        self.env = env
        self.report: SelfCheckReport | None = None
        self.error: str | None = None
        kwargs: dict[str, Any] = {"freeze": freeze}
        if system_proxies is not None:
            kwargs["system_proxies"] = system_proxies
        try:
            self.report = run_selfcheck(env, **kwargs)
        except SafetyCheckFailedError as exc:
            self.error = str(exc)
        password = env.get("DASHBOARD_PASSWORD", "")
        self.password = password if len(password) >= MIN_PASSWORD_LEN else None
        self.csrf = (hmac.new(password.encode(), b"tradinglab-csrf", hashlib.sha256).hexdigest()
                     if self.password else "")
        self.tz = ZoneInfo(env.get("DISPLAY_TZ", "America/Edmonton"))
        self.store = store  # read access for the dashboard (None until a database exists)
        self.controls = controls  # LiveSimulationEngine used for pause/resume
        self.scheduler = scheduler
        self.engine_note = ""
        self.hype: Any = None  # HypeRunner for the memecoin paper bot (None when off)
        self.intraday: Any = None  # IntradayRunner, for the dashboard's "today so far"
        self.inplay: Any = None  # InPlayRunner: the stocks-in-play bots
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @property
    def ok(self) -> bool:
        return self.report is not None and self.report.ok

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

        def log_message(self, fmt, *args):
            log.info("%s %s", self._client(), fmt % args)

        def _client(self) -> str:
            fwd = self.headers.get("X-Forwarded-For", "") if hasattr(self, "headers") else ""
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

        def _authorised(self) -> bool:
            if state.password is None:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                           "Dashboard locked: set DASHBOARD_PASSWORD (at least "
                           f"{MIN_PASSWORD_LEN} characters) in Railway variables.\n")
                return False
            client = self._client()
            if state.too_many_failures(client):
                self._send(HTTPStatus.TOO_MANY_REQUESTS, "too many attempts; try later\n",
                           extra={"Retry-After": str(FAILURE_WINDOW_S)})
                return False
            if not state.check_password(self.headers.get("Authorization")):
                if self.headers.get("Authorization"):
                    state.record_failure(client)
                self._send(HTTPStatus.UNAUTHORIZED, "login required\n",
                           extra={"WWW-Authenticate": 'Basic realm="Trading Lab"'})
                return False
            return True

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(HTTPStatus.OK, "ok\n")
            pages = {"/": "overview_page", "/stocks": "stocks_page", "/tests": "tests_page",
                     "/controls": "controls_page"}
            if path not in pages and path != "/hype" and not path.startswith("/bot/"):
                return self._send(HTTPStatus.NOT_FOUND, "not found\n")
            if not self._authorised():
                return None
            if path == "/hype":
                from tradinglab.web.views_hype import hype_page

                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                account = (query.get("a") or [""])[0][:40]
                return self._send(HTTPStatus.OK, hype_page(state, account=account),
                                  "text/html; charset=utf-8")
            if path.startswith("/bot/"):
                from tradinglab.web.views_bots import bot_page

                page = bot_page(state, path[5:])
                if page is None:
                    return self._send(HTTPStatus.NOT_FOUND, "not found\n")
                return self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            if path == "/":
                return self._send(HTTPStatus.OK, render_page(state), "text/html; charset=utf-8")
            from tradinglab.web import views_bots

            return self._send(HTTPStatus.OK, getattr(views_bots, pages[path])(state),
                              "text/html; charset=utf-8")

        def do_POST(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/control":
                return self._send(HTTPStatus.NOT_FOUND, "not found\n")
            if not self._authorised():
                return None
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if not origin or urllib.parse.urlsplit(origin).netloc != host:
                return self._send(HTTPStatus.FORBIDDEN, "cross-origin request refused\n")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too large\n")
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            token = (form.get("csrf") or [""])[0]
            if not state.csrf or not hmac.compare_digest(token, state.csrf):
                return self._send(HTTPStatus.FORBIDDEN, "invalid form token\n")
            action = (form.get("action") or [""])[0]
            if state.controls is None:
                return self._send(HTTPStatus.CONFLICT, "engine not running yet\n")
            if action == "pause":
                state.controls.pause()
            elif action == "resume":
                state.controls.resume()
            elif action == "resume_drawdown":
                state.controls.resume_after_drawdown()
            elif action.startswith("resume_bot:") and action[11:] in _runners(state):
                _runners(state)[action[11:]].resume_bot(action[11:])
            elif action.startswith("unlock_test:") and state.intraday is not None \
                    and action[12:] in {b.id for b in state.intraday.bots}:  # three-fund bots only
                state.store.set_flag(action, True)
                state.store.add_event(_event("final_test_opened", action[12:]))
            else:
                return self._send(HTTPStatus.BAD_REQUEST, "unknown action\n")
            log.warning("control action %r by %s", action, self._client())
            self.send_response(HTTPStatus.SEE_OTHER)
            ref = urllib.parse.urlsplit(self.headers.get("Referer") or "")
            back = ref.path if ref.netloc == host and (
                ref.path.startswith("/bot/") or ref.path == "/controls") else "/"
            self.send_header("Location", back)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

    return Handler


# --- rendering ----------------------------------------------------------------------------


def render_page(state: AppState) -> str:
    from tradinglab.web.views_bots import overview_page

    return overview_page(state)


def _runners(state: AppState) -> dict[str, Any]:
    """Bot id -> the runner that owns it (three-fund bots and stocks-in-play bots)."""
    out: dict[str, Any] = {}
    for runner in (state.intraday, getattr(state, "inplay", None)):
        if runner is not None:
            out.update({b.id: runner for b in runner.bots})
    return out


def _event(kind: str, detail: str):
    from datetime import UTC, datetime

    from tradinglab.engine.store import EngineEvent

    return EngineEvent(datetime.now(UTC), None, kind, detail)


# --- process entry point ------------------------------------------------------------------


def build_state(env: Mapping[str, str]) -> AppState:
    """Self-check, then (if configured) attach the database and start the scheduler."""
    state = AppState(env)
    if not state.ok:
        log.error("Self-check failed; simulation stays off: %s", state.error)
        return state
    settings = state.report.settings
    if settings.database_url is None:
        state.engine_note = "Waiting for DATABASE_URL (Railway PostgreSQL)."
        return state
    if settings.data_token is None:
        state.engine_note = "Waiting for TIINGO_API_TOKEN."
        return state
    from tradinglab.engine.runtime import Scheduler, build_engine, postgres_store

    try:
        state.store = postgres_store(settings)
        state.controls = build_engine(settings, state.store)
    except Exception as exc:  # noqa: BLE001 - show it on the dashboard, keep serving
        log.exception("database unavailable")
        state.store = None
        state.engine_note = f"Database unavailable at startup: {type(exc).__name__}: {exc}"
        return state
    from tradinglab.engine.runtime import ResearchJob, build_inplay, build_intraday

    state.intraday = build_intraday(settings, state.store)
    state.inplay = build_inplay(settings, state.store)
    start_hype(state, settings, env)

    def extra_jobs():
        store = postgres_store(settings)
        runner = build_intraday(settings, store)
        research = ResearchJob(runner, TiingoClient(settings.require_data_token()))
        # The three-fund bots run first, so they always get the data requests they need.
        return [("day-trading bots", runner.run),
                ("stocks in play", build_inplay(settings, store).run),
                ("research", research.check)]

    from tradinglab.marketdata.tiingo import TiingoClient

    sched = Scheduler(lambda: build_engine(settings, postgres_store(settings)), extra_jobs=extra_jobs)
    state.scheduler = sched
    sched.start()
    return state


def start_hype(state: AppState, settings: Any, env: Mapping[str, str]) -> None:
    """Start the hype paper bots (24/7), one per rule-set variant, each on its own pretend
    bankroll. HYPE_BOT=off disables them. Never blocks the stock bots."""
    if env.get("HYPE_BOT", "on").strip().lower() in {"off", "0", "false", "no"}:
        return
    try:
        from dataclasses import replace

        from tradinglab.engine.runtime import postgres_store
        from tradinglab.hype.bot import HypeBot, HypeRunner
        from tradinglab.hype.dexscreener import DexScreenerClient
        from tradinglab.hype.feeds import SharedFeed
        from tradinglab.hype.geckoterminal import GeckoTerminalClient
        from tradinglab.hype.model import VARIANTS
        from tradinglab.hype.store import PgHypeStore

        bankroll = float(env.get("HYPE_BANKROLL", "1000"))
        stake = float(env.get("HYPE_STAKE", "50"))
        variants = [replace(v, rules=replace(v.rules, bankroll=bankroll, stake=stake)) for v in VARIANTS]
        gecko, dex = GeckoTerminalClient(), DexScreenerClient()
        # New coins: GeckoTerminal first. Open positions: DexScreener first, so a rate-limited
        # scan feed can't stop the stops from being checked. One feed serves every variant.
        feed = SharedFeed(discover=(gecko, dex), price=(dex, gecko), trades=gecko, early=dex)

        def bots() -> list[HypeBot]:
            pg = postgres_store(settings)  # the bot thread's own connection
            return [HypeBot(feed, PgHypeStore(pg, v.id), v.rules, buys_enabled=v.buys_enabled, name=v.id)
                    for v in variants]

        # the pages read each account through the dashboard's connection
        from tradinglab.hype.scout import Scout

        runner = HypeRunner(bots, variants=[(v, PgHypeStore(state.store, v.id)) for v in variants],
                            feed=feed,
                            scout_factory=lambda: Scout(feed, PgHypeStore(postgres_store(settings), "scout")),
                            scout_store=PgHypeStore(state.store, "scout"))
        state.hype = runner
        runner.start()
        log.info("hype paper bots started: %s ($%.0f bankroll each, $%.0f per coin)",
                 ", ".join(v.id if v.buys_enabled else f"{v.id} (no new buys)" for v in variants),
                 bankroll, stake)
    except Exception:  # noqa: BLE001 - the stock bots must keep running regardless
        log.exception("hype bot failed to start")


def serve(env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state = build_state(env)
    port = int(env.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))  # noqa: S104
    log.info("Trading Lab web service listening on port %d", port)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
