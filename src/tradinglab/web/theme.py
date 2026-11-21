"""The dashboard's one look: design tokens, the page shell and the small chart helpers.

Every page renders through `shell`, so stocks, crypto and controls share one header, one set of
tabs and one stylesheet. No JavaScript (the Content-Security-Policy forbids it): charts are
inline SVG built here on the server.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

esc = html.escape

FONTS = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600"
         "&family=IBM+Plex+Sans:wght@400;500;600&display=swap")

# (key, label, href)
TABS = (("overview", "Overview", "/"), ("stocks", "Stocks", "/stocks"), ("crypto", "Crypto", "/hype"),
        ("tests", "History tests", "/tests"), ("controls", "Controls", "/controls"))

# Line colours for the "every bot" chart: the leader is bright, the rest step down.
SERIES = ("#ebe8e1", "#3ecf8e", "#e8c468", "#6aa7ff", "#b08cff", "#8b9099", "#6f747c", "#5a5f68")

CSS = """
:root{--bg:#0b0c0e;--panel:#111316;--panel2:#16191d;--line:#20242a;--txt:#ebe8e1;--mut:#8b9099;
--dim:#5a5f68;--up:#3ecf8e;--dn:#ff6159;--amber:#e8c468;--blue:#6aa7ff;--upbg:rgba(62,207,142,.1);
--dnbg:rgba(255,97,89,.1);--sans:'IBM Plex Sans',system-ui,sans-serif;
--mono:'IBM Plex Mono',ui-monospace,monospace}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 var(--sans)}
a{color:inherit}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 56px}
.num,.value,.val,.px,table.bt td,.daylist b{font-family:var(--mono);font-variant-numeric:tabular-nums}
.gain,.up{color:var(--up)} .loss,.dn{color:var(--dn)} .flat,.quiet,.mut{color:var(--mut)}
.small{font-size:13px} .nw{white-space:nowrap}
.top{display:flex;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
.logo{font-weight:600;font-size:15px;display:flex;align-items:center;gap:9px;text-decoration:none}
.logo i{width:14px;height:14px;border:2px solid var(--txt);border-radius:3px;display:block;
background:linear-gradient(135deg,transparent 50%,var(--txt) 50%)}
.sim{font:600 10px/1 var(--mono);letter-spacing:.08em;color:var(--amber);
border:1px solid rgba(232,196,104,.4);padding:4px 6px;border-radius:4px}
.top .right{margin-left:auto;display:flex;gap:14px;color:var(--mut);font-size:12px;align-items:center}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--up);margin-right:6px}
.dot.warn{background:var(--amber)} .dot.bad{background:var(--dn)} .dot.off{background:var(--dim)}
.tabs{display:flex;gap:2px;padding:10px 0 0;overflow-x:auto;border-bottom:1px solid var(--line)}
.tabs a{color:var(--mut);text-decoration:none;padding:8px 12px;border-bottom:2px solid transparent;
white-space:nowrap;font-weight:500;margin-bottom:-1px}
.tabs a.on{color:var(--txt);border-color:var(--txt)}
.tabs a:hover{color:var(--txt)}
.crumb{margin:18px 0 0;color:var(--mut);font-size:13px}
.crumb a{color:var(--txt);text-decoration:none;border-bottom:1px solid var(--line)}
.headline{margin:22px 0 4px}
h1{font:600 clamp(24px,5vw,32px)/1.15 var(--sans);letter-spacing:-.02em;margin:0 0 8px}
.lede{font-size:16px;margin:0 0 6px;max-width:44em;color:var(--txt)}
.status{color:var(--mut);font-size:13px;margin:0}
h2{font:500 12px/1.2 var(--sans);letter-spacing:.07em;text-transform:uppercase;color:var(--mut);margin:34px 0 12px}
h3{font:600 18px/1.1 var(--sans);margin:0}
h4{font:600 14px/1.3 var(--sans);margin:16px 0 4px}
section>p{max-width:60em}
.notice{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--amber);
padding:10px 14px;margin:14px 0 0;border-radius:6px;font-size:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.card.flush{padding:0;overflow:hidden}
.card.flush>h2{margin:0;padding:14px 16px;border-bottom:1px solid var(--line);display:flex;
justify-content:space-between;gap:10px}
.lbl{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--mut);margin:0 0 6px;display:block}
.big{font:600 34px/1.1 var(--mono);letter-spacing:-.02em}
.hero{display:grid;grid-template-columns:1.1fr 2fr;gap:16px;margin-top:16px}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;margin-top:16px;padding-top:14px;
border-top:1px solid var(--line)}
.kpis b{display:block;font-size:16px;font-weight:500}
.kpis.four{grid-template-columns:repeat(4,1fr);border:0;margin:0;padding:0}
.linechart{display:block;width:100%;height:170px}
.axis-row{display:flex;justify-content:space-between;color:var(--dim);font-size:11px;margin-top:6px}
.keys{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:10px;font-size:12px;color:var(--mut)}
.keys i{display:inline-block;width:14px;height:2px;margin-right:6px;vertical-align:middle}
.cols{display:grid;grid-template-columns:2.1fr 1fr;gap:16px;margin-top:16px;align-items:start}
.stack{display:grid;gap:16px}
.row{display:grid;grid-template-columns:26px minmax(0,1.7fr) 96px 92px 104px 56px 104px;gap:10px;
align-items:center;padding:11px 16px;border-bottom:1px solid var(--line);color:inherit;text-decoration:none}
.row:last-child{border-bottom:0} a.row:hover{background:var(--panel2)}
.row.head{color:var(--dim);font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding:8px 16px}
.row .r{text-align:right} .rank{color:var(--dim);font-family:var(--mono)}
.name{font-weight:500;min-width:0}
.name b{font-weight:500;display:block}
.name small{display:block;color:var(--mut);font-weight:400;font-size:12px;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.new{font:600 9px/1 var(--mono);color:var(--bg);background:var(--txt);padding:2px 4px;border-radius:3px;
margin-left:6px;vertical-align:2px}
.st{font-size:12px;color:var(--mut);text-align:right;line-height:1.3}
.tot{display:none}
.grp{padding:8px 16px;background:var(--panel2);color:var(--mut);font-size:11px;letter-spacing:.07em;
text-transform:uppercase;border-bottom:1px solid var(--line)}
.feed>div{padding:11px 16px;border-bottom:1px solid var(--line);font-size:14px}
.feed>div:last-child{border:0}
.feed time{color:var(--dim);font-size:11px;display:block;margin-bottom:2px}
.chip{font:500 11px/1 var(--mono);padding:3px 6px;border-radius:4px;background:var(--panel2);
color:var(--mut);margin-left:4px;white-space:nowrap}
.chip.gain{background:var(--upbg);color:var(--up)} .chip.loss{background:var(--dnbg);color:var(--dn)}
.health>div{display:flex;justify-content:space-between;gap:10px;padding:10px 16px;
border-bottom:1px solid var(--line);font-size:13px}
.health>div:last-child{border:0}
.empty{padding:22px 16px;color:var(--mut);text-align:center;font-size:14px}
.value{font-weight:600;font-size:clamp(30px,7vw,40px);line-height:1;letter-spacing:-.03em;margin:0}
.since{margin:8px 0 10px}
.acct{margin-top:16px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.acct p:last-child{margin-bottom:0}
.split{display:flex;gap:28px;margin:0 0 14px}
.split dt{color:var(--mut);font-size:12px} .split dd{margin:0;font:500 17px/1.3 var(--mono)}
.compare{background:var(--panel2);padding:12px 14px;border-radius:8px;margin:14px 0}
.fund{background:var(--panel);border-radius:10px;padding:16px 16px 12px;margin:0 0 14px;border:1px solid var(--line)}
.fund header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.fund header p{margin:6px 0 0}
.funds{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.funds .fund{margin:0}
.badge{font:500 13px/1.2 var(--sans);white-space:nowrap;padding-top:4px;display:flex;align-items:center;gap:7px}
.badge::before{content:'';width:8px;height:8px;border-radius:50%;background:currentColor}
.badge.watch{color:var(--mut)} .badge.hold{color:var(--up)} .badge.buy{color:var(--blue)}
.badge.sell{color:var(--dn)} .badge.warn{color:var(--amber)}
.pxrow{display:flex;align-items:baseline;gap:10px;margin:14px 0 4px}
.px{font-weight:600;font-size:20px;line-height:1}
.chart{width:100%;height:auto;display:block;margin:6px 0 2px;overflow:visible}
.chart polyline{fill:none;stroke-linejoin:round;stroke-linecap:round}
.chart .price{stroke:var(--txt);stroke-width:1.5}
.chart .slow{stroke:var(--blue);stroke-width:1.8}
.chart .fast{stroke:var(--amber);stroke-width:1.4;stroke-dasharray:4 3}
.chart .dot{fill:var(--txt)} .chart .tag{font:500 12px var(--mono);fill:var(--txt)}
.chart .axis{font:11px var(--sans);fill:var(--dim)}
.chart .base{stroke:var(--dim);stroke-width:1;stroke-dasharray:2 3}
.chart .buy{fill:var(--up)} .chart .sell{fill:var(--dn)}
.legend{font-size:12px;color:var(--mut);margin:4px 0 10px;display:flex;flex-wrap:wrap;align-items:center;gap:4px 14px}
.legend span{white-space:nowrap}
.legend .k{display:inline-block;width:18px;height:0;border-top:2px solid;margin-right:6px;vertical-align:middle}
.legend .k.price{border-color:var(--txt)} .legend .k.slow{border-color:var(--blue)}
.legend .k.fast{border-color:var(--amber);border-top-style:dashed}
.story{margin:12px 0 0;max-width:44em}
.timeline{list-style:none;margin:0;padding:0;border-left:1px solid var(--line)}
.timeline .day{position:relative;padding:0 0 14px 18px}
.timeline .day::before{content:'';position:absolute;left:-4px;top:7px;width:7px;height:7px;
border-radius:50%;background:var(--dim)}
.timeline time{font:500 12px/1.4 var(--mono);color:var(--mut)}
.timeline ul{margin:2px 0 0;padding:0;list-style:none}
.timeline ul li{margin:2px 0;max-width:52em}
details{margin-top:8px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
summary{cursor:pointer;font:500 15px/1.3 var(--sans);padding:14px 16px;list-style:none;display:flex;
justify-content:space-between;align-items:center;gap:12px}
summary::-webkit-details-marker{display:none}
summary::after{content:'Show';font:500 12px/1 var(--sans);color:var(--mut);white-space:nowrap}
details[open] summary::after{content:'Hide'}
.how{padding:0 16px 14px}
.how p,.how li{max-width:52em} .how ul{padding-left:20px}
.btns{display:flex;gap:10px;flex-wrap:wrap}
button{font:500 14px/1 var(--sans);padding:12px 16px;border-radius:8px;min-height:44px;cursor:pointer;
border:1px solid var(--line);background:var(--panel2);color:var(--txt)}
button.primary{background:var(--txt);color:var(--bg);border-color:var(--txt)}
button:hover{border-color:var(--mut)} button:active{transform:translateY(1px)}
button:focus-visible,summary:focus-visible,a:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);font-size:13px}
footer p{margin:0 0 6px}
footer .ok{color:var(--mut)}
footer details{background:none;border:0}
footer summary{font:500 13px/1.3 var(--sans);color:var(--mut);padding:6px 0;justify-content:flex-start}
.scroll{overflow-x:auto}
table.bt{border-collapse:collapse;width:100%;font-size:14px}
table.bt th,table.bt td{text-align:left;padding:9px 12px 9px 0;border-bottom:1px solid var(--line);vertical-align:top}
table.bt thead th{font:500 11px/1.3 var(--sans);color:var(--dim);letter-spacing:.06em;text-transform:uppercase}
table.bt tbody th{font-weight:500;font-family:var(--sans)}
table.bt a{text-decoration:none;border-bottom:1px solid var(--line)}
.daylist{list-style:none;margin:0;padding:0}
.daylist li{display:grid;grid-template-columns:1fr auto auto;gap:12px;padding:8px 0;border-bottom:1px solid var(--line);
align-items:baseline}
.tech{padding:0;list-style:none;color:var(--mut);font-size:12px;word-break:break-word}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
@media(max-width:1100px){.cols{grid-template-columns:1fr}}
@media(max-width:820px){.hero,.cols,.two{grid-template-columns:1fr}.big{font-size:30px}
.row{grid-template-columns:22px minmax(0,1fr) 56px 104px;gap:8px}
.row>:nth-child(5),.row>:nth-child(6),.row>:nth-child(7){display:none}
.row svg{width:56px}.name b{white-space:normal}.name small{display:none}
.tot{display:block;font-size:11px;color:var(--mut)}
.kpis.four{grid-template-columns:1fr 1fr}.top .right .wide{display:none}}
"""


def shell(state: Any, title: str, body: str, *, active: str = "overview", back: str = "",
          refresh: int = 0, market: str = "", css: str = "") -> str:
    """The whole HTML document. `market` is a short line for the header (open/closed);
    `back` adds a breadcrumb to the bot list for a single bot's page."""
    now = datetime.now(UTC).astimezone(state.tz)
    tabs = "".join(f"<a href='{href}'{' class=\"on\"' if key == active else ''}>{label}</a>"
                   for key, label, href in TABS)
    crumb = (f"<p class='crumb'><a href='/stocks'>← All bots</a> / {esc(back)}</p>" if back else "")
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    mkt = f"<span class='wide'>{esc(market)}</span>" if market else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>{esc(title)}</title>{meta}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}"><style>{CSS}{css}</style></head><body><div class="wrap">
<header class="top"><a class="logo" href="/"><i></i>Trading Lab</a><span class="sim">SIM · PRETEND MONEY</span>
<div class="right">{mkt}<span>Loaded {esc(now.strftime('%b %-d, %-I:%M %p'))}</span></div></header>
<nav class="tabs">{tabs}</nav>
{crumb}<main>
{body}
</main></div></body></html>"""


def _points(values: Sequence[float], w: float, h: float, pad: float, lo: float, hi: float) -> str:
    span = (hi - lo) or 1.0
    n = max(len(values) - 1, 1)
    return " ".join(f"{i / n * w:.1f},{pad + (hi - v) / span * (h - 2 * pad):.1f}"
                    for i, v in enumerate(values))


def spark(values: Sequence[float], w: int = 90, h: int = 26) -> str:
    """A tiny trend line, green if it ends at or above where it starts."""
    vals = [float(v) for v in values]
    if len(vals) < 2:
        return f"<svg viewBox='0 0 {w} {h}' width='{w}' height='{h}' aria-hidden='true'></svg>"
    colour = "var(--up)" if vals[-1] >= vals[0] else "var(--dn)"
    pts = _points(vals, w, h, 3, min(vals), max(vals))
    return (f"<svg viewBox='0 0 {w} {h}' width='{w}' height='{h}' aria-hidden='true'>"
            f"<polyline points='{pts}' fill='none' stroke='{colour}' stroke-width='1.5' "
            "stroke-linejoin='round'/></svg>")


def line_chart(series: Sequence[tuple[str, Sequence[float]]], w: int = 700, h: int = 170,
               zero: float | None = 0.0) -> str:
    """Several lines on one scale (first series drawn last and brightest). `zero` draws a dashed
    baseline at that value when it is inside the range. Returns the SVG and a colour key."""
    lines = [(name, [float(v) for v in vals]) for name, vals in series if len(vals) >= 2]
    if not lines:
        return "<p class='empty'>The chart fills in after the first trading days.</p>"
    flat = [v for _, vals in lines for v in vals]
    if zero is not None:
        flat.append(zero)
    lo, hi = min(flat), max(flat)
    out, keys = [], []
    if zero is not None:
        y = 6 + (hi - zero) / ((hi - lo) or 1.0) * (h - 12)
        out.append(f"<line x1='0' x2='{w}' y1='{y:.1f}' y2='{y:.1f}' stroke='#20242a' "
                   "stroke-dasharray='3 4'/>")
    for i, (_name, vals) in reversed(list(enumerate(lines))):
        colour = SERIES[min(i, len(SERIES) - 1)]
        out.append(f"<polyline points='{_points(vals, w, h, 6, lo, hi)}' fill='none' "
                   f"stroke='{colour}' stroke-width='{2 if i == 0 else 1.2}' "
                   f"opacity='{1 if i == 0 else .8}' stroke-linejoin='round'/>")
    for i, (name, _) in enumerate(lines[:6]):
        keys.append(f"<span><i style='background:{SERIES[min(i, len(SERIES) - 1)]}'></i>{esc(name)}</span>")
    return (f"<svg class='linechart' viewBox='0 0 {w} {h}' preserveAspectRatio='none' role='img' "
            f"aria-label='Profit of each bot since it started'>{''.join(out)}</svg>"
            f"<div class='keys'>{''.join(keys)}</div>")
