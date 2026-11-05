"""The hype bot's page: its pretend bankroll, open coins, every closed trade, and the live
hype radar (what it's looking at right now and why it did or didn't buy)."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from tradinglab.hype.bot import equity
from tradinglab.hype.model import Account, Rules
from tradinglab.web.views import _shell, money, signed

esc = html.escape


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def _tone(x: float) -> str:
    return "up" if x > 0 else "down" if x < 0 else ""


def _ago(iso: str, now: datetime) -> str:
    try:
        mins = int((now - datetime.fromisoformat(iso)).total_seconds() // 60)
    except ValueError:
        return ""
    return f"{mins} min" if mins < 120 else f"{mins // 60} h {mins % 60} min"


def _price(p: float) -> str:
    return f"${p:.10f}".rstrip("0") if p < 0.01 else f"${p:,.4f}"


def hype_page(state: Any, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    runner = getattr(state, "hype", None)
    store = getattr(runner, "store", None)
    rules: Rules = getattr(runner, "rules", Rules())
    if store is None:
        return _shell(state, "The hype bot isn't running.",
                      "It starts with the web service once the database is connected "
                      "(and HYPE_BOT isn't set to off).", "", "", back="Hype bot")
    try:
        acct: Account | None = store.load_hype()
        trades = store.hype_trades(100)
        n, wins, pnl_closed, best, worst = store.hype_stats()
    except Exception as exc:  # noqa: BLE001 - never 500 the page
        return _shell(state, "Can't read the hype bot right now.", f"{type(exc).__name__}", "", "",
                      back="Hype bot")
    if acct is None:
        return _shell(state, "Warming up.", "The hype bot hasn't finished its first scan yet. "
                      "Refresh in a minute.", "", "", back="Hype bot", refresh=30)
    value = equity(acct, rules)
    total = value - rules.bankroll
    big = f"{money(value)} ({signed(total)})"
    sub = (f"Pretend ${rules.bankroll:,.0f} bankroll, ${rules.stake:,.0f} per coin, following Solana "
           f"memecoin hype 24/7. {n} closed trades, {wins} winners"
           + (f" ({wins / n:.0%})" if n else "") + ".")
    status = (f"Last scan {_ago(acct.last_cycle, now)} ago." if acct.last_cycle else "")
    if acct.last_error:
        status += f" Last problem: {acct.last_error}"

    # open positions
    if acct.positions:
        rows = "".join(
            f"<tr><th scope='row'><a href='{esc(p.url)}' rel='noopener noreferrer' target='_blank'>"
            f"{esc(p.symbol)}</a></th><td class='{_tone(p.gain())}'>{_pct(p.gain())}</td>"
            f"<td>{_pct(p.peak / p.entry_price - 1)}</td><td>{_ago(p.opened, now)}</td>"
            f"<td class='small'>{'half sold, ' if p.half_taken else ''}{esc(p.why)}</td></tr>"
            for p in acct.positions)
        open_html = (f"<section><h2>Holding now ({len(acct.positions)})</h2><table><thead><tr>"
                     "<th>Coin</th><th>Now</th><th>Best</th><th>Held</th><th>Why it bought</th></tr>"
                     f"</thead><tbody>{rows}</tbody></table></section>")
    else:
        open_html = ("<section><h2>Holding now</h2><p class='quiet'>Nothing. Waiting for a setup.</p>"
                     "</section>")

    # closed trades
    if trades:
        rows = "".join(
            f"<tr><th scope='row'><a href='{esc(t.url)}' rel='noopener noreferrer' target='_blank'>"
            f"{esc(t.symbol)}</a></th><td class='{_tone(t.pnl)}'>{signed(t.pnl)}</td>"
            f"<td>{_pct(t.peak_gain)}</td><td class='small'>{esc(t.reason)}</td></tr>" for t in trades[:50])
        stats = (f"<p class='quiet small'>All closed trades: {signed(pnl_closed)} total, "
                 f"best {signed(best)}, "
                 f"worst {signed(worst)}.</p>")
        closed_html = (f"<section><h2>Closed trades</h2>{stats}<table><thead><tr><th>Coin</th><th>Result</th>"
                       "<th>Best it got</th><th>Why it sold</th></tr></thead>"
                       f"<tbody>{rows}</tbody></table></section>")
    else:
        closed_html = "<section><h2>Closed trades</h2><p class='quiet'>None yet.</p></section>"

    # radar
    if acct.watch:
        rows = "".join(
            f"<tr><th scope='row'><a href='{esc(w['url'])}' rel='noopener noreferrer' target='_blank'>"
            f"{esc(w['symbol'])}</a></th><td>{w['score']:.0f}</td>"
            f"<td>{w['buy_ratio']:.0%}</td><td>{w['vol_accel']:.1f}x</td>"
            f"<td class='{_tone(w['chg_5m'])}'>{w['chg_5m']:+.0f}%</td>"
            f"<td class='small'>{'BOUGHT' if w['bought'] else esc('; '.join(w['why_not']))}</td></tr>"
            for w in acct.watch)
        radar = ("<section><h2>Hype radar</h2><p class='quiet small'>The most-hyped Solana coins in the "
                 "last scan, best first. Score = buy pressure + volume surge + momentum + promotion.</p>"
                 "<table><thead><tr><th>Coin</th><th>Score</th><th>Buys</th><th>Volume</th><th>5 min</th>"
                 f"<th>Decision</th></tr></thead><tbody>{rows}</tbody></table></section>")
    else:
        radar = ""

    how = f"""<section><details><summary>How this bot trades (and why it's honest)</summary>
<div class='how'><ul>
<li>Every minute it pulls the Solana coins being boosted and promoted on DexScreener, then checks each
one's last 5 minutes: who's buying, how fast volume is growing, and how far the price moved.</li>
<li>It buys only when buyers clearly outnumber sellers (at least {rules.min_buy_ratio:.0%}), volume is
{rules.min_vol_accel:.1f}x normal, the price is up but not already vertical, the pool holds at least
${rules.min_liquidity:,.0f} and the coin is at least {rules.min_age_min:.0f} minutes old.</li>
<li>It sells half at +{rules.take_half_at:.0%} (a 2x) and then won't let the rest go below breakeven;
stops out at {rules.stop_loss:.0%}; trails {rules.trail_drop:.0%} below the top once it's up
{rules.trail_after:.0%}; gives up if nothing happens in {rules.stale_minutes:.0f} minutes; and never holds
more than {rules.max_hold_hours:.0f} hours.</li>
<li>Costs are harsh on purpose: a {rules.latency_penalty:.0%} delay penalty on every buy,
~{rules.platform_fee:.0%}
platform fee and pool fee each way, price impact from the pool's depth, and network fees. If a pool is pulled
(rug) it books a {rules.rug_loss:.0%} loss.</li>
<li>It only sees prices every 30 seconds, so real fast crashes can be worse than shown. Pretend money only:
it has no wallet and can't trade.</li></ul></div></details></section>"""
    return _shell(state, big, sub, status, open_html + radar + closed_html + how, back="Hype bot",
                  title="Hype bot", refresh=30)
