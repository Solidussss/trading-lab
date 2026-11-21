"""The hype bot's page: the rule sets side by side, then the chosen one in detail (portfolio
value and chart, open positions with mini charts, the live 'hot right now' list, trade history).
Rendered through the shared shell (web/theme.py); server-side HTML and inline SVG only."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from tradinglab.hype.bot import equity
from tradinglab.hype.model import Account, Rules, sell_fill
from tradinglab.hype.scout import Scout, summary
from tradinglab.web import theme

esc = html.escape

# Scoped under .hy so the coin cards can keep their own compact layout inside the shared look.
CSS = """
.hy{max-width:880px;margin:0 auto}
.hy p{margin:0}
.hy .vs{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:16px 0 6px}
.hy .v{display:block;text-decoration:none;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.hy .v.on{border-color:var(--txt)} .hy .v:hover{border-color:var(--mut)}
.hy .v b{display:block;font-weight:500} .hy .v small{color:var(--mut);font-size:12px;display:block}
.hy .v .n{font:600 20px/1.4 var(--mono)}
.hy .live{display:flex;align-items:center;gap:2px;font-size:12px;color:var(--mut);margin:16px 0 0}
.hy .hero{display:block;margin:10px 0 0}
.hy .lbl{display:inline;margin:0;text-transform:none;letter-spacing:0;font-size:12px}
.hy .value{font-size:40px;margin:2px 0 8px}
.hy .chip{display:inline-flex;font-size:13px;font-weight:600;padding:4px 9px;border-radius:6px;margin:0}
.hy .chip.up{background:var(--upbg);color:var(--up)} .hy .chip.dn{background:var(--dnbg);color:var(--dn)}
.hy .split{display:flex;gap:16px;margin:10px 0 0;font-size:12px;color:var(--mut)} .hy .split b{color:var(--txt);font-weight:500}
.hy .chart{margin:16px 0 6px} .hy .chart svg{display:block;width:100%;height:170px}
.hy .range{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}
.hy .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:16px 0 8px}
.hy .tile{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 12px}
.hy .tile .tv{font-size:17px;font-weight:600;margin:2px 0}
.hy h2{display:flex;justify-content:space-between;align-items:baseline;margin:26px 0 10px}
.hy h2 span{text-transform:none;letter-spacing:0;color:var(--dim)}
.hy .pos{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px;margin-bottom:9px;display:block;text-decoration:none}
.hy .row{display:flex;align-items:center;gap:11px;padding:0;border:0}
.hy .av{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:15px;flex:none;color:#0b0c0e}
.hy .nm{flex:1;min-width:0} .hy .nm b{font-size:15px;font-weight:500} .hy .nm small{display:block;font-size:12px;color:var(--mut)}
.hy .spark{width:84px;height:34px;flex:none}
.hy .pl{text-align:right;min-width:78px} .hy .pl b{font-size:15px;display:block;font-weight:500} .hy .pl small{font-size:12px}
.hy .bar{position:relative;height:6px;border-radius:3px;background:var(--panel2);margin:12px 0 5px;overflow:hidden}
.hy .bar i{position:absolute;top:0;bottom:0;border-radius:3px}
.hy .bar u{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--txt)}
.hy .ends{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}
.hy .why{font-size:12px;color:var(--mut);margin-top:8px}
.hy .tag{font:600 10px/1 var(--mono);padding:2px 6px;border-radius:4px;background:var(--panel2);color:var(--amber)}
.hy .list{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.hy .li{display:flex;align-items:center;gap:11px;padding:11px 13px;border-top:1px solid var(--line);text-decoration:none}
.hy .li:first-child{border-top:0} .hy .li .av{width:32px;height:32px;font-size:13px}
.hy .meter{width:46px;height:5px;border-radius:3px;background:var(--panel2);overflow:hidden;margin-top:5px}
.hy .meter i{display:block;height:100%}
.hy .cells{display:flex;gap:14px;font-size:12px;text-align:right} .hy .cells div{min-width:44px}
.hy .cells small{display:block;color:var(--dim);font-size:10px}
.hy .pill{font:600 10px/1 var(--mono);padding:4px 7px;border-radius:4px;white-space:nowrap}
.hy .pill.buy{background:var(--upbg);color:var(--up)} .hy .pill.skip{background:var(--panel2);color:var(--dim)}
.hy .reason{font-size:11px;color:var(--dim);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hy .empty{background:var(--panel);border:1px dashed var(--line);border-radius:10px;margin-top:16px}
.hy details ul{margin:0;padding:0 16px 14px 34px;color:var(--mut);font-size:13px} .hy details li{margin:6px 0}
.hy .foot{font-size:11px;color:var(--dim);text-align:center;margin-top:22px}
.hy .warn{background:var(--dnbg);color:var(--dn);border-radius:8px;padding:9px 12px;font-size:12px;margin-top:12px}
.hy .hold{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--amber);border-radius:6px;padding:9px 12px;font-size:13px;margin-top:12px}
.hy .al{display:block;padding:11px 13px;border-top:1px solid var(--line)}
.hy .al:first-child{border-top:0}
.hy .al .hd{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hy .al .hd b{font-weight:500;font-size:15px}
.hy .al .hd time{margin-left:auto;font-size:11px;color:var(--dim)}
.hy .al p{font-size:13px;color:var(--mut);margin-top:3px}
.hy .al .ft{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;margin-top:6px;color:var(--mut)}
.hy .al .ft a{color:var(--txt);text-decoration:none;border-bottom:1px solid var(--line)}
.hy .kind{font:600 10px/1 var(--mono);letter-spacing:.06em;padding:3px 6px;border-radius:4px;background:var(--panel2);color:var(--amber)}
.hy .kind.whales,.hy .kind.smart{color:var(--blue)} .hy .kind.theme{color:var(--mut)}
.hy .rec{display:flex;gap:6px 18px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:0 0 10px}
.hy .rec b{color:var(--txt);font-weight:500}
.hy table.wl{width:100%;border-collapse:collapse;font-size:13px}
.hy table.wl th,.hy table.wl td{text-align:left;padding:9px 13px;border-top:1px solid var(--line)}
.hy table.wl thead th{border-top:0;font:500 11px/1.3 var(--sans);color:var(--dim);letter-spacing:.06em;text-transform:uppercase}
.hy table.wl a{color:var(--txt);text-decoration:none;border-bottom:1px solid var(--line)}
@media(max-width:560px){.hy .tiles{grid-template-columns:repeat(2,1fr)}.hy .value{font-size:34px}
.hy .cells .hide{display:none}.hy .spark{width:64px}}
"""

PALETTE = ("#e8c468", "#3ecf8e", "#6aa7ff", "#b08cff", "#ff8a5c", "#5ed3d0", "#f47fb0", "#c9d36a")


def _usd(x: float, sign: bool = False) -> str:
    s = f"${abs(x):,.2f}"
    if sign:
        return ("+" if x > 0 else "−" if x < 0 else "") + s
    return ("−" if x < 0 else "") + s


def _pct(x: float, digits: int = 1) -> str:
    return f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x) * 100:.{digits}f}%"


def _cls(x: float) -> str:
    return "up" if x > 0 else "dn" if x < 0 else "mut"


def _ago(iso: str, now: datetime) -> str:
    try:
        secs = int((now - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return ""
    if secs < 90:
        return f"{max(secs, 0)}s"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60}m" if mins < 48 * 60 else f"{mins // 1440}d"


def _avatar(symbol: str) -> str:
    color = PALETTE[sum(map(ord, symbol)) % len(PALETTE)]
    return f"<span class='av' style='background:{color}'>{esc((symbol or '?')[:1].upper())}</span>"


def _line_path(vals: list[float], w: float, h: float, pad: float = 3) -> tuple[str, list[tuple[float, float]]]:
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or abs(hi) * 0.01 or 1.0
    n = len(vals)
    pts = [(pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0.5),
            pad + (h - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(vals)]
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts), pts


def sparkline(prices: list[float], entry: float) -> str:
    if len(prices) < 2:
        prices = [entry, prices[-1] if prices else entry]
    color = "var(--up)" if prices[-1] >= entry else "var(--dn)"
    d, _ = _line_path(prices, 84, 34)
    return (f"<svg class='spark' viewBox='0 0 84 34' preserveAspectRatio='none'>"
            f"<path d='{d}' fill='none' stroke='{color}' stroke-width='1.8' stroke-linejoin='round'/></svg>")


def equity_chart(curve: list[list], base: float) -> str:
    vals = [float(v) for _, v in curve] or [base]
    if len(vals) == 1:
        vals = [base, vals[0]]
    w, h = 600, 170
    _, pts = _line_path(vals + [base], w, h, 8)  # include the baseline in the scale
    line_pts = pts[:-1]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in line_pts)
    base_y = pts[-1][1]
    up = vals[-1] >= base
    col = "#3ecf8e" if up else "#ff6159"
    area = f"{path} L{line_pts[-1][0]:.1f},{h} L{line_pts[0][0]:.1f},{h} Z"
    lx, ly = line_pts[-1]
    return f"""<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none'>
<defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='{col}' stop-opacity='.28'/>
<stop offset='1' stop-color='{col}' stop-opacity='0'/></linearGradient></defs>
<line x1='0' x2='{w}' y1='{base_y:.1f}' y2='{base_y:.1f}' stroke='#2a2f37' stroke-dasharray='4 5' stroke-width='1'/>
<path d='{area}' fill='url(#g)'/><path d='{path}' fill='none' stroke='{col}' stroke-width='2.2' stroke-linejoin='round'/>
<circle cx='{lx:.1f}' cy='{ly:.1f}' r='4' fill='{col}'/><circle cx='{lx:.1f}' cy='{ly:.1f}' r='9' fill='{col}' opacity='.18'/>
</svg>"""


def _position_card(p: Any, rules: Rules, now: datetime) -> str:
    g = p.gain()
    value = sell_fill(p.last, p.qty, p.last_liquidity, rules) + p.realized
    pnl = value - p.stake
    lo, hi = rules.stop_loss, rules.take_half_at
    if p.half_taken:
        lo, hi = 0.0, max(hi * 2, p.peak / p.entry_price - 1)
    frac = min(max((g - lo) / (hi - lo), 0.0), 1.0)
    zero = min(max((0 - lo) / (hi - lo), 0.0), 1.0)
    fill_from, fill_to = sorted((zero, frac))
    color = "var(--up)" if g >= 0 else "var(--dn)"
    left_lbl = "breakeven" if p.half_taken else f"stop {_pct(lo, 0)}"
    right_lbl = f"{_pct(hi, 0)}" if p.half_taken else f"take half {_pct(hi, 0)}"
    half = " <span class='tag'>HALF SOLD</span>" if p.half_taken else ""
    return f"""<a class='pos' href='{esc(p.url)}' target='_blank' rel='noopener noreferrer'>
<div class='row'>{_avatar(p.symbol)}<div class='nm'><b>{esc(p.symbol)}</b>{half}
<small>held {_ago(p.opened, now)} · in {_usd(p.stake)}</small></div>{sparkline(p.trail, p.entry_price)}
<div class='pl'><b class='num {_cls(g)}'>{_pct(g)}</b><small class='num {_cls(pnl)}'>{_usd(pnl, True)}</small></div></div>
<div class='bar'><i style='left:{fill_from * 100:.1f}%;width:{(fill_to - fill_from) * 100:.1f}%;background:{color}'></i>
<u style='left:{zero * 100:.1f}%'></u></div>
<div class='ends'><span>{left_lbl}</span><span>best {_pct(p.peak / p.entry_price - 1, 0)}</span><span>{right_lbl}</span></div>
<p class='why'>{esc(p.why)}</p></a>"""


def _radar_row(w: dict) -> str:
    s = float(w.get("score", 0))
    mcol = "var(--up)" if s >= 60 else "var(--amber)" if s >= 40 else "var(--dim)"
    pill = ("<span class='pill buy'>BOUGHT</span>" if w.get("bought")
            else "<span class='pill skip'>SKIP</span>")
    reason = "" if w.get("bought") else esc("; ".join(w.get("why_not") or []))
    c5 = float(w.get("chg_5m", 0)) / 100
    return f"""<a class='li' href='{esc(w.get('url', ''))}' target='_blank' rel='noopener noreferrer'>
{_avatar(str(w.get('symbol', '?')))}<div class='nm'><b>{esc(str(w.get('symbol', '?')))}</b>
<div class='meter'><i style='width:{min(s, 100):.0f}%;background:{mcol}'></i></div>
<div class='reason'>{reason}</div></div>
<div class='cells num'><div class='hide'><small>BUYS</small>{float(w.get('buy_ratio', 0)):.0%}</div>
<div class='hide'><small>VOL</small>{float(w.get('vol_accel', 0)):.1f}x</div>
<div><small>5M</small><span class='{_cls(c5)}'>{_pct(c5, 0)}</span></div>
<div><small>SCORE</small>{s:.0f}</div></div>{pill}</a>"""


def _trade_row(t: Any, now: datetime) -> str:
    pct = t.pnl / t.stake if t.stake else 0.0
    return f"""<a class='li' href='{esc(t.url)}' target='_blank' rel='noopener noreferrer'>{_avatar(t.symbol)}
<div class='nm'><b>{esc(t.symbol)}</b><div class='reason'>{esc(t.reason)} · {_ago(t.closed, now)} ago</div></div>
<div class='pl'><b class='num {_cls(t.pnl)}'>{_usd(t.pnl, True)}</b><small class='num {_cls(pct)}'>{_pct(pct, 0)}</small></div></a>"""


def _page(state: Any, body: str, now: datetime | None = None) -> str:
    from tradinglab.web.views_bots import market_short

    now = now or datetime.now(UTC)
    return theme.shell(state, "Crypto · Trading Lab", f"<div class='hy'>{body}</div>", active="crypto",
                       refresh=30, market=market_short(now, state.tz), css=CSS)


def _live(text: str, kind: str = "") -> str:
    return f"<div class='live'><span class='dot {kind}'></span>{esc(text)}</div>"


def accounts(runner: Any) -> list[tuple[str, str, str, Rules, bool, Any]]:
    """(id, name, blurb, rules, buys enabled, store) for every rule set the runner drives. An
    older runner with one account is shown as a single 'current' set."""
    out = []
    for variant, store in getattr(runner, "variants", None) or []:
        out.append((variant.id, variant.name, variant.blurb, variant.rules, variant.buys_enabled, store))
    if not out and getattr(runner, "store", None) is not None:
        out.append(("current", "Current rules", "Chases 5-minute jumps",
                    getattr(runner, "rules", Rules()), True, runner.store))
    return out


def _compare(rows: list[tuple[str, str, str, Rules, bool, Any]], chosen: str) -> str:
    """One card per rule set: what it is worth, what it has made, how often it wins."""
    if len(rows) < 2:
        return ""
    cards = []
    for vid, name, blurb, rules, buys, store in rows:
        try:
            acct = store.load_hype()
            n, wins, _, _, _ = store.hype_stats()
            value = equity(acct, rules) if acct else rules.bankroll
            paused = (not buys) or bool(acct and getattr(acct, "paused", ""))
        except Exception:  # noqa: BLE001 - one unreadable account must not hide the others
            cards.append(f"<a class='v' href='/hype?a={esc(vid)}'><b>{esc(name)}</b>"
                         "<small>can't be read right now</small></a>")
            continue
        total = value - rules.bankroll
        wr = f"{wins / n:.0%} of {n} won" if n else "no trades yet"
        cards.append(
            f"<a class='v{' on' if vid == chosen else ''}' href='/hype?a={esc(vid)}'><b>{esc(name)}"
            f"{' · paused' if paused else ''}</b><small>{esc(blurb)}</small>"
            f"<span class='n {_cls(total)}'>{_usd(total, True)}</span>"
            f"<small>{_usd(value)} · {esc(wr)}</small></a>")
    return f"<div class='vs'>{''.join(cards)}</div>"


KIND_WORDS = {"early": "BEFORE MIGRATION", "migrated": "JUST MIGRATED", "theme": "TRENDING SUBJECT",
              "whales": "WHALES BUYING", "smart": "PROVEN WALLET"}


def scout_doc(state: Any) -> dict | None:
    """The scout's alerts and wallet records, or None (no scout, or it can't be read)."""
    runner = getattr(state, "hype", None)
    read = getattr(runner, "scout_doc", None)
    if read is None:
        return None
    try:
        return read()
    except Exception:  # noqa: BLE001 - the page still shows the accounts
        return None


def alert_row(a: dict, now: datetime) -> str:
    kind = str(a.get("kind", ""))
    moved = ""
    if kind != "theme" and a.get("price"):
        last, best = a.get("last", a["price"]) / a["price"] - 1, a.get("peak", a["price"]) / a["price"] - 1
        moved = (f"<span>since the alert <b class='num {_cls(last)}'>{_pct(last, 0)}</b></span>"
                 f"<span>best <b class='num {_cls(best)}'>{_pct(best, 0)}</b></span>")
    where = "".join(f"<a href='{esc(url)}' target='_blank' rel='noopener noreferrer'>{esc(name)}</a>"
                    for name, url in (a.get("links") or {}).items()
                    if str(url).startswith("https://"))
    return (f"<div class='al'><div class='hd'><span class='kind {esc(kind)}'>"
            f"{esc(KIND_WORDS.get(kind, kind.upper()))}</span><b>{esc(str(a.get('title', '?')))}</b>"
            f"<span class='num mut'>score {float(a.get('score', 0)):.0f}</span>"
            f"<time>{esc(_ago(a['at'], now))} ago</time></div><p>{esc(str(a.get('text', '')))}</p>"
            f"<div class='ft'>{moved}{where}</div></div>")


def alerts_section(doc: dict | None, now: datetime, limit: int = 12) -> str:
    """The scout's latest alerts, with how each has done since, and the day's record by kind."""
    if doc is None:
        return ""
    alerts = doc.get("alerts") or []
    record = "".join(
        f"<span>{esc(KIND_WORDS.get(k, k).lower())}: <b>{v['n']}</b> alerts, <b>{v['ran']}</b> went up 20%+, "
        f"now <b class='num {_cls(v['now'])}'>{_pct(v['now'], 0)}</b> on average</span>"
        for k, v in sorted(summary(doc, now).items()))
    if not alerts:
        body = ("<div class='empty'>No alerts yet. The scout looks once a minute for pump fun coins "
                "drawing a crowd, fresh migrations, trending subjects and whale buying.</div>")
    else:
        body = "<div class='list'>" + "".join(alert_row(a, now) for a in alerts[:limit]) + "</div>"
    note = (f"<div class='warn'>The scout had trouble on its last look: {esc(doc['error'])}</div>"
            if doc.get("error") else "")
    return (f"<h2>Alerts <span>{len(alerts)} kept · newest first</span></h2>"
            + (f"<p class='rec'>Last 24 hours: {record}</p>" if record else "") + body + note
            + "<p class='foot'>An alert is a heads-up, not a tip: most new coins fall. The record "
              "above is how these alerts have actually done.</p>")


def wallets_section(doc: dict | None, limit: int = 10) -> str:
    """Whale wallets the scout has scored itself: big buys it saw, and how many went up."""
    if doc is None:
        return ""
    proven = Scout.smart_wallets(doc)
    scored = sorted(((w, r) for w, r in (doc.get("wallets") or {}).items() if r[0] >= 3),
                    key=lambda kv: (kv[1][1] / kv[1][0], kv[1][0]), reverse=True)[:limit]
    if not scored:
        return ("<h2>Whale wallets <span>found by the scout</span></h2><div class='empty'>Still "
                f"learning. {len(doc.get('wallets') or {})} wallets seen so far; a wallet is listed "
                "after three of its big buys have been scored (an hour after each).</div>")
    rows = "".join(
        f"<tr><td><a href='https://solscan.io/account/{esc(w)}' target='_blank' rel='noopener noreferrer'>"
        f"{esc(w[:4])}…{esc(w[-4:])}</a>{' <span class=\'tag\'>PROVEN</span>' if w in proven else ''}</td>"
        f"<td class='num'>{r[1]} of {r[0]}</td><td class='num {_cls(r[2])}'>{_pct(r[2] / r[0], 0)}</td>"
        f"<td>{esc(str(r[4]))}</td></tr>" for w, r in scored)
    return ("<h2>Whale wallets <span>found by the scout</span></h2><div class='list'><table class='wl'>"
            "<thead><tr><th>Wallet</th><th>Big buys that rose 20%+</th><th>Average after an hour</th>"
            f"<th>Last coin</th></tr></thead><tbody>{rows}</tbody></table></div>"
            "<p class='foot'>\"Proven\" means at least five scored buys with three in five up. "
            "A proven wallet buying raises its own alert.</p>")


def hype_page(state: Any, now: datetime | None = None, account: str = "") -> str:
    now = now or datetime.now(UTC)
    runner = getattr(state, "hype", None)
    rows = accounts(runner) if runner is not None else []
    if not rows:
        return _page(state, _live("Offline", "off") + "<div class='empty'>The hype bot isn't running. "
                     "It starts with the web service once the database is connected (and HYPE_BOT "
                     "isn't off).</div>", now)
    ids = [r[0] for r in rows]
    if account not in ids:  # default: the first rule set that is still buying
        account = next((r[0] for r in rows if r[4]), ids[0])
    vid, name, blurb, rules, buys, store = rows[ids.index(account)]
    doc = scout_doc(state)
    compare = _compare(rows, vid) + alerts_section(doc, now)
    try:
        acct: Account | None = store.load_hype()
        trades = store.hype_trades(60)
        n, wins, pnl_closed, best, worst = store.hype_stats()
    except Exception as exc:  # noqa: BLE001 - never 500 the page
        return _page(state, compare + _live("Database", "bad") + "<div class='empty'>Can't read "
                     f"the bot right now ({esc(type(exc).__name__)}). It retries automatically.</div>", now)
    if acct is None:
        return _page(state, compare + _live("Starting") + "<div class='empty'>Warming up. The first "
                     "scan finishes within a minute.</div>", now)

    value = equity(acct, rules)
    total = value - rules.bankroll
    in_coins = value - acct.cash
    ok = not acct.last_error
    live = f"Live · checked {_ago(acct.last_cycle, now)} ago" if acct.last_cycle else "Live"
    start = acct.curve[0][0] if acct.curve else acct.started
    losses = n - wins
    avg_win = avg_loss = 0.0
    if trades:
        w = [t.pnl for t in trades if t.pnl > 0]
        lo = [t.pnl for t in trades if t.pnl <= 0]
        avg_win = sum(w) / len(w) if w else 0.0
        avg_loss = sum(lo) / len(lo) if lo else 0.0
    hold = ""
    reason = getattr(acct, "paused", "")
    if not buys:
        hold = ("<div class='hold'>Paused: this rule set is not buying. It still sells what it "
                "holds by its rules.</div>")
    elif reason:
        hold = f"<div class='hold'>Paused automatically: {esc(reason)}. It still sells what it holds.</div>"

    hero = f"""<section class='hero'><p class='lbl'>{esc(name)} · portfolio value</p>
<p class='value num'>{_usd(value)}</p>
<span class='chip {_cls(total)} num'>{_usd(total, True)} · {_pct(total / rules.bankroll if rules.bankroll else 0)}</span>
<span class='lbl'>&nbsp; since {esc(_ago(acct.started, now))} ago</span>
<div class='split'><span>Cash <b class='num'>{_usd(acct.cash)}</b></span>
<span>In coins <b class='num'>{_usd(in_coins)}</b></span><span>Open <b class='num'>{len(acct.positions)}/{rules.max_open}</b></span></div>
{hold}{"<div class='warn'>Data problem on the last scan: " + esc(acct.last_error) + "</div>" if acct.last_error else ""}
</section>
<div class='chart'>{equity_chart(acct.curve, rules.bankroll)}</div>
<div class='range'><span>{esc(_ago(start, now))} ago</span><span>— — start {_usd(rules.bankroll)}</span><span>now</span></div>"""

    win_rate = f"{wins / n:.0%}" if n else "—"
    tiles = f"""<div class='tiles'>
<div class='tile'><p class='lbl'>Win rate</p><p class='tv num'>{win_rate}</p><p class='lbl'>{wins}W · {losses}L</p></div>
<div class='tile'><p class='lbl'>Closed profit</p><p class='tv num {_cls(pnl_closed)}'>{_usd(pnl_closed, True)}</p><p class='lbl'>{n} trades</p></div>
<div class='tile'><p class='lbl'>Avg win</p><p class='tv num up'>{_usd(avg_win, True) if avg_win else '—'}</p><p class='lbl'>best {_usd(best, True) if n else '—'}</p></div>
<div class='tile'><p class='lbl'>Avg loss</p><p class='tv num dn'>{_usd(avg_loss, True) if avg_loss else '—'}</p><p class='lbl'>worst {_usd(worst, True) if n else '—'}</p></div>
</div>"""

    positions = "".join(_position_card(p, rules, now) for p in acct.positions) or \
        "<div class='empty'>No open positions. Waiting for a coin that passes every rule.</div>"
    radar = ("<div class='list'>" + "".join(_radar_row(w) for w in acct.watch) + "</div>") if acct.watch \
        else "<div class='empty'>Scanning…</div>"
    history = ("<div class='list'>" + "".join(_trade_row(t, now) for t in trades[:30]) + "</div>") if trades \
        else "<div class='empty'>No closed trades yet.</div>"
    extra = ""
    if getattr(rules, "stage", "") == "pre":
        extra += ("<li><b>Only coins still on pump fun</b>, before they migrate, found through "
                  "DexScreener's fresh profiles and boosts.</li>")
    if getattr(rules, "whale_min_usd", 0) > 0:
        extra += (f"<li><b>Needs whales:</b> at least {rules.whale_min_buyers} different wallets must "
                  f"each have made a big buy (1% of the pool, at least ${rules.whale_min_usd:,.0f}) in the last "
                  f"{rules.whale_window_min:.0f} minutes, with twice as much bought as sold. It "
                  "sells when the whales turn to selling.</li>")
    if getattr(rules, "stake_pct", 0) > 0:
        extra += f"<li><b>Bets {rules.stake_pct:.0%} of the account</b> per coin, not a fixed amount.</li>"
    if getattr(rules, "no_repeat_losers", False):
        extra += "<li><b>Never re-buys</b> a coin it has lost money on.</li>"

    rules_html = f"""<details><summary>How this rule set trades</summary><ul>
<li><b>Scans</b> the trending and newest Solana pools every minute and scores each coin 0–100 on buy
pressure, volume surge, momentum and pool depth.</li>
<li><b>Buys ${rules.stake:,.0f}</b> only if ≥{rules.min_buy_ratio:.0%} of trades are buys, volume is
≥{rules.min_vol_accel:.1f}x normal, price is up {rules.min_chg_5m:.0f}–{rules.max_chg_5m:.0f}% in 5 min, the pool
holds ≥${rules.min_liquidity:,.0f}, and the coin is ≥{rules.min_age_min:.0f} min old. Max {rules.max_open} at once.</li>{extra}
<li><b>Sells</b> half at {_pct(rules.take_half_at, 0)} then never lets the rest go below breakeven; stop
{_pct(rules.stop_loss, 0)}; trails {rules.trail_drop:.0%} off the top once up {rules.trail_after:.0%}; drops flat coins
after {rules.stale_minutes:.0f} min; max hold {rules.max_hold_hours:.0f} h.</li>
<li><b>Costs are harsh on purpose:</b> {rules.latency_penalty:.0%} delay penalty per buy, ~{rules.platform_fee:.0%}
platform fee + pool fee each way, price impact from pool depth, network fees. A pulled pool is booked at
{_pct(rules.rug_loss, 0)}.</li>
<li>Pretend money only. No wallet, no keys, and trading platforms are blocked in code.</li></ul></details>"""

    body = (compare + _live(live, "" if ok else "bad") + hero + tiles
            + f"<h2>Positions <span>{len(acct.positions)} open</span></h2>" + positions
            + f"<h2>Hot right now <span>last scan · {len(acct.watch)} coins</span></h2>" + radar
            + f"<h2>History <span>{n} closed</span></h2>" + history + wallets_section(doc) + rules_html
            + f"<p class='foot'>Prices from DexScreener and GeckoTerminal · refreshes every 30s · {now.strftime('%H:%M UTC')}</p>")
    return _page(state, body, now)
