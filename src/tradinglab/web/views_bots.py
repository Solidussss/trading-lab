"""The leaderboard (home page) and one page per day-trading bot.

Written for someone with no trading background: every number says what it is, every trade says
why it happened, and nothing uses jargon without explaining it.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from tradinglab.intraday.core import DayResult, IFill
from tradinglab.intraday.live import DEFAULT_BOTS, BotSpec, settled
from tradinglab.intraday.research import TRAIN, VALIDATION, backfill_status
from tradinglab.intraday.strategies import REGISTRY
from tradinglab.marketdata.calendar import (
    is_trading_day,
    next_trading_day,
    ny_date,
    previous_trading_day,
    session,
)
from tradinglab.web.views import (
    FUNDS,
    TREND_NAME,
    Snapshot,
    _shell,
    capital_of,
    day_label,
    money,
    pct,
    signed,
    tone,
)

esc = html.escape

EXIT_WORDS = {
    "end_of_day": "at the close (it never holds overnight)",
    "stop_hit": "because the price fell to the safety stop",
    "stop_gap": "because the price dropped past the safety stop",
    "target_hit": "because it reached the target price",
    "time_exit": "because the bounce hadn't happened by 1 pm",
    "back_at_average": "because the price got back to the day's average",
    "trend_broke": "because the price fell below the day's average (the trend broke)",
}
ENTRY_WORDS = {
    "breakout_above_opening_range": "it broke above its high from the first 30 minutes",
    "gap_down_open": "it opened lower than yesterday's close",
    "far_below_average": "it dropped unusually far below the day's average price",
    "bounce_off_rising_average": "it bounced off the day's rising average price",
    "fair_value_gap_order": "the price came back to the middle of a fair value gap",
}
REJECT_WORDS = {
    "too_close_to_the_close": "it was too close to the end of the day",
    "max_entries_today": "it had already traded that fund as often as allowed today",
    "paused_by_owner": "you paused new buys",
    "daily_loss_halt": "the bot was down 3% on the day, so it stopped buying",
    "peak_drawdown_halt": "the bot is 10% below its best, so buying is on hold",
    "max_open_positions": "it already held 3 positions",
    "stop_not_below_price": "the safety stop wasn't below the price",
    "position_too_small": "even one share would have risked more than its limit",
    "max_exposure_reached": "30% of its money was already invested (its limit)",
    "not_enough_cash": "there wasn't enough cash left",
    "opened_below_stop": "the price opened below the safety stop",
    "opened_above_target": "the price had already reached the target",
    "gap_failed": "the price closed below the gap, so the gap failed",
}


def tone_usd(x: float) -> str:
    return "gain" if x >= 0.5 else ("loss" if x <= -0.5 else "flat")


def local_time(ts: datetime, tz) -> str:
    return ts.astimezone(tz).strftime("%-I:%M %p").lower()


def _reason(code: str, table: dict[str, str]) -> str:
    parts = [p.strip() for p in code.split(";")]
    for p in reversed(parts):
        if p in table:
            return table[p]
    return parts[-1].replace("_", " ")


# --- data for one bot ------------------------------------------------------------------------


@dataclass
class BotView:
    id: str
    name: str
    summary: str
    value: float
    start_capital: float
    today_pnl: float | None
    today: DayResult | None
    trades: int
    wins: int
    started: date | None
    status: str
    href: str
    kind: str = "day"

    @property
    def total(self) -> float:
        return self.value - self.start_capital

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.trades if self.trades else None


def bot_view(state: Any, bot: BotSpec, now: datetime) -> BotView:
    store, runner = state.store, state.intraday
    cls = REGISTRY[bot.strategy_id]
    st = store.load_bot(bot.id)
    trades = store.bot_trades(bot.id)
    today = runner.today(bot, now) if runner is not None else None
    cap = st.initial_capital if st else capital_of(state)
    value = (st.cash if st else cap) + (today.pnl if today else 0.0)
    days = store.bot_days(bot.id)
    today_pnl = today.pnl if today else (days[-1].pnl if days and days[-1].day == ny_date(now)
                                         else None)
    t_all = trades + (today.trades if today else [])
    return BotView(bot.id, cls.name, cls.summary, value, cap, today_pnl, today, len(t_all),
                   sum(1 for t in t_all if t.pnl > 0), st.start_day if st else None,
                   bot_status(state, st, today, now), f"/bot/{bot.id}")


def trend_view(state: Any, now: datetime) -> BotView | None:
    try:
        snap = Snapshot(state.store, list(state.report.settings.symbols))
    except Exception:  # noqa: BLE001
        return None
    if snap.state is None:
        return None
    curve = snap.curve
    today_pnl = (curve[-1].equity - curve[-2].equity) if len(curve) >= 2 else None
    held = sorted(snap.state.positions)
    status = ("Holding " + ", ".join(held)) if held else (
        "Buying at the next open" if snap.state.pending else "Waiting for an uptrend, in cash")
    return BotView("trend", TREND_NAME, "Checks once a day after the close; buys funds in a "
                   "long uptrend and can hold them for weeks.", snap.value,
                   snap.state.initial_capital, today_pnl, None, len(snap.trades),
                   sum(1 for t in snap.trades if t.pnl > 0), snap.start_day, status,
                   "/bot/trend", kind="trend")


def bot_status(state: Any, st, today: DayResult | None, now: datetime) -> str:
    if st is None:
        return "Starting soon"
    if st.halt_latched:
        return "On hold after a 10% drop"
    d = ny_date(now)
    if st.start_day and st.start_day > d:
        return f"Starts trading {day_label(st.start_day)}"
    if today is not None:
        if today.open_positions:
            return "Holding " + ", ".join(sorted(today.open_positions))
        if today.trades:
            n = len(today.trades)
            return f"{n} trade{'s' if n != 1 else ''} today, now in cash"
        return "Watching, no trades yet today"
    return "Market closed, all in cash"


# --- market clock ------------------------------------------------------------------------------


def market_line(now: datetime, tz) -> str:
    d = ny_date(now)
    if is_trading_day(d):
        s = session(d)
        if now < s.open_utc:
            return f"The market opens today at {local_time(s.open_utc, tz)} your time."
        if now < s.close_utc:
            return (f"The market is open until {local_time(s.close_utc, tz)} your time. "
                    "Prices update every 5 minutes.")
        if not settled(d, now):
            return "The market just closed. Today's results are being finalised."
    nxt = next_trading_day(d)
    return (f"The market is closed. It opens {day_label(nxt)} at "
            f"{local_time(session(nxt).open_utc, tz)} your time.")


def market_open(now: datetime) -> bool:
    d = ny_date(now)
    return is_trading_day(d) and session(d).open_utc <= now < session(d).close_utc + timedelta(
        minutes=20)


# --- leaderboard -------------------------------------------------------------------------------


def leaderboard_page(state: Any, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if state.store is None or state.intraday is None:
        from tradinglab.web.views import render_trend_page

        return render_trend_page(state)
    try:
        views = [bot_view(state, b, now) for b in DEFAULT_BOTS]
        trend = trend_view(state, now)
    except Exception as exc:  # noqa: BLE001 - never 500 the page
        return _shell(state, "Can't read the accounts right now.",
                      f"The database didn't respond ({type(exc).__name__}). It retries "
                      "automatically.", "", "")
    if trend:
        views.append(trend)
    views.sort(key=lambda v: v.total, reverse=True)
    big, sub = leaderboard_headline(views, now, state)
    rows = "".join(leaderboard_row(i, v) for i, v in enumerate(views, 1))
    body = (f"<section><h2>The bots</h2><p class='quiet small'>Each bot has its own "
            f"{money(capital_of(state), False)} of pretend money and its own strategy. Ranked by "
            f"how much each has made or lost since it started.</p><ol class='board'>{rows}</ol>"
            "</section>" + research_section(state, now) + rules_section(state)
            + controls(state))
    return _shell(state, big, sub, market_line(now, state.tz), body,
                  refresh=60 if market_open(now) else 0)


def leaderboard_headline(views: Sequence[BotView], now: datetime, state: Any) -> tuple[str, str]:
    if state.store.get_flag("paused"):
        return ("New buys are paused.", "You paused every bot. Anything they hold is still "
                "managed and sold by the end of the day.")
    d = ny_date(now)
    day_bots = [v for v in views if v.kind == "day"]
    if all(v.started and v.started > d for v in day_bots):
        start = min(v.started for v in day_bots if v.started)
        return (f"{len(day_bots)} day-trading bots start {day_label(start)}.",
                f"Each one gets {money(capital_of(state), False)} of pretend money and a different "
                "strategy. They buy and sell during the day, and sell everything before the close.")
    live = [v for v in day_bots if v.today is not None]
    if live and market_open(now):
        traded = [v for v in live if v.today and (v.today.trades or v.today.open_positions)]
        best = max(live, key=lambda v: v.today_pnl or 0.0)
        if not traded:
            return ("No trades yet today.", "The bots are watching for their setups.")
        return (f"{len(traded)} of {len(live)} day-trading bots have traded today.",
                f"Best so far: {best.name}, {signed(best.today_pnl or 0.0)}.")
    leader = views[0]
    return (f"{leader.name} is in the lead.",
            f"It's {signed(leader.total)} since it started. Tap any bot to see every trade and why "
            "it made it.")


def leaderboard_row(rank: int, v: BotView) -> str:
    today = ("<span class='quiet'>—</span>" if v.today_pnl is None
             else f"<span class='{tone_usd(v.today_pnl)}'>{signed(v.today_pnl)}</span>")
    wr = f"{v.win_rate:.0%} of {v.trades} trades won" if v.win_rate is not None else "No trades yet"
    return f"""<li><a class='botrow' href='{esc(v.href)}'>
<span class='rank'>{rank}</span>
<span class='who'><b>{esc(v.name)}</b><span class='quiet small'>{esc(v.summary)}</span>
<span class='small status-line'>{esc(v.status)}</span></span>
<span class='nums'><b class='val'>{money(v.value, False)}</b>
<span class='{tone_usd(v.total)} small nw'>{signed(v.total, False)} total</span>
<span class='small nw'>Today {today}</span><span class='quiet small nw'>{esc(wr)}</span></span>
</a></li>"""


# --- research ----------------------------------------------------------------------------------


def research_section(state: Any, now: datetime) -> str:
    syms = list(state.report.settings.symbols)
    end = previous_trading_day(ny_date(now))
    try:
        status = backfill_status(state.store, syms, end)
        results = {b.id: state.store.backtest(b.id) for b in DEFAULT_BOTS}
    except Exception:  # noqa: BLE001
        return ""
    rows = []
    for b in DEFAULT_BOTS:
        r = results.get(b.id)
        name = REGISTRY[b.strategy_id].name
        if not r:
            continue
        cells = []
        for key in ("train", "validation"):
            w = r["windows"].get(key, {})
            cells.append(_window_cell(w))
        rows.append(f"<tr><th scope='row'><a href='/bot/{b.id}'>{esc(name)}</a></th>"
                    f"{''.join(cells)}</tr>")
    progress = ("" if status.done else
                f"<p class='quiet'>Loading past 5-minute prices back to August 2017: "
                f"<b>{status.fraction:.0%}</b> done. Results fill in as more history loads.</p>")
    table = ""
    if rows:
        table = (f"<div class='scroll'><table class='bt'><thead><tr><th></th>"
                 f"<th>{TRAIN.start.year}–{TRAIN.end.year}</th>"
                 f"<th>{VALIDATION.start.year}–{VALIDATION.end.year}</th></tr></thead>"
                 f"<tbody>{''.join(rows)}</tbody></table></div>"
                 "<p class='quiet small'>Each number is how much the bot would have made or lost "
                 "per year, after costs, starting with the same pretend money. Underneath: simply "
                 "holding SPY over the same years. The newest years (2025 on) are kept sealed as a "
                 "final exam, so nobody can tweak a strategy to fit them.</p>")
    return (f"<section><h2>How they did on past prices</h2>{progress}{table or ''}"
            + ("" if rows or not status.done else "<p class='quiet'>Running the tests…</p>")
            + "</section>")


def _annual(total: float | None, days: int) -> float | None:
    if total is None or days <= 0 or total <= -1:
        return None
    return (1 + total) ** (252 / days) - 1


def _window_cell(w: dict) -> str:
    if not w or not w.get("days"):
        return "<td class='quiet'>not loaded yet</td>"
    a = _annual(w.get("total_return"), w["days"])
    b = _annual(w.get("benchmark_return"), w["days"])
    spy = f"<span class='quiet small nw'>SPY {pct(b)}</span>" if b is not None else ""
    return (f"<td><b class='{tone(a or 0)}'>{pct(a or 0)}</b> a year<br>{spy}</td>")


def rules_section(state: Any) -> str:
    if not state.report:
        return ""
    r = state.report.settings.risk
    cap = capital_of(state)
    return f"""<section><details><summary>Rules every bot follows</summary><div class='how'>
<ul>
<li>Pretend money only. Nothing is connected to a bank or brokerage, so no real trade can happen.</li>
<li>Real prices: 5-minute prices for SPY, QQQ and IWM from the IEX exchange (via Tiingo).</li>
<li>When a bot decides to buy or sell, it gets the price at the start of the next 5 minutes, plus
trading costs and commission. It can't see the future.</li>
<li>Each trade risks at most about {money(cap * float(r.risk_per_trade_pct), False)}
({float(r.risk_per_trade_pct):.1%}) if its safety stop is hit. At most
{float(r.max_exposure_pct):.0%} of the money is invested at once, in at most
{r.max_open_positions} funds. No borrowing.</li>
<li>Day-trading bots sell everything before the close and never hold overnight.</li>
<li>A bot that's down {float(r.intraday_loss_halt_pct):.0%} on the day stops buying until
tomorrow. One that falls {float(r.peak_drawdown_halt_pct):.0%} below its best stops buying until
you resume it.</li>
</ul></div></details></section>"""


def controls(state: Any) -> str:
    if state.controls is None:
        return ""
    paused = state.store.get_flag("paused")

    def form(action: str, label: str, kind: str) -> str:
        return (f"<form method='post' action='/control'><input type='hidden' name='csrf' "
                f"value='{esc(state.csrf)}'><input type='hidden' name='action' value='{action}'>"
                f"<button class='{kind}'>{esc(label)}</button></form>")

    btn = form("resume", "Resume buying", "primary") if paused else form(
        "pause", "Pause new buys", "secondary")
    text = ("Paused: no bot will buy anything new. They still sell what they hold."
            if paused else "Stops every bot from buying anything new. They still sell what they "
            "hold, and the day-trading bots still sell everything by the close.")
    return (f"<section class='controls'><h2>Controls</h2><p class='quiet'>{text}</p>"
            f"<div class='btns'>{btn}</div></section>")


# --- one bot -----------------------------------------------------------------------------------


def bot_page(state: Any, bot_id: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now(UTC)
    if bot_id == "trend":
        from tradinglab.web.views import render_trend_page

        return render_trend_page(state)
    bot = next((b for b in DEFAULT_BOTS if b.id == bot_id), None)
    if bot is None or state.store is None or state.intraday is None:
        return None
    cls = REGISTRY[bot.strategy_id]
    v = bot_view(state, bot, now)
    st = state.store.load_bot(bot.id)
    days = state.store.bot_days(bot.id)
    show_day, result = _day_to_show(state, bot, v, days, now)
    big, sub = bot_headline(v, result, show_day, now)
    parts = [
        f"""<section class='acct'><p class='value'>{money(v.value)}</p>
<p class='since'><span class='{tone_usd(v.total)}'>{signed(v.total)}</span> since it
started with {money(v.start_capital, False)}{(' on ' + day_label(v.started)) if v.started else ''}</p>
<p class='quiet'>{esc(v.status)}.</p></section>""",
    ]
    if result is not None and show_day is not None:
        parts.append(day_section(state, bot, show_day, result, now))
    parts.append(history_section(days))
    parts.append(f"""<section><details open><summary>How this bot trades</summary><div class='how'>
<p>{esc(cls.summary)}</p><ul>{''.join(f'<li>{esc(r)}</li>' for r in cls.rules)}</ul>
<p class='quiet small'>Like every bot, it pays trading costs on every trade, risks about
{money(v.start_capital * 0.005, False)} per trade at most, and never holds overnight.</p>
</div></details></section>""")
    parts.append(bot_research(state, bot))
    if st is not None and st.halt_latched:
        parts.append(_resume_form(state, bot.id))
    return _shell(state, big, sub, market_line(now, state.tz), "".join(parts), back=cls.name,
                  title=f"{cls.name} · Trading Lab", refresh=60 if market_open(now) else 0)


def _day_to_show(state: Any, bot: BotSpec, v: BotView, days, now: datetime):
    if v.today is not None:
        return ny_date(now), v.today
    if not days:
        return None, None
    d = days[-1].day
    store = state.store
    r = DayResult(d, days[-1].start_equity, days[-1].end_equity, True,
                  fills=store.bot_fills(bot.id, d), trades=store.bot_trades(bot.id, d),
                  signals=store.bot_signals(bot.id, d), halted=days[-1].halted,
                  excluded=days[-1].excluded)
    return d, r


def bot_headline(v: BotView, r: DayResult | None, d: date | None, now: datetime) -> tuple[str, str]:
    if v.started and v.started > ny_date(now):
        return (f"Starts trading {day_label(v.started)}.", v.summary)
    if r is None:
        return ("No trading days yet.", v.summary)
    when = "today" if d == ny_date(now) else f"on {day_label(d)}"
    if r.open_positions:
        held = ", ".join(sorted(r.open_positions))
        return (f"Holding {held} right now.",
                f"{signed(r.pnl)} {when} so far. It sells everything before the close.")
    if not r.trades:
        return (f"No trades {when}.", "None of its setups showed up. Sitting in cash is a "
                "normal result for a day.")
    won = sum(1 for t in r.trades if t.pnl > 0)
    n = len(r.trades)
    return (f"{signed(r.pnl)} {when}.",
            f"{n} trade{'s' if n != 1 else ''}, {won} made money. Every trade is listed below with "
            "the reason for it.")


def day_section(state: Any, bot: BotSpec, d: date, r: DayResult, now: datetime) -> str:
    tz = state.tz
    symbols = list(state.report.settings.symbols)
    charts = []
    for sym in symbols:
        bars = state.store.ibars(sym, d)
        fills = [f for f in r.fills if f.symbol == sym]
        if not bars:
            continue
        name, desc = FUNDS.get(sym, (sym, ""))
        charts.append(f"<article class='fund'><header><div><h3>{esc(sym)}</h3><p class='quiet small'>"
                      f"{esc(name)}</p></div>{_sym_result(sym, r)}</header>"
                      f"{intraday_chart(sym, bars, fills, tz)}</article>")
    events = timeline(r, tz)
    label = "Today" if d == ny_date(now) else day_label(d)
    legend = ("<p class='legend'><span><i class='k price'></i>Price</span><span><i class='k fast'>"
              "</i>Day's average price</span><span><b class='gain'>▲</b> bought</span>"
              "<span><b class='loss'>▼</b> sold</span></p>")
    return (f"<section><h2>{esc(label)}, trade by trade</h2><p class='quiet small'>Times are "
            "your local time.</p>"
            f"<ol class='timeline'>{events}</ol></section>"
            f"<section><h2>{esc(label)}'s prices</h2>{legend}{''.join(charts)}</section>")


def _sym_result(sym: str, r: DayResult) -> str:
    pnl = sum(t.pnl for t in r.trades if t.symbol == sym)
    if sym in r.open_positions:
        return "<span class='badge hold'>Holding</span>"
    if any(t.symbol == sym for t in r.trades):
        return f"<span class='{tone_usd(pnl)} nw'><b>{signed(pnl)}</b></span>"
    return "<span class='badge watch'>No trade</span>"


def timeline(r: DayResult, tz) -> str:
    items: list[tuple[datetime, str]] = []
    trade_by_exit = {(t.symbol, t.exit_ts): t for t in r.trades}
    for f in r.fills:
        if f.side == "buy":
            stop = next((g.diagnostics.get("stop") for g in r.signals
                         if g.ts == f.ts and g.symbol == f.symbol and g.outcome == "filled"), None)
            text = (f"Bought {f.shares} {f.symbol} at {money(f.fill_price)} because "
                    f"{_reason(f.reason, ENTRY_WORDS)}.")
            if stop:
                text += f" Safety stop at {money(stop)}."
            items.append((f.ts, text))
        else:
            t = trade_by_exit.get((f.symbol, f.ts))
            text = f"Sold {f.shares} {f.symbol} at {money(f.fill_price)} {_reason(f.reason, EXIT_WORDS)}."
            if t:
                text += (f" {'Made' if t.pnl > 0 else 'Lost'} {money(abs(t.pnl))} after costs "
                         f"({money(t.costs)} of that was trading costs).")
            items.append((f.ts, text))
    for g in r.signals:
        if g.outcome == "ordered" and g.action == "enter" and g.diagnostics.get("limit"):
            items.append((g.ts + timedelta(minutes=5), f"Spotted a fair value gap in {g.symbol} "
                          f"between {money(g.diagnostics.get('gap_bottom', 0))} and "
                          f"{money(g.diagnostics.get('gap_top', 0))}. Placed a buy order at "
                          f"{money(g.diagnostics['limit'])}, waiting for the price to come back."))
        if g.outcome == "rejected" and g.symbol != "*" and g.action == "enter":
            items.append((g.ts, f"Saw a reason to buy {g.symbol} but didn't, because "
                                f"{_reason(g.reason, REJECT_WORDS)}."))
        elif g.outcome == "cancelled":
            items.append((g.ts, f"Cancelled a {g.symbol} order because "
                                f"{_reason(g.reason, REJECT_WORDS)}."))
        elif g.symbol == "*" and g.reason in REJECT_WORDS:
            items.append((g.ts, REJECT_WORDS[g.reason].capitalize() + "."))
    for sym, why in r.excluded.items():
        items.append((session(r.day).open_utc, f"Sat out {sym} today: {why}."))
    for sym, p in r.open_positions.items():
        gain = (p["last"] - p["entry"]) * p["shares"]
        items.append((datetime.now(UTC), f"Still holding {int(p['shares'])} {sym}: "
                      f"{'up' if gain >= 0 else 'down'} {money(abs(gain))} so far. "
                      f"Safety stop {money(p['stop'])}."))
    if not items:
        return "<li class='day'><ul><li>No trades. None of its setups showed up.</li></ul></li>"
    items.sort(key=lambda x: x[0])
    return "".join(f"<li class='day'><time>{esc(local_time(ts, tz))}</time><ul><li>{esc(t)}</li>"
                   f"</ul></li>" for ts, t in items)


def intraday_chart(sym: str, bars, fills: Sequence[IFill], tz) -> str:
    w, h, left, right, top, bottom = 360, 150, 2, 54, 8, 20
    vw, pv, vol = [], 0.0, 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3
        wgt = b.volume if b.volume > 0 else 1.0
        pv, vol = pv + tp * wgt, vol + wgt
        vw.append(pv / vol)
    s = session(bars[0].day)
    t0, t1 = s.open_utc, s.close_utc
    vals = [b.close for b in bars] + vw + [f.ref_price for f in fills]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.1 or 0.5
    lo, hi = lo - pad, hi + pad

    def x(ts: datetime) -> float:
        return left + (ts - t0) / (t1 - t0) * (w - left - right)

    def y(v: float) -> float:
        return top + (hi - v) * (h - top - bottom) / (hi - lo)

    price = " ".join(f"{x(b.end):.1f},{y(b.close):.1f}" for b in bars)
    avg = " ".join(f"{x(b.end):.1f},{y(v):.1f}" for b, v in zip(bars, vw, strict=True))
    marks = []
    for f in fills:
        cx, cy = x(f.ts), y(f.ref_price)
        if f.side == "buy":
            marks.append(f"<path class='buy' d='M{cx:.1f},{cy - 7:.1f} l6,10 h-12 z'/>")
        else:
            marks.append(f"<path class='sell' d='M{cx:.1f},{cy + 7:.1f} l6,-10 h-12 z'/>")
    ticks = []
    t = t0
    while t <= t1:
        ticks.append(f"<text x='{x(t):.1f}' y='{h - 5}' class='axis' text-anchor='middle'>"
                     f"{t.astimezone(tz).strftime('%-I%p').lower()}</text>")
        t += timedelta(hours=2)
    last = bars[-1]
    return (f"<svg viewBox='0 0 {w} {h}' class='chart' role='img' aria-label='{esc(sym)} price "
            f"every 5 minutes with the day&#39;s average price and the bot&#39;s trades'>"
            f"<polyline class='fast' points='{avg}'/><polyline class='price' points='{price}'/>"
            f"{''.join(marks)}<text x='{x(last.end) + 6:.1f}' y='{y(last.close) + 4:.1f}' "
            f"class='tag'>{money(last.close)}</text>{''.join(ticks)}</svg>")


def history_section(days) -> str:
    if not days:
        return ""
    rows = []
    for d in reversed(days[-15:]):
        rows.append(f"<li><span>{esc(day_label(d.day))}</span><span class='quiet small'>"
                    f"{d.trades} trade{'s' if d.trades != 1 else ''}</span>"
                    f"<b class='{tone_usd(d.pnl)} nw'>{signed(d.pnl)}</b></li>")
    good = sum(1 for d in days if d.pnl > 0.005)
    bad = sum(1 for d in days if d.pnl < -0.005)
    return (f"<section><h2>Day by day</h2><p class='quiet small'>{len(days)} trading day"
            f"{'s' if len(days) != 1 else ''}: {good} up, {bad} down, "
            f"{len(days) - good - bad} flat.</p><ul class='daylist'>{''.join(rows)}</ul></section>")


def bot_research(state: Any, bot: BotSpec) -> str:
    r = state.store.backtest(bot.id)
    if not r:
        return ("<section><h2>On past prices</h2><p class='quiet'>Past results appear here once "
                "enough history has loaded.</p></section>")
    rows = []
    for key, label in (("train", f"{TRAIN.start.year}–{TRAIN.end.year}"),
                       ("validation", f"{VALIDATION.start.year}–{VALIDATION.end.year}"),
                       ("test", "2025 on (final exam)")):
        w = r["windows"].get(key, {})
        if w.get("sealed"):
            rows.append(f"<tr><th scope='row'>{label}</th><td colspan='4' class='quiet'>Sealed. "
                        "Opened only once you're happy with the strategy.</td></tr>")
            continue
        if not w.get("days"):
            rows.append(f"<tr><th scope='row'>{label}</th><td colspan='4' class='quiet'>Not loaded "
                        "yet</td></tr>")
            continue
        a = _annual(w["total_return"], w["days"])
        b = _annual(w.get("benchmark_return"), w["days"])
        rows.append(
            f"<tr><th scope='row'>{label}</th><td><b class='{tone(a or 0)}'>{pct(a or 0)}</b></td>"
            f"<td>{pct(b) if b is not None else '—'}</td><td>{pct(w['max_drawdown']).replace('.0%', '%')}</td>"
            f"<td>{w['win_rate']:.0%} of {w['trades']:,}</td></tr>")
    unlock = ""
    if r.get("test_sealed") and state.controls is not None:
        unlock = (f"<details><summary>Open the final exam</summary><div class='how'><p>The final "
                  "exam is the newest stretch of prices, kept unseen so the result is honest. "
                  "Open it once you've decided this strategy is worth judging. After that, any "
                  "change to the strategy has to be judged on new days instead.</p>"
                  f"<form method='post' action='/control'><input type='hidden' name='csrf' "
                  f"value='{esc(state.csrf)}'><input type='hidden' name='action' "
                  f"value='unlock_test:{esc(bot.id)}'><button class='secondary'>Open the final exam"
                  "</button></form></div></details>")
    return (f"<section><h2>On past prices</h2><div class='scroll'><table class='bt'><thead><tr><th></th>"
            "<th>Per year</th><th>SPY per year</th><th>Worst drop</th><th>Trades won</th></tr>"
            f"</thead><tbody>{''.join(rows)}</tbody></table></div><p class='quiet small'>Same "
            "rules, costs and pretend money as live. “Worst drop” is the biggest fall from a high "
            f"point.</p>{unlock}</section>")


def _resume_form(state: Any, bot_id: str) -> str:
    return (f"<section class='controls'><h2>On hold</h2><p class='quiet'>This bot fell 10% below "
            "its best, so it stopped buying. Resume when you're ready.</p><form method='post' "
            f"action='/control'><input type='hidden' name='csrf' value='{esc(state.csrf)}'>"
            f"<input type='hidden' name='action' value='resume_bot:{esc(bot_id)}'>"
            "<button class='primary'>Resume this bot</button></form></section>")

