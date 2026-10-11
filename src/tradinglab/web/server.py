"""Trading Lab web service (stdlib only): dashboard + engine scheduler, for Railway.

Routes:
  GET  /healthz   public, "ok" while the server is up (Railway health check).
  GET  /          password-protected Overview: portfolio, positions, orders, trades,
                  latest decisions, engine status, events, and the system/safety panel.
  POST /control   password-protected: pause / resume / resume after drawdown halt.

Authentication: HTTP Basic over Railway's HTTPS, password from DASHBOARD_PASSWORD (min 12
characters); missing or short means the dashboard is locked (fail closed). Failed logins are
rate-limited per client. POSTs require a CSRF token and a same-origin Origin/Referer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import os
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
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
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "form-action 'self'; frame-ancestors 'none'"),
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
            if path != "/":
                return self._send(HTTPStatus.NOT_FOUND, "not found\n")
            if not self._authorised():
                return None
            return self._send(HTTPStatus.OK, render_page(state), "text/html; charset=utf-8")

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
            else:
                return self._send(HTTPStatus.BAD_REQUEST, "unknown action\n")
            log.warning("control action %r by %s", action, self._client())
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

    return Handler


# --- rendering ----------------------------------------------------------------------------

esc = html.escape


def _money(x: float, sign: bool = False) -> str:
    s = f"{x:+,.2f}" if sign else f"{x:,.2f}"
    return ("-$" + s[1:] if s.startswith("-") else ("+$" + s[1:] if s.startswith("+") else "$" + s))


def _pct(x: float, sign: bool = True) -> str:
    return f"{x:+.2%}" if sign else f"{x:.2%}"


def _cls(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


_REASONS = [  # internal reason code fragment -> plain English (first match wins per part)
    ("uptrend: close>slow & fast>slow", "Uptrend: price and short-term average above the long-term average"),
    ("no_uptrend: close<=sma_slow", "No uptrend: price below its long-term average"),
    ("no_uptrend: sma_fast<=sma_slow", "No uptrend: short-term average below the long-term average"),
    ("trend_end: sma_fast<sma_slow", "Trend ended: short-term average fell below the long-term average"),
    ("trend_end: close<sma_slow", "Trend ended: price fell below its long-term average"),
    ("trend_intact", "Trend intact, holding"),
    ("warmup", "Not enough price history yet"),
    ("sized_by=risk_budget", "size set by the per-trade risk limit"),
    ("sized_by=max_exposure", "size capped by the exposure limit"),
    ("sized_by=cash", "size capped by available cash"),
    ("sizing:max_open_positions", "Blocked: already at the maximum number of positions"),
    ("sizing:max_exposure_zero", "Blocked: exposure limit reached"),
    ("sizing:risk_budget_zero", "Blocked: one share would exceed the risk limit"),
    ("sizing:cash_zero", "Blocked: not enough cash"),
    ("sizing:invalid_stop", "Blocked: invalid stop level"),
    ("emergency_pause", "Blocked: emergency pause is on"),
    ("intraday_loss_halt", "Blocked: daily loss limit hit"),
    ("peak_drawdown_halt", "Blocked: drawdown limit hit"),
    ("drawdown_halt_latched", "Blocked: drawdown halt active until you resume"),
    ("symbol_not_configured", "Blocked: symbol no longer in your list"),
    ("stop_gap_at_open", "Gapped below stop at the open"),
    ("stop_hit", "Stop hit"),
    ("no_bar_today", "No price data for this day"),
    ("data_gap_at_open", "Cancelled: no price data at the open"),
    ("insufficient_cash_at_open", "Cancelled: not enough cash at the open"),
    ("no_new_orders", "No new orders"),
]
_ACTIONS = {"enter_long": "Buy signal", "exit": "Sell signal", "hold": "Hold",
            "no_trade": "No signal", "none": "—"}
_OUTCOMES = {"order_queued": "Order for next open", "rejected": "Blocked", "none": "—",
             "data_gap": "No data", "order_cancelled": "Cancelled"}


def humanize(reason: str) -> str:
    parts = []
    for part in reason.split(";"):
        part = part.strip()
        if part.startswith("open ") and "above limit" in part:
            parts.append("Not filled: opened above the limit price")
            continue
        for code, text in _REASONS:
            if part.startswith(code):
                parts.append(text)
                break
        else:
            parts.append(part.replace("_", " "))
    out = "; ".join(parts)
    return out[:1].upper() + out[1:] if out else out


def _short_day(d) -> str:
    return d.strftime("%b %d").replace(" 0", " ")


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str) -> str:
    if not rows:
        return f"<p class='muted'>{esc(empty)}</p>"
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _list(items: Sequence[tuple[str, str, str]], empty: str) -> str:
    """Phone-friendly rows: (title_html, right_html, detail_text)."""
    if not items:
        return f"<p class='muted'>{esc(empty)}</p>"
    rows = "".join(f"<li><div class='row'><span class='title'>{t}</span><span class='right'>{r}</span>"
                   f"</div>{f'<div class=detail>{esc(d)}</div>' if d else ''}</li>"
                   for t, r, d in items)
    return f"<ul class='list'>{rows}</ul>"


def _sparkline(values: Sequence[float]) -> str:
    if len(values) < 2:
        return "<p class='muted'>The equity chart appears after the first few sessions.</p>"
    w, h, pad = 600, 140, 6
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = " ".join(f"{pad + i * (w - 2 * pad) / (len(values) - 1):.1f},"
                   f"{h - pad - (v - lo) * (h - 2 * pad) / span:.1f}" for i, v in enumerate(values))
    return (f"<svg viewBox='0 0 {w} {h}' class='spark' role='img' aria-label='Equity curve'>"
            f"<polyline points='{pts}' fill='none' stroke='currentColor' stroke-width='2' "
            "stroke-linejoin='round'/></svg>"
            f"<div class='muted small spread'><span>{_money(lo)}</span><span>{_money(hi)}</span></div>")


def _fmt_time(state: AppState, dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(state.tz).strftime("%b %d %H:%M %Z")


def render_portfolio(state: AppState) -> str:
    store = state.store
    st = store.load_state()
    if st is None:
        return ("<section><h2>Portfolio</h2><p class='muted'>The simulated portfolio starts on the "
                "first evening run after a trading day settles (8:30 pm ET or later).</p></section>")
    curve = store.equity_curve()
    equity = curve[-1].equity if curve else st.equity()
    prev = curve[-2].equity if len(curve) > 1 else st.initial_capital
    total = equity - st.initial_capital
    day = equity - prev
    peak = max([p.equity for p in curve] + [st.initial_capital])
    dd = equity / peak - 1 if peak else 0.0
    mv = sum(p.shares * st.last_close.get(p.symbol, p.entry_ref) for p in st.positions.values())
    tiles = [
        ("Equity", _money(equity), ""),
        ("Total P&L", f"{_money(total, True)} ({_pct(total / st.initial_capital)})", _cls(total)),
        ("Last session", f"{_money(day, True)}", _cls(day)),
        ("Drawdown", _pct(dd, sign=False) if dd < 0 else "0.00%", "neg" if dd < 0 else ""),
        ("Cash", _money(st.cash), ""),
        ("Exposure", _pct(mv / equity if equity else 0, sign=False), ""),
    ]
    tiles_html = "".join(f"<div class='tile'><div class='muted small'>{esc(k)}</div>"
                         f"<div class='big {c}'>{esc(v)}</div></div>" for k, v, c in tiles)
    pos_rows = []
    for p in sorted(st.positions.values(), key=lambda p: p.symbol):
        last = st.last_close.get(p.symbol, p.entry_ref)
        pnl = (last - p.entry_price) * p.shares + p.dividends
        pos_rows.append([f"<b>{esc(p.symbol)}</b>", str(p.shares), _money(p.entry_price),
                         _money(last), f"<span class='{_cls(pnl)}'>{_money(pnl, True)}</span>",
                         _money(p.stop)])
    pend_items = [(f"<b>{esc(o.symbol)}</b> · {'Buy' if o.action == 'enter_long' else 'Sell'} "
                   f"{o.shares}",
                   (f"limit {_money(o.limit)} · stop {_money(o.stop)}" if o.limit and o.stop
                    else "at the open"),
                   humanize(o.reason)) for o in st.pending]
    trades = store.trades()[-10:][::-1]
    trade_items = [(f"<b>{esc(t.symbol)}</b> · {esc(_short_day(t.entry_day))} – "
                    f"{esc(_short_day(t.exit_day))} · {t.shares} sh",
                    f"<span class='{_cls(t.pnl)}'>{_money(t.pnl, True)}</span>",
                    humanize(t.exit_reason)) for t in trades]
    last_day = st.last_session
    decs = [d for d in store.recent_decisions(60) if d.day == last_day and d.symbol != "*"]
    dec_items = [(f"<b>{esc(d.symbol)}</b> · {esc(_ACTIONS.get(d.action, d.action.replace('_', ' ')))}",
                  esc(_OUTCOMES.get(d.outcome, d.outcome.replace("_", " "))),
                  humanize(d.reason)) for d in decs]
    return f"""
<section><h2>Portfolio</h2><div class='tiles'>{tiles_html}</div>
{_sparkline([p.equity for p in curve])}</section>
<section><h2>Open positions</h2>{_table(["", "Sh", "Entry", "Last", "P&L", "Stop"], pos_rows, "No open positions.")}</section>
<section><h2>Orders for the next open</h2>{_list(pend_items, "No orders queued.")}
<p class='muted small'>Filled at the next session's opening price, or cancelled if it opens above the limit.</p></section>
<section><h2>Latest decisions <span class='muted small'>({esc(last_day.strftime('%a %b %d, %Y') if last_day else '—')} close)</span></h2>{_list(dec_items, "No decisions yet.")}</section>
<section><h2>Recent trades</h2>{_list(trade_items, "No closed trades yet.")}</section>"""


def render_engine(state: AppState) -> str:
    rows = []
    paused = False
    latched = False
    if state.store is not None:
        try:
            paused = state.store.get_flag("paused")
            st = state.store.load_state()
            latched = bool(st and st.drawdown_halt_latched)
        except Exception as exc:  # noqa: BLE001
            rows.append(["Database", f"<span class='neg'>{esc(type(exc).__name__)}</span>"])
    sched = state.scheduler
    if sched is not None:
        s = sched.status
        rep = s.last_report
        rows.append(["Last check", esc(_fmt_time(state, s.last_check))])
        if rep is not None:
            detail = rep.status.replace("_", " ")
            if rep.processed:
                detail += f" · processed {', '.join(d.isoformat() for d in rep.processed)}"
            if rep.messages:
                detail += " · " + "; ".join(rep.messages)
            rows.append(["Result", esc(detail)])
        if s.last_error:
            rows.append(["Error", f"<span class='neg'>{esc(s.last_error)}</span>"])
    if state.engine_note:
        rows.append(["Note", esc(state.engine_note)])
    rows.append(["Emergency pause", "<span class='neg'>ON</span>" if paused else "off"])
    if latched:
        rows.append(["Drawdown halt", "<span class='neg'>ACTIVE — new entries stopped until you "
                     "resume</span>"])
    buttons = ""
    if state.controls is not None:
        def button(action: str, label: str, danger: bool = False) -> str:
            return (f"<form method='post' action='/control'><input type='hidden' name='csrf' "
                    f"value='{esc(state.csrf)}'><input type='hidden' name='action' "
                    f"value='{esc(action)}'><button class='{'danger' if danger else ''}'>"
                    f"{esc(label)}</button></form>")
        buttons = "<div class='buttons'>" + (
            button("resume", "Resume new entries") if paused
            else button("pause", "Pause new entries", danger=True))
        if latched:
            buttons += button("resume_drawdown", "Resume after drawdown halt")
        buttons += "</div><p class='muted small'>Pausing stops new positions; stops and exits keep running.</p>"
    body = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    return f"<section><h2>Engine</h2><table>{body}</table>{buttons}</section>"


def render_events(state: AppState) -> str:
    if state.store is None:
        return ""
    try:
        events = state.store.recent_events(10)[::-1]
    except Exception:  # noqa: BLE001
        return ""
    items = [(esc(e.kind.replace("_", " ").capitalize()), esc(_fmt_time(state, e.at)), e.detail)
             for e in events]
    return f"<section><h2>Activity log</h2>{_list(items, 'Nothing yet.')}</section>"


def render_system(state: AppState) -> str:
    rows = []
    if state.report:
        for name, passed, detail in state.report.checks:
            rows.append(f"<tr><td>{'✅' if passed else '❌'}</td><td>{esc(name)}</td>"
                        f"<td>{esc(detail)}</td></tr>")
    settings_html = ""
    if state.report:
        s, r = state.report.settings, state.report.settings.risk
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
    return f"""<section><details><summary>System &amp; safety</summary>
<h3>Settings</h3><table>{settings_html or "<tr><td>Configuration not loaded</td></tr>"}</table>
<h3>Safety self-check</h3><table>{''.join(rows) or "<tr><td>Not run</td></tr>"}</table>
</details></section>"""


def render_page(state: AppState) -> str:
    banner = ("<p class='ok'>Safety self-check passed. No brokerage is reachable.</p>" if state.ok
              else f"<p class='bad'>Not ready: {esc(state.error or 'self-check failed')}</p>")
    portfolio = ""
    if state.store is not None:
        try:
            portfolio = render_portfolio(state)
        except Exception as exc:  # noqa: BLE001 - never 500 the dashboard
            log.exception("portfolio render failed")
            portfolio = (f"<section><h2>Portfolio</h2><p class='bad'>Could not load data "
                         f"({esc(type(exc).__name__)}).</p></section>")
    else:
        portfolio = ("<section><h2>Portfolio</h2><p class='muted'>Attach the PostgreSQL database "
                     "(DATABASE_URL) to start the simulation.</p></section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading Lab</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--line:#e3e3de;--ok:#1d7a46;--bad:#b3261e;--accent:#2f5bd3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ececea;--muted:#9a9a94;--card:#1f1f1d;--line:#33332f;--ok:#5cc489;--bad:#ff8a80;--accent:#8fb0ff}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 48px}}
h1{{font-size:22px;margin:0}} h2{{font-size:15px;margin:28px 0 10px;font-weight:650}} h3{{font-size:14px;margin:18px 0 8px;color:var(--muted)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th{{text-align:left;font-weight:600;color:var(--muted);font-size:13px}}
td,th{{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}}
.scroll td,.scroll th{{font-size:14px;white-space:nowrap;padding:8px}}
.list{{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:10px}}
.list li{{padding:10px 12px;border-top:1px solid var(--line)}} .list li:first-child{{border-top:0}}
.row{{display:flex;justify-content:space-between;gap:12px;align-items:baseline}}
.right{{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--muted);font-size:14px}}
.right .pos,.right .neg{{font-weight:600}}
.detail{{color:var(--muted);font-size:13px;margin-top:2px}}
thead tr th{{border-top:0}} tbody tr:first-child td{{border-top:1px solid var(--line)}} table:not(:has(thead)) tr:first-child td{{border-top:0}}
.scroll{{overflow-x:auto}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.big{{font-size:19px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}}
.spark{{width:100%;height:140px;color:var(--accent);background:var(--card);border:1px solid var(--line);border-radius:10px}}
.spread{{display:flex;justify-content:space-between}}
.ok,.pos{{color:var(--ok)}} .bad,.neg{{color:var(--bad)}} .ok,.bad{{font-weight:600}}
.muted{{color:var(--muted)}} .small{{font-size:13px}}
.buttons{{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}}
button{{font:inherit;padding:10px 14px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--fg);min-height:44px}}
button.danger{{border-color:var(--bad);color:var(--bad)}}
summary{{cursor:pointer;font-weight:650}}
</style></head><body><main>
<h1>Trading Lab</h1><p class="muted small">Simulation only · no brokerage connection · {esc(datetime.now(UTC).astimezone(state.tz).strftime('%b %d %H:%M %Z'))}</p>
{banner}
{portfolio}
{render_engine(state)}
{render_events(state)}
{render_system(state)}
</main></body></html>"""


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
    sched = Scheduler(lambda: build_engine(settings, postgres_store(settings)))
    state.scheduler = sched
    sched.start()
    return state


def serve(env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    state = build_state(env)
    port = int(env.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))  # noqa: S104
    log.info("Trading Lab web service listening on port %d", port)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
