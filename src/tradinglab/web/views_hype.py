"""The hype bot's page, styled like a trading app: portfolio value and chart, open positions
with mini charts, a live 'hot right now' market list, and trade history. Server-rendered HTML
and inline SVG only (the dashboard's CSP allows no scripts)."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from tradinglab.hype.bot import equity
from tradinglab.hype.model import Account, Rules, sell_fill

esc = html.escape

FONTS = ("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800"
         "&family=JetBrains+Mono:wght@500;700&display=swap")

CSS = """
:root{--bg:#0a0d12;--card:#11161d;--card2:#161c25;--line:#1f2732;--txt:#e8ecf1;--mut:#8691a2;
--dim:#56606f;--up:#16c784;--upbg:rgba(22,199,132,.12);--dn:#f6465d;--dnbg:rgba(246,70,93,.12);
--acc:#f0b90b;--blue:#3d8bfd}
*{box-sizing:border-box;margin:0}
html,body{background:var(--bg);color:var(--txt);font:15px/1.45 Inter,system-ui,sans-serif;
-webkit-font-smoothing:antialiased}
body{padding:14px 14px 40px;max-width:760px;margin:0 auto}
a{color:inherit;text-decoration:none}
.num{font-family:'JetBrains Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}.dim{color:var(--dim)}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:18px}
.logo{display:flex;align-items:center;gap:9px;font-weight:800;letter-spacing:.02em}
.logo i{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;font-style:normal;
background:linear-gradient(135deg,#f0b90b,#f6465d);color:#0a0d12;font-size:15px}
.paper{font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--acc);border:1px solid rgba(240,185,11,.4);
padding:2px 6px;border-radius:5px}
.live{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--mut)}
.live b{width:8px;height:8px;border-radius:50%;background:var(--up);box-shadow:0 0 0 4px rgba(22,199,132,.18)}
.live.err b{background:var(--dn);box-shadow:0 0 0 4px rgba(246,70,93,.18)}
.back{font-size:12px;color:var(--mut);border:1px solid var(--line);padding:5px 10px;border-radius:999px}
.hero{padding:4px 2px 0}
.lbl{font-size:12px;color:var(--mut);font-weight:500}
.value{font-size:40px;font-weight:700;letter-spacing:-.02em;margin:2px 0 6px}
.chip{display:inline-flex;gap:6px;align-items:center;font-size:13px;font-weight:700;padding:4px 9px;border-radius:7px}
.chip.up{background:var(--upbg)}.chip.dn{background:var(--dnbg)}
.split{display:flex;gap:16px;margin-top:10px;font-size:12px;color:var(--mut)}
.split b{color:var(--txt);font-weight:600}
.chart{margin:16px -4px 6px}
.chart svg{display:block;width:100%;height:170px}
.range{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);padding:0 4px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:16px 0 22px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:11px 12px}
.tile .v{font-size:17px;font-weight:700;margin-top:2px}
h2{font-size:13px;font-weight:700;color:var(--mut);letter-spacing:.06em;text-transform:uppercase;
margin:24px 2px 10px;display:flex;justify-content:space-between;align-items:baseline}
h2 span{font-size:12px;font-weight:500;letter-spacing:0;text-transform:none;color:var(--dim)}
.pos{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px;margin-bottom:9px;display:block}
.row{display:flex;align-items:center;gap:11px}
.av{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:15px;
flex:none;color:#0a0d12}
.nm{flex:1;min-width:0}.nm b{font-size:15px}.nm small{display:block;font-size:12px;color:var(--mut)}
.spark{width:84px;height:34px;flex:none}
.pl{text-align:right;min-width:78px}.pl b{font-size:16px;display:block}.pl small{font-size:12px}
.bar{position:relative;height:6px;border-radius:3px;background:var(--card2);margin:12px 0 5px;overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;border-radius:3px}
.bar u{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--txt);border-radius:1px}
.ends{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}
.why{font-size:12px;color:var(--mut);margin-top:8px;line-height:1.4}
.tag{font-size:10px;font-weight:700;letter-spacing:.06em;padding:2px 6px;border-radius:4px;background:var(--card2);color:var(--acc)}
.list{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.li{display:flex;align-items:center;gap:11px;padding:11px 13px;border-top:1px solid var(--line)}
.li:first-child{border-top:0}
.li .av{width:32px;height:32px;font-size:13px}
.meter{width:46px;height:5px;border-radius:3px;background:var(--card2);overflow:hidden;margin-top:5px}
.meter i{display:block;height:100%;border-radius:3px}
.cells{display:flex;gap:14px;font-size:12px;text-align:right}
.cells div{min-width:44px}.cells small{display:block;color:var(--dim);font-size:10px}
.pill{font-size:10px;font-weight:800;letter-spacing:.06em;padding:3px 7px;border-radius:5px;white-space:nowrap}
.pill.buy{background:var(--upbg);color:var(--up)}.pill.skip{background:var(--card2);color:var(--dim)}
.reason{font-size:11px;color:var(--dim);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty{background:var(--card);border:1px dashed var(--line);border-radius:14px;padding:22px;text-align:center;color:var(--mut);font-size:14px}
details{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 14px;margin-top:22px}
summary{cursor:pointer;font-weight:600;font-size:14px}
details ul{margin:10px 0 0 18px;color:var(--mut);font-size:13px}details li{margin:6px 0}
.foot{font-size:11px;color:var(--dim);text-align:center;margin-top:22px}
.warn{background:var(--dnbg);color:#ffb3bd;border-radius:10px;padding:9px 12px;font-size:12px;margin-top:12px}
@media (max-width:560px){.tiles{grid-template-columns:repeat(2,1fr)}.value{font-size:34px}
.cells .hide{display:none}.spark{width:64px}}
"""

PALETTE = ["#f0b90b", "#3d8bfd", "#16c784", "#f6465d", "#b56cff", "#ff8a3d", "#2ad1d1", "#ff5fa2"]


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
    col = "#16c784" if up else "#f6465d"
    area = f"{path} L{line_pts[-1][0]:.1f},{h} L{line_pts[0][0]:.1f},{h} Z"
    lx, ly = line_pts[-1]
    return f"""<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none'>
<defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='{col}' stop-opacity='.28'/>
<stop offset='1' stop-color='{col}' stop-opacity='0'/></linearGradient></defs>
<line x1='0' x2='{w}' y1='{base_y:.1f}' y2='{base_y:.1f}' stroke='#3a4453' stroke-dasharray='4 5' stroke-width='1'/>
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
    mcol = "var(--up)" if s >= 60 else "var(--acc)" if s >= 40 else "var(--dim)"
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


def _page(body: str, title: str = "Hype bot") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0d12"><meta http-equiv="refresh" content="30">
<title>{esc(title)}</title><link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="{FONTS}">
<style>{CSS}</style></head><body>{body}</body></html>"""


def _top(live: str, ok: bool) -> str:
    return (f"<div class='top'><div class='logo'><i>◆</i>HYPE<span class='paper'>PAPER</span></div>"
            f"<div class='live{'' if ok else ' err'}'><b></b>{esc(live)}</div>"
            f"<a class='back' href='/'>Stocks →</a></div>")


def hype_page(state: Any, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    runner = getattr(state, "hype", None)
    store = getattr(runner, "store", None)
    rules: Rules = getattr(runner, "rules", Rules())
    if store is None:
        return _page(_top("OFFLINE", False) + "<div class='empty'>The hype bot isn't running. It starts with "
                     "the web service once the database is connected (and HYPE_BOT isn't off).</div>")
    try:
        acct: Account | None = store.load_hype()
        trades = store.hype_trades(60)
        n, wins, pnl_closed, best, worst = store.hype_stats()
    except Exception as exc:  # noqa: BLE001 - never 500 the page
        return _page(_top("DATABASE", False) + f"<div class='empty'>Can't read the bot right now "
                     f"({esc(type(exc).__name__)}). It retries automatically.</div>")
    if acct is None:
        return _page(_top("STARTING", True) + "<div class='empty'>Warming up. The first scan finishes "
                     "within a minute.</div>")

    value = equity(acct, rules)
    total = value - rules.bankroll
    in_coins = value - acct.cash
    ok = not acct.last_error
    live = f"LIVE · {_ago(acct.last_cycle, now)} ago" if acct.last_cycle else "LIVE"
    start = acct.curve[0][0] if acct.curve else acct.started
    losses = n - wins
    avg_win = avg_loss = 0.0
    if trades:
        w = [t.pnl for t in trades if t.pnl > 0]
        lo = [t.pnl for t in trades if t.pnl <= 0]
        avg_win = sum(w) / len(w) if w else 0.0
        avg_loss = sum(lo) / len(lo) if lo else 0.0

    hero = f"""<section class='hero'><p class='lbl'>Portfolio value</p>
<p class='value num'>{_usd(value)}</p>
<span class='chip {_cls(total)} num'>{_usd(total, True)} · {_pct(total / rules.bankroll if rules.bankroll else 0)}</span>
<span class='lbl'>&nbsp; since {esc(_ago(acct.started, now))} ago</span>
<div class='split'><span>Cash <b class='num'>{_usd(acct.cash)}</b></span>
<span>In coins <b class='num'>{_usd(in_coins)}</b></span><span>Open <b class='num'>{len(acct.positions)}/{rules.max_open}</b></span></div>
{"<div class='warn'>Data problem on the last scan: " + esc(acct.last_error) + "</div>" if acct.last_error else ""}
</section>
<div class='chart'>{equity_chart(acct.curve, rules.bankroll)}</div>
<div class='range'><span>{esc(_ago(start, now))} ago</span><span>— — start {_usd(rules.bankroll)}</span><span>now</span></div>"""

    win_rate = f"{wins / n:.0%}" if n else "—"
    tiles = f"""<div class='tiles'>
<div class='tile'><p class='lbl'>Win rate</p><p class='v num'>{win_rate}</p><p class='lbl'>{wins}W · {losses}L</p></div>
<div class='tile'><p class='lbl'>Closed P&amp;L</p><p class='v num {_cls(pnl_closed)}'>{_usd(pnl_closed, True)}</p><p class='lbl'>{n} trades</p></div>
<div class='tile'><p class='lbl'>Avg win</p><p class='v num up'>{_usd(avg_win, True) if avg_win else '—'}</p><p class='lbl'>best {_usd(best, True) if n else '—'}</p></div>
<div class='tile'><p class='lbl'>Avg loss</p><p class='v num dn'>{_usd(avg_loss, True) if avg_loss else '—'}</p><p class='lbl'>worst {_usd(worst, True) if n else '—'}</p></div>
</div>"""

    positions = "".join(_position_card(p, rules, now) for p in acct.positions) or \
        "<div class='empty'>No open positions. Waiting for a coin that passes every rule.</div>"
    radar = ("<div class='list'>" + "".join(_radar_row(w) for w in acct.watch) + "</div>") if acct.watch \
        else "<div class='empty'>Scanning…</div>"
    history = ("<div class='list'>" + "".join(_trade_row(t, now) for t in trades[:30]) + "</div>") if trades \
        else "<div class='empty'>No closed trades yet.</div>"

    rules_html = f"""<details><summary>How the bot trades</summary><ul>
<li><b>Scans</b> the trending and newest Solana pools every minute and scores each coin 0–100 on buy
pressure, volume surge, momentum and pool depth.</li>
<li><b>Buys ${rules.stake:,.0f}</b> only if ≥{rules.min_buy_ratio:.0%} of trades are buys, volume is
≥{rules.min_vol_accel:.1f}x normal, price is up {rules.min_chg_5m:.0f}–{rules.max_chg_5m:.0f}% in 5 min, the pool
holds ≥${rules.min_liquidity:,.0f}, and the coin is ≥{rules.min_age_min:.0f} min old. Max {rules.max_open} at once.</li>
<li><b>Sells</b> half at {_pct(rules.take_half_at, 0)} then never lets the rest go below breakeven; stop
{_pct(rules.stop_loss, 0)}; trails {rules.trail_drop:.0%} off the top once up {rules.trail_after:.0%}; drops flat coins
after {rules.stale_minutes:.0f} min; max hold {rules.max_hold_hours:.0f} h.</li>
<li><b>Costs are harsh on purpose:</b> {rules.latency_penalty:.0%} delay penalty per buy, ~{rules.platform_fee:.0%}
platform fee + pool fee each way, price impact from pool depth, network fees. A pulled pool is booked at
{_pct(rules.rug_loss, 0)}.</li>
<li>Pretend money only. No wallet, no keys, and trading platforms are blocked in code.</li></ul></details>"""

    body = (_top(live, ok) + hero + tiles
            + f"<h2>Positions <span>{len(acct.positions)} open</span></h2>" + positions
            + f"<h2>Hot right now <span>last scan · {len(acct.watch)} coins</span></h2>" + radar
            + f"<h2>History <span>{n} closed</span></h2>" + history + rules_html
            + f"<p class='foot'>Prices from GeckoTerminal · refreshes every 30s · {now.strftime('%H:%M UTC')}</p>")
    return _page(body)
