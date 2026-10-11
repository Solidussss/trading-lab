"""Dashboard views: a plain-English daily briefing for a simulated portfolio.

Designed for someone with no trading background, checking on a phone. The page answers, in
order: what is the bot doing and why; what happens next; how is the money doing; what has it
done so far; how does it work. No jargon on the surface; detail is one tap away.
"""

from __future__ import annotations

import html
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from tradinglab.marketdata.bars import Bar
from tradinglab.marketdata.calendar import next_trading_day
from tradinglab.strategies.trend_following import DEFAULT_PARAMS

esc = html.escape
FAST = int(DEFAULT_PARAMS["fast"])
SLOW = int(DEFAULT_PARAMS["slow"])
CHART_DAYS = 120

FUNDS = {
    "SPY": ("S&P 500", "the 500 largest US companies"),
    "QQQ": ("Nasdaq-100", "100 large, mostly tech companies"),
    "IWM": ("Russell 2000", "2,000 smaller US companies"),
}

# --- formatting ---------------------------------------------------------------------------


def money(x: float, cents: bool = True) -> str:
    s = f"{abs(x):,.2f}" if cents else f"{abs(x):,.0f}"
    return f"-${s}" if x < 0 else f"${s}"


def signed(x: float, cents: bool = True) -> str:
    if abs(x) < 0.005:
        return "$0.00" if cents else "$0"
    return ("+" if x > 0 else "−") + money(abs(x), cents)


def pct(x: float) -> str:
    return f"{x:+.1%}".replace("-", "−")


def tone(x: float) -> str:
    return "gain" if x > 0.004 else ("loss" if x < -0.004 else "flat")


def day_label(d: date) -> str:
    return d.strftime("%a %b ") + str(d.day)


def time_label(dt: datetime, tz) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%-I:%M %p").lower().replace(":00", "")


# --- plain-English reasons ----------------------------------------------------------------

REASONS = [
    ("uptrend: close>slow & fast>slow", "it's in an uptrend"),
    ("no_uptrend: close<=sma_slow", "the price is below its long-term trend line"),
    ("no_uptrend: sma_fast<=sma_slow", "the short-term trend line is still below the long-term one"),
    ("trend_end: sma_fast<sma_slow", "the uptrend ended (short-term line fell below the long-term one)"),
    ("trend_end: close<sma_slow", "the uptrend ended (price fell below its long-term trend line)"),
    ("stop_gap_at_open", "the price opened below the safety stop"),
    ("stop_hit", "the price fell to the safety stop"),
    ("end_of_backtest", "the test period ended"),
    ("sizing:max_open_positions", "it already holds the maximum number of funds"),
    ("sizing:max_exposure_zero", "it's already invested as much as your limit allows"),
    ("sizing:risk_budget_zero", "even one share would risk more than your per-trade limit"),
    ("sizing:cash_zero", "there isn't enough cash"),
    ("emergency_pause", "you paused new buys"),
    ("intraday_loss_halt", "the account fell more than your daily loss limit"),
    ("peak_drawdown_halt", "the account fell more than your limit from its peak"),
    ("drawdown_halt_latched", "buying is paused after a big drop until you resume it"),
    ("symbol_not_configured", "the fund is no longer on your list"),
    ("data_gap_at_open", "there was no price data at the open"),
    ("insufficient_cash_at_open", "there wasn't enough cash at the open"),
]


def why(reason: str) -> str:
    out = []
    for part in reason.split(";"):
        part = part.strip()
        if part.startswith("open ") and "above limit" in part:
            out.append("it opened above the price limit")
            continue
        if part.startswith("sized_by="):
            continue
        for code, text in REASONS:
            if part.startswith(code):
                out.append(text)
                break
        else:
            if part:
                out.append(part.replace("_", " "))
    return " and ".join(out)


# --- data helpers -------------------------------------------------------------------------


def sma_series(closes: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    run = 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= n:
            run -= closes[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


class Snapshot:
    """Everything the page needs, read once from the store."""

    def __init__(self, store: Any, symbols: Sequence[str]) -> None:
        self.state = store.load_state()
        self.curve = store.equity_curve()
        self.trades = store.trades()
        self.fills = store.fills()
        self.decisions = store.recent_decisions(400)
        self.events = store.recent_events(30)
        self.paused = store.get_flag("paused")
        self.prices: dict[str, list[Bar]] = {}
        reader = getattr(store, "price_history", None)
        for s in symbols:
            self.prices[s] = reader(s, CHART_DAYS + SLOW) if reader else []

    def latest_decision(self, sym: str):
        last = self.state.last_session if self.state else None
        for d in reversed(self.decisions):
            if d.symbol == sym and d.day == last and d.outcome != "corporate_action":
                return d
        return None

    @property
    def value(self) -> float:
        return self.curve[-1].equity if self.curve else self.state.equity()

    @property
    def invested(self) -> float:
        st = self.state
        return sum(p.shares * st.last_close.get(p.symbol, p.entry_ref) for p in st.positions.values())

    @property
    def start_day(self) -> date | None:
        return self.curve[0].day if self.curve else (self.state.last_session if self.state else None)


# --- sections -----------------------------------------------------------------------------


def fund_story(sym: str, snap: Snapshot) -> tuple[str, str, str]:
    """(badge text, badge kind, one-paragraph explanation)."""
    st = snap.state
    pos = st.positions.get(sym)
    pend = [o for o in st.pending if o.symbol == sym]
    dec = snap.latest_decision(sym)
    if pend and pend[0].action == "exit":
        return ("Selling at next open", "sell",
                f"Selling all {pos.shares if pos else pend[0].shares} shares when the market opens, "
                f"because {why(pend[0].reason)}.")
    if pend:
        o = pend[0]
        return ("Buying at next open", "buy",
                f"Buying {o.shares} shares when the market opens, because {why(o.reason)}. "
                f"It only buys if the price opens below {money(o.limit)}. A safety stop at "
                f"{money(o.stop)} will sell automatically if the price falls that far.")
    if pos:
        last = st.last_close.get(sym, pos.entry_ref)
        gain = (last - pos.entry_price) * pos.shares + pos.dividends
        return ("Holding", "hold",
                f"Holding {pos.shares} shares bought at {money(pos.entry_price)} on "
                f"{day_label(pos.entry_day)}, now worth {money(last * pos.shares)} "
                f"({'up' if gain >= 0 else 'down'} {money(abs(gain))} so far). If the price drops to {money(pos.stop)}, the safety stop "
                "sells automatically. The stop moves up as the price rises, never down.")
    if dec is None:
        return ("Waiting", "watch", "Waiting for the first check after the market closes.")
    if dec.reason == "warmup":
        return ("Waiting", "watch", "Still collecting enough price history to judge the trend.")
    if dec.outcome == "rejected":
        return ("Blocked", "warn", f"It wanted to buy, but didn't because {why(dec.reason)}.")
    d = dec.diagnostics or {}
    close, fast, slow = d.get("close"), d.get("sma_fast"), d.get("sma_slow")
    if close and slow and close <= slow:
        gap = (slow - close) / close
        return ("Watching", "watch",
                f"The price is {gap:.1%} below its {SLOW}-day average, so it isn't in an uptrend. "
                f"The bot buys once the price and the {FAST}-day average are both above the "
                f"{SLOW}-day average.")
    if close and fast and slow and fast <= slow:
        return ("Watching", "watch",
                f"The price is above its {SLOW}-day average, but the {FAST}-day average hasn't "
                "crossed above it yet. The bot waits for that confirmation before buying.")
    return ("Watching", "watch", f"No trade today: {why(dec.reason)}.")


def headline(snap: Snapshot | None, state: Any) -> tuple[str, str]:
    """(big sentence, supporting sentence)."""
    if not state.ok:
        return ("Stopped: a safety check failed.",
                "Nothing will trade until the setting below is fixed in Railway.")
    if snap is None or snap.state is None:
        return ("Getting ready.",
                "Your simulated $10,000 account starts after the next market close, once the "
                "day's prices are final.")
    st = snap.state
    held = sorted(st.positions)
    buys = [o.symbol for o in st.pending if o.action != "exit"]
    sells = [o.symbol for o in st.pending if o.action == "exit"]
    if st.drawdown_halt_latched:
        return ("Buying is on hold after a big drop.",
                "The account fell more than your limit from its peak. It still manages what it "
                "holds. Tap Resume buying below when you're ready.")
    if snap.paused:
        return ("Paused by you.", "No new buys until you resume. Anything already held is still "
                "protected by its safety stop.")
    parts = []
    if buys:
        parts.append(f"Buying {join(buys)} at the next open.")
    if sells:
        parts.append(f"Selling {join(sells)} at the next open.")
    if held and not parts:
        parts.append(f"Holding {join(held)}.")
    if parts:
        invested = snap.invested
        sub = (f"{money(invested, False)} of your {money(snap.value, False)} is invested; the rest "
               "is cash." if invested else "Everything else stays in cash.")
        return (" ".join(parts), sub)
    return ("Watching, all in cash.",
            "None of your three funds is in an uptrend, so the bot is waiting. It only buys in "
            "confirmed uptrends.")


def join(items: Sequence[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def status_line(snap: Snapshot | None, state: Any) -> str:
    bits = []
    sched = state.scheduler
    if snap and snap.state and snap.state.last_session:
        bits.append(f"Based on {day_label(snap.state.last_session)}'s closing prices.")
        nxt = next_trading_day(snap.state.last_session)
        from tradinglab.engine.live import decision_time

        bits.append(f"Next update {day_label(nxt)} after {time_label(decision_time(nxt), state.tz)}.")
    if sched is not None and sched.status.last_check:
        bits.append(f"Last checked {time_label(sched.status.last_check, state.tz)}.")
    return " ".join(bits)


def notice(state: Any) -> str:
    sched = state.scheduler
    msgs = []
    if not state.ok:
        msgs.append(f"Safety check failed, so the simulation is stopped: {state.error}")
    elif state.engine_note:
        msgs.append(state.engine_note)
    if sched is not None:
        rep, err = sched.status.last_report, sched.status.last_error
        if err:
            msgs.append(f"The last check ran into a problem and will retry automatically: {err}")
        elif rep is not None and rep.status == "data_error":
            msgs.append("Couldn't get prices from Tiingo on the last check. It retries every 15 "
                        "minutes.")
        elif rep is not None and rep.status in ("waiting_for_data", "blocked") and rep.messages:
            msgs.append("Waiting on price data: " + rep.messages[0])
    if not msgs:
        return ""
    return "".join(f"<p class='notice'>{esc(m)}</p>" for m in msgs)


def price_chart(sym: str, bars: Sequence[Bar]) -> str:
    if len(bars) < 30:
        return "<p class='quiet small'>The price chart appears once a few weeks of prices are saved.</p>"
    closes = [b.close for b in bars]
    fast, slow = sma_series(closes, FAST), sma_series(closes, SLOW)
    start = max(0, len(bars) - CHART_DAYS)
    view = list(range(start, len(bars)))
    vals = [closes[i] for i in view] + [v for i in view for v in (fast[i], slow[i]) if v]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    w, h, left, right, top, bottom = 360, 170, 2, 58, 8, 22

    def xy(k: int, v: float) -> str:
        x = left + k * (w - left - right) / max(len(view) - 1, 1)
        y = top + (hi - v) * (h - top - bottom) / (hi - lo)
        return f"{x:.1f},{y:.1f}"

    def line(series, cls):
        pts = [xy(k, series[i]) for k, i in enumerate(view) if series[i] is not None]
        return f"<polyline class='{cls}' points='{' '.join(pts)}'/>" if len(pts) > 1 else ""

    months = []
    for k, i in enumerate(view):
        if k == 0 or bars[i].day.month != bars[view[k - 1]].day.month:
            if k > 4:
                x = xy(k, lo).split(",")[0]
                months.append(f"<text x='{x}' y='{h - 6}' class='axis'>{bars[i].day.strftime('%b')}</text>")
    last_x, last_y = xy(len(view) - 1, closes[-1]).split(",")
    label = (f"{sym} price over the last {len(view)} trading days with its {FAST}-day and "
             f"{SLOW}-day averages")
    return (f"<svg viewBox='0 0 {w} {h}' class='chart' role='img' aria-label='{esc(label)}'>"
            f"{line(slow, 'slow')}{line(fast, 'fast')}{line(closes, 'price')}"
            f"<circle cx='{last_x}' cy='{last_y}' r='4' class='dot'/>"
            f"<text x='{float(last_x) + 8:.1f}' y='{float(last_y) + 4:.1f}' class='tag'>"
            f"{money(closes[-1])}</text>{''.join(months)}</svg>"
            "<p class='legend'><span><i class='k price'></i>Price</span>"
            f"<span><i class='k fast'></i>{FAST}-day average</span>"
            f"<span><i class='k slow'></i>{SLOW}-day average</span></p>")


def funds_section(snap: Snapshot, symbols: Sequence[str]) -> str:
    cards = []
    for sym in symbols:
        name, desc = FUNDS.get(sym, (sym, ""))
        badge, kind, story = fund_story(sym, snap)
        bars = snap.prices.get(sym, [])
        price = ""
        if bars:
            chg = bars[-1].close / bars[-2].close - 1 if len(bars) > 1 else 0.0
            price = (f"<span class='px'>{money(bars[-1].close)}</span>"
                     f"<span class='{tone(chg)} small'>{pct(chg)} that day</span>")
        cards.append(f"""<article class='fund'>
<header><div><h3>{esc(sym)}</h3><p class='quiet small'>{esc(name)}, {esc(desc)}</p></div>
<span class='badge {kind}'>{esc(badge)}</span></header>
<div class='pxrow'>{price}</div>
{price_chart(sym, bars)}
<p class='story'>{esc(story)}</p></article>""")
    return "<section><h2>Your three funds</h2>" + "".join(cards) + "</section>"


def money_section(snap: Snapshot) -> str:
    st = snap.state
    value, start = snap.value, st.initial_capital
    since = value - start
    rows = [f"<div><dt>Cash</dt><dd>{money(st.cash)}</dd></div>",
            f"<div><dt>Invested</dt><dd>{money(snap.invested)}</dd></div>"]
    compare = ""
    spy = snap.prices.get("SPY", [])
    if snap.start_day and spy and len(snap.curve) >= 2:
        start_bar = next((b for b in spy if b.day >= snap.start_day), None)
        if start_bar:
            hold = start * spy[-1].close / start_bar.close
            diff = value - hold
            compare = (f"<p class='compare'>If you'd simply bought SPY on "
                       f"{day_label(snap.start_day)}, you'd have <b>{money(hold)}</b>. The bot is "
                       f"<span class='{tone(diff)} nw'>{money(abs(diff))} "
                       f"{'ahead of' if diff >= 0 else 'behind'}</span> that.</p>")
    chart = ""
    if len(snap.curve) >= 3:
        vals = [p.equity for p in snap.curve]
        lo, hi = min(vals + [start]), max(vals + [start])
        span = (hi - lo) or 1.0
        w, h = 360, 90
        pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{8 + (hi - v) * (h - 16) / span:.1f}"
                       for i, v in enumerate(vals))
        base_y = 8 + (hi - start) * (h - 16) / span
        chart = (f"<svg viewBox='0 0 {w} {h}' class='chart small-chart' role='img' "
                 f"aria-label='Account value since {snap.start_day}'>"
                 f"<line x1='0' x2='{w}' y1='{base_y:.1f}' y2='{base_y:.1f}' class='base'/>"
                 f"<polyline class='price' points='{pts}'/></svg>"
                 "<p class='legend'>Account value each day. The dotted line is the $10,000 you "
                 "started with.</p>")
    closed = snap.trades
    results = ""
    if closed:
        wins = sum(1 for t in closed if t.pnl > 0)
        total = sum(t.pnl for t in closed)
        results = (f"<p class='quiet'>{len(closed)} finished trade{'s' if len(closed) != 1 else ''}: "
                   f"{wins} made money, {len(closed) - wins} lost money, "
                   f"<span class='{tone(total)}'>{signed(total)}</span> in total.</p>")
    return f"""<section><h2>Your money</h2>
<p class='value'>{money(value)}</p>
<p class='since'><span class='{tone(since)}'>{signed(since)}</span> since you started with
{money(start, False)} on {day_label(snap.start_day) if snap.start_day else '—'}</p>
<dl class='split'>{''.join(rows)}</dl>{compare}{chart}{results}</section>"""


def timeline_section(snap: Snapshot, symbols: Sequence[str]) -> str:
    by_day: dict[date, list[str]] = defaultdict(list)
    started = next((e for e in snap.events if e.kind == "initialized"), None)
    for f in snap.fills:
        if f.side == "buy":
            by_day[f.day].append(f"Bought {f.shares} {f.symbol} at {money(f.fill_price)}.")
    for t in snap.trades:
        word = "made" if t.pnl > 0 else "lost"
        by_day[t.exit_day].append(
            f"Sold {t.shares} {t.symbol} at {money(t.exit_price)} because {why(t.exit_reason)}. "
            f"It {word} {money(abs(t.pnl))} after {t.bars_held} trading day"
            f"{'s' if t.bars_held != 1 else ''}.")
    days = sorted({d.day for d in snap.decisions if d.symbol != "*"} | set(by_day), reverse=True)
    for d in days:
        decs = [x for x in snap.decisions if x.day == d and x.symbol != "*"]
        for x in decs:
            if x.outcome == "order_queued" and x.action == "enter_long":
                by_day[d].append(f"Decided to buy {x.symbol} at the next open.")
            elif x.outcome == "order_queued" and x.action == "exit":
                by_day[d].append(f"Decided to sell {x.symbol} at the next open.")
            elif x.outcome == "rejected":
                by_day[d].append(f"Wanted to buy {x.symbol}, but didn't because {why(x.reason)}.")
            elif x.outcome == "corporate_action" and "dividend" in x.reason:
                by_day[d].append(f"Received a {x.symbol} dividend.")
        if decs and not by_day[d]:
            by_day[d].append(f"Checked {join(symbols)}. No changes.")
    if started and started.session in by_day:
        by_day[started.session].insert(0, "Started your simulated account with $10,000.")
    quiet = f"Checked {join(symbols)}. No changes."
    groups: list[tuple[date, date, list[str]]] = []
    for d in days:
        if by_day[d] == [quiet] and groups and groups[-1][2] == [quiet]:
            groups[-1] = (groups[-1][0], d, groups[-1][2])  # extend the quiet run backwards
        else:
            groups.append((d, d, by_day[d]))
    items = []
    for newest, oldest, lines in groups[:8]:
        when = day_label(newest)
        texts = lines
        if oldest != newest:
            when = f"{day_label(oldest)} to {day_label(newest)}"
            texts = [f"Checked {join(symbols)} each day. No changes."]
        lis = "".join(f"<li>{esc(t)}</li>" for t in texts)
        items.append(f"<li class='day'><time>{esc(when)}</time><ul>{lis}</ul></li>")
    if not items:
        return ""
    return "<section><h2>What it's done</h2><ol class='timeline'>" + "".join(items) + "</ol></section>"


def how_section(state: Any) -> str:
    if not state.report:
        return ""
    s = state.report.settings
    r = s.risk
    cap = float(s.capital_base_usd)
    return f"""<section><details><summary>How the bot works</summary>
<div class='how'>
<p>It watches three index funds once a day, after the market closes, using the day's final prices.
No real money is involved and it isn't connected to any brokerage.</p>
<h4>When it buys</h4>
<p>Only in an uptrend: the price and its {FAST}-day average must both be above the {SLOW}-day
average. It places the order for the next morning's opening price, and skips it if the price jumps
more than 1% overnight.</p>
<h4>When it sells</h4>
<p>When the uptrend ends, or if the price falls to its safety stop. The stop is set below the
buying price when it buys, and moves up as the price rises.</p>
<h4>Your safety limits</h4>
<ul>
<li>Each trade can lose at most about {money(cap * float(r.risk_per_trade_pct), False)}
({float(r.risk_per_trade_pct):.1%} of the account) if the stop is hit.</li>
<li>At most {float(r.max_exposure_pct):.0%} of the account is invested at once, in at most
{r.max_open_positions} funds. No borrowing.</li>
<li>It stops buying for the day after a {float(r.intraday_loss_halt_pct):.0%} daily drop, and
until you resume after a {float(r.peak_drawdown_halt_pct):.0%} drop from its best value.</li>
</ul>
<h4>Costs</h4>
<p>Every simulated trade pays a bit more than real trading costs, so results lean cautious.
Dividends are added when paid. Results are in US dollars.</p>
</div></details></section>"""


def controls_section(state: Any, snap: Snapshot | None) -> str:
    if state.controls is None:
        return ""

    def form(action: str, label: str, kind: str) -> str:
        return (f"<form method='post' action='/control'><input type='hidden' name='csrf' "
                f"value='{esc(state.csrf)}'><input type='hidden' name='action' value='{action}'>"
                f"<button class='{kind}'>{esc(label)}</button></form>")

    paused = bool(snap and snap.paused)
    latched = bool(snap and snap.state and snap.state.drawdown_halt_latched)
    btns = form("resume", "Resume buying", "primary") if paused else form(
        "pause", "Pause new buys", "secondary")
    if latched:
        btns += form("resume_drawdown", "Resume buying after the drop", "primary")
    text = ("Paused. No new buys until you tap Resume buying. Safety stops still protect anything "
            "held." if paused else
            "Pausing stops new buys. Anything already held keeps its safety stop.")
    return f"<section class='controls'><h2>Controls</h2><p class='quiet'>{text}</p><div class='btns'>{btns}</div></section>"


def system_section(state: Any) -> str:
    rows = ""
    if state.report:
        rows = "".join(f"<li>{'✓' if ok else '✗'} {esc(name.replace('_', ' '))}: {esc(detail)}</li>"
                       for name, ok, detail in state.report.checks)
    ok_text = ("Safety checks passed. It can't connect to any brokerage, so no real trade is "
               "possible." if state.ok else "Safety checks failed; the simulation is stopped.")
    return f"""<footer><p class='{'ok' if state.ok else 'loss'}'>{esc(ok_text)}</p>
<details><summary>Technical details</summary><ul class='tech'>{rows}</ul></details></footer>"""


CSS = """
:root{--bg:#EDF0EC;--surface:#FBFCFA;--ink:#1B2A33;--quiet:#5D6C74;--line:#D6DCD6;
--amber:#B7791F;--teal:#2B7A84;--gain:#1D7F52;--loss:#BD3B2A;--buy:#2B5EA8;--warn:#9A6A12;
--display:'Bricolage Grotesque',system-ui,sans-serif;--body:'Public Sans',system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#161C20;--surface:#1E262B;--ink:#E7ECEA;
--quiet:#9AA8AE;--line:#2F393F;--amber:#E0A447;--teal:#5DB7C2;--gain:#5CC48C;--loss:#F08A78;
--buy:#86ACEB;--warn:#E3B358}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 var(--body)}
main{max-width:680px;margin:0 auto;padding:28px 18px 56px}
.brand{font:600 15px/1 var(--display);letter-spacing:.01em;margin:0 0 26px;color:var(--quiet)}
h1{font:650 clamp(30px,8vw,44px)/1.08 var(--display);letter-spacing:-.02em;margin:0 0 12px;
font-variation-settings:'opsz' 96}
.lede{font-size:18px;margin:0 0 10px;max-width:36em}
.status{color:var(--quiet);font-size:14px;margin:0}
h2{font:650 22px/1.2 var(--display);letter-spacing:-.01em;margin:44px 0 14px}
h3{font:700 24px/1 var(--display);margin:0}
h4{font:650 16px/1.3 var(--display);margin:18px 0 4px}
.notice{background:var(--surface);border-left:4px solid var(--warn);padding:12px 14px;margin:18px 0 0;
border-radius:0 8px 8px 0}
.quiet{color:var(--quiet)} .small{font-size:14px}
.nw{white-space:nowrap}
.gain{color:var(--gain)} .loss{color:var(--loss)} .flat{color:var(--quiet)}
.value{font:650 clamp(40px,11vw,56px)/1 var(--display);letter-spacing:-.03em;margin:0;
font-variant-numeric:tabular-nums}
.since{margin:8px 0 16px}
.split{display:flex;gap:28px;margin:0 0 14px}
.split dt{color:var(--quiet);font-size:14px} .split dd{margin:0;font:600 19px/1.3 var(--display)}
.compare{background:var(--surface);padding:12px 14px;border-radius:10px;margin:14px 0}
.fund{background:var(--surface);border-radius:14px;padding:18px 16px 14px;margin:0 0 14px;
border:1px solid var(--line)}
.fund header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.fund header p{margin:6px 0 0}
.badge{font:600 14px/1.2 var(--body);white-space:nowrap;padding-top:4px;display:flex;align-items:center;gap:7px}
.badge::before{content:'';width:9px;height:9px;border-radius:50%;background:currentColor}
.badge.watch{color:var(--quiet)} .badge.hold{color:var(--gain)} .badge.buy{color:var(--buy)}
.badge.sell{color:var(--loss)} .badge.warn{color:var(--warn)}
.pxrow{display:flex;align-items:baseline;gap:10px;margin:14px 0 4px}
.px{font:650 22px/1 var(--display);font-variant-numeric:tabular-nums}
.chart{width:100%;height:auto;display:block;margin:6px 0 2px;overflow:visible}
.chart polyline{fill:none;stroke-linejoin:round;stroke-linecap:round}
.chart .price{stroke:var(--ink);stroke-width:1.6}
.chart .slow{stroke:var(--amber);stroke-width:2}
.chart .fast{stroke:var(--teal);stroke-width:1.6;stroke-dasharray:4 3}
.chart .dot{fill:var(--ink)} .chart .tag{font:600 13px var(--body);fill:var(--ink)}
.chart .axis{font:11px var(--body);fill:var(--quiet)}
.chart .base{stroke:var(--quiet);stroke-width:1;stroke-dasharray:2 3}
.legend{font-size:13px;color:var(--quiet);margin:4px 0 0;display:flex;flex-wrap:wrap;
align-items:center;gap:4px 14px}
.legend span{white-space:nowrap}
.legend .k{display:inline-block;width:18px;height:0;border-top:2.5px solid;margin-right:6px;
vertical-align:middle}
.legend .k.price{border-color:var(--ink)} .legend .k.slow{border-color:var(--amber)}
.legend .k.fast{border-color:var(--teal);border-top-style:dashed}
.story{margin:12px 0 0;max-width:38em}
.timeline{list-style:none;margin:0;padding:0;border-left:2px solid var(--line)}
.timeline .day{position:relative;padding:0 0 18px 18px}
.timeline .day::before{content:'';position:absolute;left:-6px;top:6px;width:10px;height:10px;
border-radius:50%;background:var(--bg);border:2px solid var(--quiet)}
.timeline time{font:650 15px/1.4 var(--display)}
.timeline ul{margin:2px 0 0;padding:0;list-style:none}
.timeline ul li{margin:2px 0}
details{margin-top:8px;background:var(--surface);border:1px solid var(--line);border-radius:12px}
summary{cursor:pointer;font:650 18px/1.3 var(--display);padding:16px;list-style:none;display:flex;
justify-content:space-between;align-items:center;gap:12px}
summary::-webkit-details-marker{display:none}
summary::after{content:'Show';font:600 14px/1 var(--body);color:var(--teal);white-space:nowrap}
details[open] summary::after{content:'Hide'}
.how{padding:0 16px 14px}
.how p,.how li{max-width:38em} .how ul{padding-left:20px}
.btns{display:flex;gap:10px;flex-wrap:wrap}
button{font:600 16px/1 var(--body);padding:13px 18px;border-radius:10px;min-height:46px;cursor:pointer;
border:1.5px solid var(--ink);background:transparent;color:var(--ink)}
button.primary{background:var(--ink);color:var(--bg)}
button:hover{background:var(--ink);color:var(--bg)} button:active{transform:translateY(1px)}
button:focus-visible,summary:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
footer{margin-top:48px;padding-top:18px;border-top:1px solid var(--line);font-size:14px}
footer p{margin:0 0 6px}
footer .ok{color:var(--gain)}
footer details{background:none;border:0}
footer summary{font:600 14px/1.3 var(--body);color:var(--quiet);padding:6px 0;justify-content:flex-start}
.tech{padding:0 0 0 0;list-style:none;color:var(--quiet);font-size:13px;word-break:break-word}
"""

FONTS = ("https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..700"
         "&family=Public+Sans:wght@400;600;700&display=swap")


def render_page(state: Any) -> str:
    symbols = list(state.report.settings.symbols) if state.report else list(FUNDS)
    snap = None
    body_main = ""
    if state.store is not None:
        try:
            snap = Snapshot(state.store, symbols)
        except Exception as exc:  # never 500 the page
            return _shell(state, "Can't read the account right now.",
                          f"The database didn't respond ({type(exc).__name__}). It will retry "
                          "automatically.", "", "")
    big, sub = headline(snap, state)
    if snap is not None and snap.state is not None:
        body_main = (money_section(snap) + funds_section(snap, symbols)
                     + timeline_section(snap, symbols))
    return _shell(state, big, sub, status_line(snap, state), body_main + how_section(state)
                  + controls_section(state, snap), snap)


def _shell(state: Any, big: str, sub: str, status: str, body: str, snap: Any = None) -> str:
    now = datetime.now(UTC).astimezone(state.tz)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Trading Lab</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONTS}"><style>{CSS}</style></head><body><main>
<p class="brand">Trading Lab <span class="quiet">simulated account</span></p>
<h1>{esc(big)}</h1><p class="lede">{esc(sub)}</p><p class="status">{esc(status)}</p>
{notice(state)}
{body}
{system_section(state)}
<p class="quiet small">Page loaded {esc(now.strftime('%b %-d, %-I:%M %p'))}.</p>
</main></body></html>"""

